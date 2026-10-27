# Core inference components

- `detector/`: YOLO deployment weights for front-ID field localization.
- `digits/`: strict 14-digit recognizer, CNN weights and runtime architecture.
- `fieldmatch/`: printed-word classifiers and deployed weights for gender,
  religion and marital status (`models/<field>/best.pt`).
- `ocr_old/`: active Qari 2B Arabic name/address reader and offline model bundle.

Runtime paths are defined in root `project_config.py`. The API imports the
pipeline, and the pipeline calls these components. Unused alternative models
are archived under `D:\ansAI\try1_unused_20260927\models`.
