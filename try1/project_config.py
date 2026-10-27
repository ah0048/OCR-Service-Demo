"""Canonical paths for the active offline ID-extraction project."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DETECTOR_MODEL = ROOT / 'core/detector/weights/best.pt'
DIGIT_EXTRACTOR = ROOT / 'core/digits/files/digit_extractor/extract_digits.py'
OCR_MODEL = ROOT / 'core/ocr_old/bundle/models/Qari-OCR-merged'
PIPELINE_OUTPUT = ROOT / 'outputs/pipeline'
API_OUTPUT = ROOT / 'outputs/api'
ARCHIVE = Path(r'D:\ansAI\try1_unused_20260927')

BACK_DETECTOR_MODEL = ROOT / 'core/detector/id-back detect/runs_id_back/yolo26_id_back_v2/weights/best.pt'

# Deployed printed-word classifiers live alongside their inference component.
FIELD_CLASSIFIER_DIR = ROOT / 'core/fieldmatch/models'
# Compatibility alias for callers importing the former field-bank location.
FIELD_BANK = FIELD_CLASSIFIER_DIR.parent

# Original marital checkpoint outperformed the augmented experiment on holdout.
FIELD_CLASSIFIER_MODELS = {
    field: FIELD_CLASSIFIER_DIR / field / 'best.pt'
    for field in ('gender', 'religion', 'marital')
}

