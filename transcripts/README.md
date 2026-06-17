# AI Development Transcripts

This directory contains the AI coding session logs used to develop the boundary alignment solution, as required by the BhuMe submission contract.

## Session Log

### Session 1 — June 11, 2026

**File**: [transcript.md](transcript.md)

A single extended session covering the full development arc, from first reading the BhuMe problem specification through to a working submission. Key decision points captured in the transcript:

- Read the task rubric carefully before writing any code. Understood that confidence calibration (AUC) was weighted most heavily, which drove the design toward classical cross-correlation rather than deep learning.
- Investigated existing open-source alignment repositories (`Lydorn/mapalignment`, SAM, U-Net variants). Rejected all of them: the deep-learning options required training data we didn't have, produced no natural confidence signal, and wouldn't install cleanly on Mac ARM.
- Settled on FFT cross-correlation after concluding it gave interpretable confidence scores from first principles (peak sharpness), required no training data, and produced results the reviewer could follow in a 5-minute video.
- Iterated on the search window: the first attempt used a fixed 80 m pad for all villages; this worked well for Vadnerbhairav but produced poor results for Malatavadi where plots are ~872 m² median. Switched to adaptive max-shift thresholds based on the village's median plot area.
- Added global consensus fallback for dense villages: when many individual plots align confidently, compute a median shift from the top-confidence subset and apply it to remaining noisy plots.

## Note on Transcript Format

The session transcript is saved as a full prompt ↔ response log in Markdown format, per the BhuMe submission guidelines for IDE-based coding assistants. All commands shown in the transcript as "suggested" or "expected" were run locally by the developer — the AI assistant cannot execute shell commands or move files directly.
