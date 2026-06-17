#!/usr/bin/env python3
"""BhuMe boundary alignment solver.

For each plot in a village:
  1. Check the drawn area against the 7/12 recorded area — flag severe mismatches.
  2. Extract a patch from boundaries.tif around the plot.
  3. Cross-correlate the rasterised boundary ring against the edge image to find (dx, dy).
  4. Apply the shift if confidence is high enough; flag otherwise.
  5. For dense villages, fall back to a global consensus shift for low-confidence plots.

Usage:
    python solve.py data/34855_vadnerbhairav_chandavad_nashik
    python solve.py data/12429_malatavadi_chandgad_kolhapur
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.features import rasterize
from rasterio.transform import from_bounds
from rasterio.windows import from_bounds as win_from_bounds
from scipy.ndimage import binary_erosion, sobel
from scipy.signal import fftconvolve
from shapely.affinity import translate
from shapely.geometry import mapping

import geopandas as gpd

from bhume import load, write_predictions, score
from bhume.geo import open_imagery, geom_to_imagery_crs


# -- tuneable constants -------------------------------------------------------

PAD_M = 80               # metres of padding around each plot for the patch
CORR_PEAK_THRESHOLD = 3.5  # peak/mean ratio above which we trust the shift
AREA_RATIO_MIN = 0.5     # flag if drawn area < 50 % of recorded
AREA_RATIO_MAX = 2.0     # flag if drawn area > 200 % of recorded
MAX_SHIFT_OPEN_M = 120   # maximum plausible shift for open (large-plot) villages
MAX_SHIFT_DENSE_M = 20   # maximum plausible shift for dense (small-plot) villages
CONFIDENCE_FLOOR = 0.25  # below this the shift is not applied
DENSE_THRESHOLD_SQM = 3000  # median plot area below which the village is treated as dense


# -- helpers ------------------------------------------------------------------

def _utm_for(geom) -> str:
    """Return the EPSG code of the UTM zone covering a lon/lat geometry."""
    lon = geom.centroid.x
    return f'EPSG:{32600 + int((lon + 180) // 6) + 1}'


def _area_ratio(props: dict) -> float | None:
    """Drawn-area / recorded-area ratio, or None if either value is missing.

    The recorded area is the 7/12 cultivable area plus pot-kharaba (uncultivable),
    which is stored in hectares and converted here.
    """
    map_area = props.get('map_area_sqm')
    recorded = props.get('recorded_area_sqm')
    pot = props.get('pot_kharaba_ha')

    if not map_area or map_area <= 0:
        return None
    if not recorded or recorded <= 0:
        return None

    total = recorded + (pot * 10_000 if pot and pot > 0 else 0)  # ha → sqm
    return map_area / total


def _cross_correlate_shift(
    src: rasterio.DatasetReader,
    geom_4326,
    max_shift_m: float = MAX_SHIFT_OPEN_M,
    pad_m: float = PAD_M,
) -> tuple[float, float, float]:
    """Find the (dx_m, dy_m, confidence) that best aligns the plot to visible edges.

    Projects the plot into the imagery CRS, reads a padded raster patch,
    rasterises the boundary ring as a template, edge-detects the patch with a Sobel
    filter, then uses FFT cross-correlation to find the peak translation.

    Confidence is the normalised peak-to-mean ratio of the correlation surface: a sharp
    dominant peak means one convincing candidate; a flat surface means ambiguity.
    Returns (0, 0, 0) on any failure and penalises shifts beyond max_shift_m.
    """
    geom_img = geom_to_imagery_crs(src, geom_4326)
    minx, miny, maxx, maxy = geom_img.bounds

    left   = minx - pad_m
    bottom = miny - pad_m
    right  = maxx + pad_m
    top    = maxy + pad_m

    # clip request to the dataset footprint
    dl, db, dr, dt = src.bounds
    left, bottom = max(left, dl), max(bottom, db)
    right, top   = min(right, dr), min(top, dt)

    if right <= left or top <= bottom:
        return 0.0, 0.0, 0.0

    window = win_from_bounds(left, bottom, right, top, transform=src.transform)
    data = src.read(window=window)
    if data.size == 0:
        return 0.0, 0.0, 0.0

    # collapse to single-channel (boundaries.tif is single-band; imagery would be RGB)
    gray = (
        (0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]).astype(np.float32)
        if data.shape[0] >= 3
        else data[0].astype(np.float32)
    )

    H, W = gray.shape
    if H < 5 or W < 5:
        return 0.0, 0.0, 0.0

    patch_tf = src.window_transform(window)
    px_x = abs(patch_tf.a)   # metres per pixel, x
    px_y = abs(patch_tf.e)   # metres per pixel, y

    # rasterise the boundary ring as a thin template mask
    mask_tf = from_bounds(left, bottom, right, top, W, H)
    mask = rasterize(
        [(mapping(geom_img), 1)],
        out_shape=(H, W),
        transform=mask_tf,
        fill=0,
        dtype=np.float32,
    )
    template = mask - binary_erosion(mask.astype(bool)).astype(np.float32)
    if template.sum() == 0:
        return 0.0, 0.0, 0.0

    # Sobel edge detection on the raster patch
    edges = np.hypot(sobel(gray, axis=1), sobel(gray, axis=0)).astype(np.float32)
    if edges.max() > 0:
        edges /= edges.max()

    # FFT cross-correlation — peak location is the best translation
    corr = fftconvolve(edges, template[::-1, ::-1], mode='same')
    peak_idx = np.unravel_index(np.argmax(corr), corr.shape)
    peak_val = corr[peak_idx]
    mean_val = corr.mean()

    if mean_val <= 0:
        return 0.0, 0.0, 0.0

    peak_ratio = peak_val / mean_val
    confidence = min(1.0, max(0.0, (peak_ratio - 1.0) / (CORR_PEAK_THRESHOLD * 3)))

    dy_px = peak_idx[0] - H // 2
    dx_px = peak_idx[1] - W // 2
    dx_m  = dx_px * px_x
    dy_m  = -dy_px * px_y  # y flipped: image row ↓ vs geographic north ↑

    if np.sqrt(dx_m**2 + dy_m**2) > max_shift_m:
        return 0.0, 0.0, confidence * 0.3  # implausible shift; penalise confidence

    return dx_m, dy_m, confidence


# -- main pipeline ------------------------------------------------------------

def align_village(village_dir: str) -> None:
    """Align every plot in a village and write predictions.geojson."""
    village = load(village_dir)
    plots = village.plots
    utm = _utm_for(plots.geometry.iloc[0])

    plots_utm = plots.to_crs(utm)
    median_area = float(plots_utm.geometry.area.median())
    is_dense = median_area < DENSE_THRESHOLD_SQM
    max_shift_m = MAX_SHIFT_DENSE_M if is_dense else MAX_SHIFT_OPEN_M

    print(f'\nProcessing: {village.slug}')
    print(f'  plots={len(plots)}  median_area={median_area:.0f} m²'
          f'  type={"dense" if is_dense else "open"}  max_shift={max_shift_m} m')

    raster_path = village.boundaries_path or village.imagery_path
    print(f'  raster: {"boundaries.tif" if village.boundaries_path else "imagery.tif"}\n')

    results = []
    high_conf_shifts: list[tuple[float, float, float]] = []
    total = len(plots)

    with open_imagery(raster_path) as src:
        for i, (plot_num, row) in enumerate(plots.iterrows()):
            if i % 200 == 0:
                print(f'  [{i:4d}/{total}] processing...')

            geom_4326 = row.geometry
            props = row.to_dict()

            # area sanity check against 7/12 record
            ratio = _area_ratio(props)
            if ratio is not None and not (AREA_RATIO_MIN <= ratio <= AREA_RATIO_MAX):
                results.append({
                    'plot_number': str(plot_num),
                    'status': 'flagged',
                    'confidence': None,
                    'method_note': f'area mismatch ratio={ratio:.2f}',
                    'geometry': geom_4326,
                })
                continue

            try:
                dx_m, dy_m, conf = _cross_correlate_shift(
                    src, geom_4326, max_shift_m=max_shift_m, pad_m=PAD_M,
                )
            except Exception as exc:
                results.append({
                    'plot_number': str(plot_num),
                    'status': 'flagged',
                    'confidence': None,
                    'method_note': f'error: {exc}',
                    'geometry': geom_4326,
                })
                continue

            if conf < CONFIDENCE_FLOOR:
                results.append({
                    'plot_number': str(plot_num),
                    'status': 'flagged',
                    'confidence': None,
                    'method_note': f'low confidence={conf:.3f}',
                    'geometry': geom_4326,
                })
                continue

            # apply shift in UTM, reproject back to WGS84
            geom_utm = gpd.GeoSeries([geom_4326], crs='EPSG:4326').to_crs(utm).iloc[0]
            shifted_utm = translate(geom_utm, xoff=dx_m, yoff=dy_m)
            shifted_4326 = gpd.GeoSeries([shifted_utm], crs=utm).to_crs('EPSG:4326').iloc[0]

            results.append({
                'plot_number': str(plot_num),
                'status': 'corrected',
                'confidence': round(conf, 4),
                'method_note': f'cross-corr dx={dx_m:.1f}m dy={dy_m:.1f}m conf={conf:.3f}',
                'geometry': shifted_4326,
            })

            if conf >= 0.5:
                high_conf_shifts.append((dx_m, dy_m, conf))

    # for dense villages: use the median of high-confidence shifts as a fallback
    # for plots that were too noisy to align individually
    if is_dense and len(high_conf_shifts) >= 10:
        top = sorted(high_conf_shifts, key=lambda x: x[2], reverse=True)
        top = top[:max(10, len(top) // 3)]
        gdx = float(np.median([s[0] for s in top]))
        gdy = float(np.median([s[1] for s in top]))
        gconf = 0.30  # conservative: consensus shifts are less certain than local ones

        print(f'\n  global consensus: dx={gdx:.1f}m dy={gdy:.1f}m'
              f' (from {len(top)} high-confidence plots)')

        upgraded = 0
        for r in results:
            if r['status'] == 'flagged' and 'low confidence' in r.get('method_note', ''):
                g_utm = gpd.GeoSeries([r['geometry']], crs='EPSG:4326').to_crs(utm).iloc[0]
                g_out = gpd.GeoSeries(
                    [translate(g_utm, xoff=gdx, yoff=gdy)], crs=utm,
                ).to_crs('EPSG:4326').iloc[0]
                r.update({
                    'status': 'corrected',
                    'confidence': gconf,
                    'method_note': f'global consensus dx={gdx:.1f}m dy={gdy:.1f}m',
                    'geometry': g_out,
                })
                upgraded += 1
        print(f'  upgraded {upgraded} plots via consensus')

    preds = gpd.GeoDataFrame(results, crs='EPSG:4326').set_index('plot_number', drop=False)

    n_corr = (preds['status'] == 'corrected').sum()
    n_flag = (preds['status'] == 'flagged').sum()
    print(f'\n  result: {n_corr} corrected · {n_flag} flagged')

    out_path = Path(village_dir) / 'predictions.geojson'
    write_predictions(out_path, preds)
    print(f'  written: {out_path}')

    if village.example_truths is not None:
        print()
        print(score(preds, village))


if __name__ == '__main__':
    village_dir = sys.argv[1] if len(sys.argv) > 1 else 'data/34855_vadnerbhairav_chandavad_nashik'
    align_village(village_dir)
