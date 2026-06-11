# AI Development Transcripts

This directory contains information about the AI coding sessions used to develop the boundary alignment solution, as required by the BhuMe submission contract.

## Sessions

### Session 1: Initial Exploration and Algorithm Design
* **Date**: June 11, 2026
* **Description**: Focused on parsing the starter kit files, researching existing tools, and building the core FFT cross-correlation script in Python.

### Session 2: Optimization and Village Strategies
* **Date**: June 11, 2026
* **Description**: Iterated on the local alignment boundaries and implemented the global consensus fallback mechanism to handle noisy predictions in small-plot villages like Malatavadi.

## Dev Log Notes
- Explored using existing map alignment repositories, but decided on a custom FFT cross-correlation approach for better execution control and confidence calibration.
- Formulated the confidence metric based on peak prominence to ensure the AUC scores match the alignment accuracy.
