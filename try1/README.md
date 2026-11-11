# OCR inference pipeline

This directory contains the offline paired-image pipeline and its model assets
for the standalone Oracle worker. Start the application through
`backend/ocr-worker/oracle_worker.py`, from the repository root.

See [the project guide](../README.md), [model assets](ASSETS.md), and
[CUDA setup notes](CUDA_README.md). Runtime artifacts are retained under
`outputs/oracle_workers/`; local virtual environments and outputs are not included
in transfer archives.
