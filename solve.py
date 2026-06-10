#!/usr/bin/env python3
"""
BhuMe boundary alignment solver.

For each plot in a village:
  1. Extract the satellite image patch around the plot
  2. Use cross-correlation to find the best (dx, dy) shift
  3. Apply the shift to the geometry if confidence is high enough
  4. Flag the plot if confidence is too low or area mismatch is severe

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
from scipy.signal import fftconvolve
from shapely.affinity import translate
from shapely.geometry import mapping

import geopandas as gpd
from pyproj import Transformer

from bhume import load, write_predictions, score
from bhume.geo import open_imagery, geom_to_imagery_crs


# Parameters
PAD_M = 80  # Padding around the plot in meters

# Correlation peak threshold to trust the shift
CORR_PEAK_THRESHOLD = 3.5

# Flag plots where drawn area vs recorded area is outside this range
AREA_RATIO_MIN = 0.5
AREA_RATIO_MAX = 2.0

# Maximum plausible shifts
MAX_SHIFT_OPEN_M = 120
MAX_SHIFT_DENSE_M = 20

CONFIDENCE_FLOOR = 0.25
DENSE_THRESHOLD_SQM = 3000


def _utm_crs(geom) -> str:
    """Pick UTM zone for a geometry."""
    lon = geom.centroid.x
    zone = int((lon + 180) // 6) + 1
    return f"EPSG:{32600 + zone}"


def _area_ratio(plot_row) -> float | None:
    """
    Compare drawn map area vs recorded 7/12 total area.
    Returns ratio (drawn / recorded_total) or None if no record.
    """
    map_area = plot_row.get("map_area_sqm")
    recorded = plot_row.get("recorded_area_sqm")
    pot = plot_row.get("pot_kharaba_ha")

    if not map_area or map_area <= 0:
        return None
    if not recorded or recorded <= 0:
        return None

    total_recorded = recorded
    if pot and pot > 0:
        total_recorded += pot * 10_000  # ha → sqm

    return map_area / total_recorded


def _cross_correlate_shift(
    src: rasterio.DatasetReader,
    geom_4326,
    max_shift_m: float = MAX_SHIFT_OPEN_M,
    pad_m: float = PAD_M,
) -> tuple[float, float, float]:
    """
    Find (dx_m, dy_m, confidence) by cross-correlating the rasterised
    plot mask against the boundaries.tif patch.

    Returns shift in metres (UTM space) and a 0-1 confidence score.
    """
    geom_img = geom_to_imagery_crs(src, geom_4326)
    minx, miny, maxx, maxy = geom_img.bounds
    left   = minx - pad_m
    bottom = miny - pad_m
    right  = maxx + pad_m
    top    = maxy + pad_m

    # clip to dataset extent
    dl, db, dr, dt = src.bounds
    left   = max(left, dl)
    bottom = max(bottom, db)
    right  = min(right, dr)
    top    = min(top, dt)

    if right <= left or top <= bottom:
        return 0.0, 0.0, 0.0

    from rasterio.windows import from_bounds as win_from_bounds
    window = win_from_bounds(left, bottom, right, top, transform=src.transform)
    data = src.read(window=window)

    if data.size == 0:
        return 0.0, 0.0, 0.0

    # Grayscale
    if data.shape[0] >= 3:
        gray = (0.299 * data[0] + 0.587 * data[1] + 0.114 * data[2]).astype(np.float32)
    else:
        gray = data[0].astype(np.float32)

    H, W = gray.shape
    if H < 5 or W < 5:
        return 0.0, 0.0, 0.0

    patch_transform = src.window_transform(window)
    pixel_size_x = abs(patch_transform.a)  # metres/pixel
    pixel_size_y = abs(patch_transform.e)

    # Rasterise the plot boundary ring as template
    mask_transform = from_bounds(left, bottom, right, top, W, H)
    mask = rasterize(
        [(mapping(geom_img), 1)],
        out_shape=(H, W),
        transform=mask_transform,
        fill=0,
        dtype=np.float32,
    )

    from scipy.ndimage import binary_erosion
    eroded = binary_erosion(mask.astype(bool)).astype(np.float32)
    template = mask - eroded  # thin boundary ring

    if template.sum() == 0:
        return 0.0, 0.0, 0.0

    # Edge-detect the raster patch (Sobel)
    from scipy.ndimage import sobel
    sx = sobel(gray, axis=1)
    sy = sobel(gray, axis=0)
    edges = np.hypot(sx, sy).astype(np.float32)
    if edges.max() > 0:
        edges /= edges.max()

    # Cross-correlate
    corr = fftconvolve(edges, template[::-1, ::-1], mode="same")

    peak_idx = np.unravel_index(np.argmax(corr), corr.shape)
    peak_val = corr[peak_idx]
    mean_val = corr.mean()

    if mean_val <= 0:
        return 0.0, 0.0, 0.0

    peak_ratio = peak_val / mean_val
    confidence = min(1.0, max(0.0, (peak_ratio - 1.0) / (CORR_PEAK_THRESHOLD * 3)))

    centre_row = H // 2
    centre_col = W // 2
    dy_px = peak_idx[0] - centre_row
    dx_px = peak_idx[1] - centre_col

    dx_m = dx_px * pixel_size_x
    dy_m = -dy_px * pixel_size_y  # flip y for geographic coords

    # Reject implausibly large shifts
    shift_magnitude = np.sqrt(dx_m**2 + dy_m**2)
    if shift_magnitude > max_shift_m:
        return 0.0, 0.0, confidence * 0.3

    return dx_m, dy_m, confidence


def align_village(village_dir: str) -> None:
    """Load village, align each plot, write predictions.geojson."""

    village = load(village_dir)
    plots = village.plots
    utm = _utm_crs(plots.geometry.iloc[0])

    # Detect village type using proper UTM area
    plots_utm = plots.to_crs(utm)
    median_area = float(plots_utm.geometry.area.median())
    is_dense = median_area < DENSE_THRESHOLD_SQM
    max_shift_m = MAX_SHIFT_DENSE_M if is_dense else MAX_SHIFT_OPEN_M

    print(f"\n{'='*60}")
    print(f"Processing: {village.slug}")
    print(f"  Plots: {len(plots)}")
    print(f"  Median plot area: {median_area:.0f} m²")
    print(f"  Village type: {'DENSE (small plots)' if is_dense else 'OPEN (large plots)'}")
    print(f"  Max shift allowed: {max_shift_m}m")
    raster_path = village.boundaries_path or village.imagery_path
    print(f"  Raster: {'boundaries.tif' if village.boundaries_path else 'imagery.tif'}")
    print(f"{'='*60}\n")

    results = []
    high_conf_shifts = []  # collect for global consensus
    total = len(plots)

    with open_imagery(raster_path) as src:
        for i, (plot_num, row) in enumerate(plots.iterrows()):
            if i % 200 == 0:
                print(f"  [{i:4d}/{total}] processing...")

            geom_4326 = row.geometry
            props = row.to_dict()

            # Check area matches holding record
            ratio = _area_ratio(props)
            if ratio is not None and (ratio < AREA_RATIO_MIN or ratio > AREA_RATIO_MAX):
                results.append({
                    "plot_number": str(plot_num),
                    "status": "flagged",
                    "confidence": None,
                    "method_note": f"area mismatch ratio={ratio:.2f}",
                    "geometry": geom_4326,
                })
                continue

            # FFT cross-correlation alignment
            try:
                dx_m, dy_m, conf = _cross_correlate_shift(
                    src, geom_4326,
                    max_shift_m=max_shift_m,
                    pad_m=PAD_M,
                )
            except Exception as e:
                results.append({
                    "plot_number": str(plot_num),
                    "status": "flagged",
                    "confidence": None,
                    "method_note": f"error: {e}",
                    "geometry": geom_4326,
                })
                continue

            if conf < CONFIDENCE_FLOOR:
                results.append({
                    "plot_number": str(plot_num),
                    "status": "flagged",
                    "confidence": None,
                    "method_note": f"low confidence={conf:.3f}",
                    "geometry": geom_4326,
                })
                continue

            # Apply shift
            geom_utm = gpd.GeoSeries([geom_4326], crs="EPSG:4326").to_crs(utm).iloc[0]
            geom_shifted_utm = translate(geom_utm, xoff=dx_m, yoff=dy_m)
            geom_shifted_4326 = (
                gpd.GeoSeries([geom_shifted_utm], crs=utm).to_crs("EPSG:4326").iloc[0]
            )

            results.append({
                "plot_number": str(plot_num),
                "status": "corrected",
                "confidence": round(conf, 4),
                "method_note": f"cross-corr dx={dx_m:.1f}m dy={dy_m:.1f}m conf={conf:.3f}",
                "geometry": geom_shifted_4326,
            })

            if conf >= 0.5:
                high_conf_shifts.append((dx_m, dy_m, conf))

    # Save results to predictions.geojson
    preds = gpd.GeoDataFrame(results, crs="EPSG:4326")
    preds = preds.set_index("plot_number", drop=False)
    out_path = Path(village_dir) / "predictions.geojson"
    write_predictions(out_path, preds)
    if village.example_truths is not None:
        print(score(preds, village))

if __name__ == "__main__":
    village_dir = sys.argv[1] if len(sys.argv) > 1 else "data/34855_vadnerbhairav_chandavad_nashik"
    align_village(village_dir)