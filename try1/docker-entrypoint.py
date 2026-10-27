"""Validate production assets before starting the API."""
from pathlib import Path
import sys

from project_config import (BACK_DETECTOR_MODEL, DETECTOR_MODEL,
                            FIELD_CLASSIFIER_MODELS, OCR_MODEL)


required = (
    DETECTOR_MODEL,
    BACK_DETECTOR_MODEL,
    OCR_MODEL / 'config.json',
    OCR_MODEL / 'model.safetensors',
    OCR_MODEL / 'tokenizer_config.json',
    OCR_MODEL / 'preprocessor_config.json',
    Path('core/digits/files/digit_extractor/extract_digits.py'),
    Path('core/digits/files/arabic_digit_ai/models/arabic_digit_cnn.pt'),
    *FIELD_CLASSIFIER_MODELS.values(),
)
missing = [str(path) for path in required if not path.is_file()]
if missing:
    print('Cannot start: required production assets are missing:', file=sys.stderr)
    for path in missing:
        print(f'  - {path}', file=sys.stderr)
    print('Copy the private model assets described in ASSETS.md before building.', file=sys.stderr)
    raise SystemExit(78)
