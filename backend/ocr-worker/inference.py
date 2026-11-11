"""Offline paired-image inference used by the Oracle OCR worker."""
import math
from contextlib import nullcontext
import os
import sys
import tempfile
import threading
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL_ROOT = Path(os.getenv('OCR_PROJECT_ROOT', str(ROOT / 'try1'))).resolve()
DEVICE = os.getenv('OCR_DEVICE', 'cpu')
MAX_IMAGE_BYTES = 20 * 1024 * 1024
_inference_lock = threading.Lock()


@dataclass
class OcrOutput:
    outcome: str
    confidence: float | None = None
    fields: dict[str, str] | None = None
    details: dict | None = None


def initialize():
    """Fail before consuming if assets/device are unavailable; cache models per process."""
    sys.path.insert(0, str(MODEL_ROOT))
    from project_config import DETECTOR_MODEL, BACK_DETECTOR_MODEL, FIELD_CLASSIFIER_MODELS
    from pipeline.front_id import load_detector, load_qari
    from pipeline.document import load_field_classifier
    from core.digits.files.digit_extractor.extract_digits import warm_model
    from project_config import ROOT as project_root
    from core.runtime import resolve_device
    global DEVICE
    DEVICE = resolve_device(DEVICE)
    required = [DETECTOR_MODEL, BACK_DETECTOR_MODEL, *FIELD_CLASSIFIER_MODELS.values(),
                project_root / 'core/digits/files/arabic_digit_ai/models/arabic_digit_cnn.pt']
    missing = [str(p) for p in required if not p.is_file()]
    if missing:
        raise RuntimeError('Missing offline model assets: ' + ', '.join(missing))
    load_detector(DETECTOR_MODEL)
    load_detector(BACK_DETECTOR_MODEL)
    warm_model()
    for checkpoint in FIELD_CLASSIFIER_MODELS.values():
        load_field_classifier(Path(checkpoint).resolve())
    load_qari(None, DEVICE).warm()


def validate_image(path):
    from PIL import Image
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError('Image exceeds 20 MB')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(path) as image:
            if image.format not in ('JPEG', 'PNG', 'WEBP', 'BMP', 'TIFF'):
                raise ValueError('Unsupported image format')
            if image.width * image.height > 25_000_000 or getattr(image, 'n_frames', 1) != 1:
                raise ValueError('Image dimensions/frame count exceed limits')
            image.verify()


def public_report(value):
    """Keep extraction evidence while omitting machine-local model/crop paths."""
    private = {'crop', 'selected_crop', 'ocr_target', 'model', 'model_dir',
               'source', 'result_file', 'output', 'result_path', 'image'}
    if isinstance(value, dict):
        return {k: public_report(v) for k, v in value.items() if k not in private}
    if isinstance(value, list):
        return [public_report(v) for v in value]
    return value



def overall_confidence(report):
    """Mean field evidence score, gated by front/back consistency; not calibrated accuracy."""
    required = {'front': ('id', 'name', 'address'),
                'back': ('id', 'expiredate', 'gender', 'job', 'marital', 'religion')}
    components = {}

    def score(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value) if math.isfinite(value) and 0 <= value <= 1 else None

    for side, names in required.items():
        fields = report.get('sides', {}).get(side, {}).get('fields', {})
        # Spouse is optional; count it only when detected or read.
        if side == 'back' and any(fields.get('hasband', {}).get(k) for k in ('text', 'selected_crop', 'candidates')):
            names = (*names, 'hasband')
        for name in names:
            entry = fields.get(name) or {}
            recognition = entry.get('date_recognition') or entry.get('classifier') or entry.get('ocr') or {}
            recognized = next((s for key in ('min_confidence', 'confidence', 'score')
                               if (s := score(recognition.get(key))) is not None), None)
            candidates = entry.get('candidates') or []
            selected = entry.get('selected_crop')
            # Use the selected crop, never the best score from a different crop.
            candidate = next((c for c in candidates if c.get('crop') == selected), None)
            if candidate is None and not selected and candidates:
                candidate = candidates[0]
            detected = score((candidate or {}).get('confidence'))
            usable = bool(entry.get('text') or entry.get('iso_date')) and entry.get('status') in ('extracted', 'needs_review')
            available = [s for s in (recognized, detected) if s is not None]
            value = min(available) if usable and available else 0.0
            components[f'{side}.{name}'] = {
                'score': round(value, 6), 'recognitionScore': recognized,
                'detectionScore': detected,
                'source': ('recognition_and_detection' if recognized is not None and detected is not None
                           else 'recognition' if recognized is not None
                           else 'detection_proxy' if detected is not None else 'unavailable'),
                'status': entry.get('status', 'missing'), 'hasValue': usable,
            }
    raw = sum(c['score'] for c in components.values()) / len(components)
    agreement = report.get('national_id', {}).get('status')
    passed = agreement in ('agreed', 'reconciled') and report.get('pipeline', {}).get('continued') is not False
    return round(raw if passed else 0.0, 6), {
        'method': 'mean_field_evidence_v1', 'calibrated': False,
        'formula': 'mean of required field scores; each is min(recognition, detection) when both exist; missing fields score 0; unverified/mismatched IDs gate the overall score to 0',
        'rawMean': round(raw, 6), 'idGatePassed': passed,
        'fields': components,
    }



def map_report(report):
    fields = {}
    aliases = {('front', 'name'): 'fullName', ('front', 'address'): 'address',
               ('back', 'expiredate'): 'expiryDate', ('back', 'gender'): 'gender',
               ('back', 'hasband'): 'spouseName', ('back', 'job'): 'occupation',
               ('back', 'marital'): 'maritalStatus', ('back', 'religion'): 'religion'}
    entries = []
    for side, result in report.get('sides', {}).items():
        for name, entry in result.get('fields', {}).items():
            entries.append(entry)
            text = entry.get('iso_date') if name == 'expiredate' else entry.get('text')
            text = text or entry.get('text')
            if text is not None and str(text).strip():
                fields[aliases.get((side, name), f'{side}.{name}')] = str(text).strip()
    agreement = report.get('national_id', {})
    national_id = agreement.get('text')
    if national_id:
        fields['nationalId'] = national_id
        try:
            century = {'2': 1900, '3': 2000}[national_id[0]]
            fields['birthDate'] = date(century + int(national_id[1:3]),
                                       int(national_id[3:5]), int(national_id[5:7])).isoformat()
        except (KeyError, ValueError):
            pass
    # Preserve review flags; detector scores are not document/text accuracy.
    review = (report.get('review_required', False) or report.get('status') != 'complete'
              or agreement.get('status') != 'agreed'
              or any(e.get('needs_review') or e.get('status') != 'extracted' for e in entries))
    outcome = 'LowConfidence' if review else 'Success'
    if not fields:
        outcome = 'Unreadable'
    if report.get('status') == 'id_mismatch' or agreement.get('status') == 'conflict':
        outcome = 'Rejected'
    details = public_report(report)
    confidence, breakdown = overall_confidence(report)
    details['confidenceKind'] = 'heuristic'
    details['confidenceCalculation'] = breakdown
    details['birthDateSource'] = 'nationalId' if 'birthDate' in fields else None
    return OcrOutput(outcome, confidence, fields or None, details)


def _infer_pair(front, back, output, job_id=None):
    from pipeline.document import run_document
    options = {'document_id': job_id} if job_id is not None else {}
    # CUDA/CPU jobs can overlap. YOLO instances and Qari generation retain their
    # own locks; digit/word CNNs are warmed, read-only eval models. Preserve the
    # previously tested whole-document gate on Metal.
    with (_inference_lock if DEVICE == 'mps' else nullcontext()):
        _, report = run_document(front, back, output=output, device=DEVICE, qari_device=DEVICE, **options)
    return map_report(report)


def run_ocr_paths(front, back, output=None, job_id=None):
    """Infer local images; optionally retain API-style reports/crops under output."""
    from PIL import UnidentifiedImageError, Image
    try:
        validate_image(front)
        validate_image(back)
    except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        return OcrOutput('Unreadable', 0.0, details={'errorCode': 'INVALID_IMAGE_PAIR'})
    if output is not None:
        Path(output).mkdir(parents=True, exist_ok=True)
        return _infer_pair(front, back, output, job_id=job_id)
    with tempfile.TemporaryDirectory(prefix='ocr-') as temporary:
        return _infer_pair(front, back, temporary)
