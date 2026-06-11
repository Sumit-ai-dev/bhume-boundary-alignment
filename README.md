# BhuMe Boundary Alignment Submission

This repository contains the Python solution to the BhuMe boundary alignment task. The goal is to correct the positions of digitised official land-plot boundaries using satellite imagery and field edge rasters.

## Approach

The alignment algorithm works as follows for each land plot:

1. **Area Check**: Compares the drawn polygon area with the legal 7/12 land record area. Plots with a severe area mismatch (less than 50% or more than 200% of recorded area) are flagged and kept at their official position.
2. **Patch Extraction**: Clips the `boundaries.tif` raster around the plot coordinates with a pad of 80 meters.
3. **Template Rasterisation**: Converts the official boundary polygon outline into a thin template mask.
4. **Edge Detection**: Applies a Sobel filter to detect field boundaries in the satellite patch.
5. **Cross-Correlation**: Slides the template mask over the edge image using FFT convolution to find the translation offset (dx, dy) that maximizes overlap.
6. **Confidence Estimation**: Calculates confidence based on the sharpness of the cross-correlation peak. A sharp peak represents a high-confidence match, while a flat or ambiguous peak flags the plot for manual review.
7. **Shift Reprojection**: Converts the UTM shift back to WGS84 and applies the transformation.

### Village-Specific Strategies
The script auto-detects the village type based on the median plot size:
- **Open Villages (e.g. Vadnerbhairav)**: Large plots are aligned using local cross-correlation up to a maximum shift of 120m.
- **Dense Villages (e.g. Malatavadi)**: Small plots are aligned using local cross-correlation with a lower shift limit of 20m. Plots that fail local alignment are shifted using a global consensus offset calculated from the highest-confidence neighbors.

## Evaluation Results

Scores achieved on the sample ground truths:

### Vadnerbhairav
- Median IoU: **0.852** (Official Start: 0.612)
- Centroid Error: 9.9m
- Accurate Plots (IoU >= 0.5): 66.7%
- Calibration AUC: **0.875**

### Malatavadi
- Median IoU: **0.544** (Official Start: 0.510)
- Centroid Error: 4.1m
- Accurate Plots (IoU >= 0.5): 66.7%
- Calibration AUC: **0.750**

## Setup and Running

Install the required packages:
```bash
pip install geopandas rasterio shapely numpy scipy pillow pyproj
```

Run the alignment script on a village folder:
```bash
python solve.py data/34855_vadnerbhairav_chandavad_nashik
python solve.py data/12429_malatavadi_chandgad_kolhapur
```

The output prediction files will be written to `data/<village_slug>/predictions.geojson`.

## Project Structure
- `solve.py` - Main alignment pipeline
- `bhume/` - Starter kit helper library
- `data/` - Input and generated prediction files
- `transcripts/` - Development log transcripts
