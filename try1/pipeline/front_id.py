"""Detect front-ID fields, crop them, and run the existing digit extractor."""
import argparse
import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from functools import lru_cache

from PIL import Image, ImageOps

from project_config import ROOT, DETECTOR_MODEL, DIGIT_EXTRACTOR, PIPELINE_OUTPUT
from pipeline.id_agreement import EXPECTED_LENGTH, is_valid_id, reject_reason
from pipeline.arabic_text import remove_tashkeel
DEFAULT_MODEL = DETECTOR_MODEL
DEFAULT_EXTRACTOR = DIGIT_EXTRACTOR
FIELDS = ('id', 'name', 'address')


@lru_cache(maxsize=2)
def load_detector(model_path=DEFAULT_MODEL):
    """Load YOLO weights once and reuse them across requests.

    Loading the detector takes several seconds per request; caching it keeps
    that cost out of response time. Callers must serialize inference (the API
    already does with its per-request lock).
    """
    from ultralytics import YOLO
    return YOLO(str(Path(model_path).resolve()))


@lru_cache(maxsize=2)
def _qari_reader(model_dir, device):
    # model_dir=None lets qari_local auto-select the engine for this process.
    from core.ocr_old.reader import QariOCR
    return QariOCR(model_dir, device=device)


def load_qari(model_dir=None, device='auto'):
    """Cached Qari reader.

    The cache key is built from the values, not from how they were passed, so
    load_qari(device='cpu') and load_qari(None, 'cpu') share one reader.
    """
    return _qari_reader(model_dir, device)


# Kept for callers and tests that manage the cache on load_qari itself.
load_qari.cache_clear = _qari_reader.cache_clear
load_qari.cache_info = _qari_reader.cache_info


def apply_text_ocr(fields, reader, text_fields=None):
    """OCR the text fields, one concurrent pass over all of their crops.

    The highest-confidence candidate of every field goes out in a single batch, so
    the fields are recognised at the same time rather than one after another -
    this is where a document's wall time actually goes (measured: Qari was 44.9s
    of a 45.8s request). Only fields that come back empty fall back to their
    remaining candidates, which are then batched together as well, so a second
    pass still costs one batch rather than one call per field.

    Each field is still independent: one field's failure is recorded on that field
    only, and the first usable recognition (non-empty text) is kept as the selected
    result so the report points to the crop that actually produced it.
    """
    apply_text_ocr_groups([(fields, text_fields)], reader)


def apply_text_ocr_groups(groups, reader):
    """OCR several fields maps in one concurrent pass over all their crops.

    ``groups`` is a sequence of ``(fields, text_fields)``. Passing the front and
    the back together means name, address, job and spouse are recognised at the
    same time instead of one card side after the other, which is the difference
    between three serial OCR calls and one concurrent batch.
    """
    targets = []
    for fields, text_fields in groups:
        names = list(text_fields if text_fields is not None else ('name', 'address'))
        targets.extend((fields, name) for name in names if fields.get(name) is not None)
    # Indexed by position: a fields dict is not hashable, and two sides may carry
    # the same field name.
    pending = [list(_candidate_crops(fields[name])) for fields, name in targets]
    had_candidates = [bool(crops) for crops in pending]
    errors = [None] * len(targets)
    done = [False] * len(targets)
    while True:
        # One crop per unfinished field per pass, so a whole pass is one batch.
        batch = [(index, pending[index][0]) for index in range(len(targets))
                 if not done[index] and pending[index]]
        if not batch:
            break
        recognitions = _extract_many(reader, [crop for _, crop in batch])
        for (index, crop), recognition in zip(batch, recognitions):
            pending[index].pop(0)
            recognition = dict(recognition or {})
            text = remove_tashkeel(recognition.get('text'))
            text = text if text and text.strip() else None
            recognition['text'] = text
            if recognition.get('error'):
                errors[index] = recognition['error']
            elif not text:
                errors[index] = 'Qari returned no usable text'
            else:
                fields, name = targets[index]
                fields[name].update(
                    selected_crop=crop, text=text,
                    status=recognition.get('status') or 'needs_review',
                    ocr=recognition)
                errors[index] = None
                done[index] = True
    for index, (fields, name) in enumerate(targets):
        if done[index] or not had_candidates[index]:
            continue
        fields[name].update(text=None, status='ocr_error',
                            error=errors[index] or 'Qari returned no usable text')


def _candidate_crops(entry):
    """Crops to try for one field, highest-confidence first."""
    candidates = list(entry.get('candidates') or [])
    if not candidates and entry.get('selected_crop'):
        candidates = [{'crop': entry['selected_crop']}]
    return [candidate['crop'] for candidate in candidates]


def _extract_many(reader, crops):
    """OCR crops as one batch, never raising.

    A batch call that fails, or a reader without a working batch API, falls back
    to one call per crop, and each crop's failure is captured as an error result.
    One unreadable crop must not cost the other fields, which is why nothing here
    propagates an exception to the caller.
    """
    batch = getattr(reader, 'extract_many', None)
    if callable(batch):
        try:
            recognitions = list(batch(crops))
        except Exception:
            recognitions = None
        if recognitions is not None and len(recognitions) == len(crops):
            return recognitions
        # A reader that failed, or answered with the wrong number of results,
        # falls through rather than mismatching fields to crops.
    results = []
    for crop in crops:
        try:
            results.append(reader.extract(crop))
        except Exception as exc:
            results.append({'text': None, 'status': 'ocr_error',
                            'error': f'{type(exc).__name__}: {exc}'})
    return results


def load_extractor(path):
    """Reuse the digit reader and its CNN cache across document sides and jobs."""
    path = Path(path).resolve()
    return _load_extractor(path)


@lru_cache(maxsize=2)
def _load_extractor(path):
    if not path.is_file():
        raise FileNotFoundError(f'Digit extractor not found: {path}')
    if path == Path(DEFAULT_EXTRACTOR).resolve():
        # Date recognition imports this same module. A dynamic import here
        # would create a separate strict-reader/model cache on every call.
        from core.digits.files.digit_extractor import extract_digits as module
        return module.extract_id_details
    spec = importlib.util.spec_from_file_location('front_id_digit_extractor', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, 'extract_id_details', module.extract_digits)


def crop_fields(image, detections, output, extract_digits, padding=0, field_names=FIELDS):
    """Save every candidate; send the highest-confidence ID candidate to OCR."""
    output = Path(output)
    crops = output / 'crops'
    crops.mkdir(parents=True, exist_ok=True)
    width, height = image.size
    fields = {}
    for field in field_names:
        candidates = []
        matches = sorted((d for d in detections if d['label'] == field),
                         key=lambda d: d['confidence'], reverse=True)
        for detection in matches:
            x1, y1, x2, y2 = detection['box']
            box = [max(0, min(width, math.floor(x1) - padding)),
                   max(0, min(height, math.floor(y1) - padding)),
                   max(0, min(width, math.ceil(x2) + padding)),
                   max(0, min(height, math.ceil(y2) + padding))]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            path = crops / f'{field}_{len(candidates) + 1}.png'
            image.crop(tuple(box)).save(path)
            candidates.append({'crop': str(path.resolve()), 'box_xyxy': box,
                               'confidence': detection['confidence']})
        entry = {'status': 'missing' if not candidates else 'detected',
                 'candidates': candidates, 'selected_crop': candidates[0]['crop'] if candidates else None,
                 'text': None, 'needs_review': True}
        fields[field] = entry
        if not candidates:
            continue
        if field != 'id':
            entry['status'] = 'pending_ocr'
            entry['ocr_target'] = str(ROOT / 'core/ocr_old')
            continue
        try:
            recognition = extract_digits(entry['selected_crop'])
            if isinstance(recognition, dict):
                entry['ocr'] = recognition
                text = recognition.get('text')
                recognition_status = recognition.get('status')
            else:
                text = recognition
                recognition_status = 'extracted'
            entry['has_14_digits'] = is_valid_id(text)
            entry['text'] = text if entry['has_14_digits'] else None
            if not entry['has_14_digits'] and text:
                # Keep the offending value visible instead of dropping it silently:
                # 13 or 15 digits means a glyph was lost or a neighbour was grabbed,
                # which is what a reviewer needs to see to judge the crop.
                entry['length_violation'] = reject_reason({'text': text})
                entry['rejected_text'] = text
            entry['status'] = ('extracted' if entry['has_14_digits'] and
                               recognition_status == 'extracted' else 'needs_review')
            # A length check is not evidence that the recognized digits are correct.
        except Exception as exc:
            entry['status'] = 'ocr_error'
            entry['error'] = f'{type(exc).__name__}: {exc}'
    return fields


def run_pipeline(source, output=PIPELINE_OUTPUT, model_path=DEFAULT_MODEL,
                 extractor_path=DEFAULT_EXTRACTOR, conf=0.25, imgsz=1280,
                 device=None, padding=0, qari=False, qari_model_dir=None, qari_device='auto',
                 field_names=FIELDS, text_fields=None):
    text_fields = tuple(f for f in field_names if f != 'id') if text_fields is None else tuple(text_fields)
    if not set(text_fields).issubset(set(field_names) - {'id'}):
        raise ValueError('OCR fields must be non-ID fields in the detector vocabulary')
    source, model_path = Path(source).resolve(), Path(model_path).resolve()
    if not source.is_file():
        raise FileNotFoundError(f'Input image not found: {source}')
    if not model_path.is_file():
        raise FileNotFoundError(f'Model not found: {model_path}')
    if not 0 <= conf <= 1 or imgsz < 1 or padding < 0:
        raise ValueError('Require confidence in [0,1], positive imgsz and nonnegative padding')
    extractor = load_extractor(extractor_path)
    with Image.open(source) as raw:
        image = ImageOps.exif_transpose(raw).convert('RGB')
    model = load_detector(model_path)
    names = model.names
    labels = {str(name).strip().lower() for name in names.values()}
    if not set(field_names).issubset(labels):
        raise ValueError(f'Model must contain {field_names}; got {names}')
    result = model.predict(source=image, conf=conf, imgsz=imgsz, device=device,
                           verbose=False)[0]
    detections = []
    for box in result.boxes:
        detections.append({'label': str(names[int(box.cls.item())]).strip().lower(),
                           'confidence': float(box.conf.item()),
                           'box': box.xyxy[0].cpu().tolist()})
    run_dir = Path(output).resolve() / f'{source.stem}_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}'
    run_dir.mkdir(parents=True)
    fields = crop_fields(image, detections, run_dir, extractor, padding, field_names)
    result.save(filename=str(run_dir / 'annotated.jpg'))
    report = {'source': str(source), 'model': str(model_path),
              'digit_extractor': str(Path(extractor_path).resolve()),
              'image_size': list(image.size), 'coordinate_system': 'EXIF-oriented image pixels',
              'settings': {'conf': conf, 'imgsz': imgsz, 'padding': padding},
              'fields': fields, 'name_address_ocr': 'deferred',
              'review_required': True}
    result_path = run_dir / 'result.json'
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    if qari and text_fields:
        report['name_address_ocr'] = 'qari'
        try:
            reader = load_qari(qari_model_dir, qari_device)
            apply_text_ocr(fields, reader, text_fields)
        except Exception as exc:
            report['name_address_ocr'] = 'unavailable'
            report['qari_error'] = f'{type(exc).__name__}: {exc}'
            for field in text_fields:
                if fields[field]['selected_crop']:
                    fields[field].update(status='ocr_error', error=report['qari_error'])
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return result_path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, help='front-ID image')
    parser.add_argument('--output', type=Path, default=PIPELINE_OUTPUT)
    parser.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    parser.add_argument('--digit-extractor', type=Path, default=DEFAULT_EXTRACTOR)
    parser.add_argument('--conf', type=float, default=0.25)
    parser.add_argument('--imgsz', type=int, default=1280)
    parser.add_argument('--device', default=None)
    parser.add_argument('--padding', type=int, default=0, help='extra pixels around each crop')
    parser.add_argument('--qari', action='store_true', help='run offline Qari on name/address crops')
    parser.add_argument('--qari-model-dir', type=Path, default=None)
    parser.add_argument('--qari-device', default='auto', choices=['auto', 'cpu', 'cuda', 'mps'])
    args = parser.parse_args()
    try:
        path, report = run_pipeline(args.image, args.output, args.model,
                                    args.digit_extractor, args.conf, args.imgsz,
                                    args.device, args.padding, args.qari,
                                    args.qari_model_dir, args.qari_device)
    except Exception as exc:
        parser.exit(1, f'Pipeline failed: {exc}\n')
    print(f'Result: {path}')
    for field, entry in report['fields'].items():
        print(f'{field}: {entry["status"]} ({len(entry["candidates"])} crops)')
    print(f'Name/address OCR: {report["name_address_ocr"]}. Review all recognized text before use.')
    if args.qari and any(report['fields'][f]['status'] == 'ocr_error' for f in ('name', 'address')):
        parser.exit(1, 'Qari OCR is incomplete; crops and ID results were preserved in result.json.\n')


if __name__ == '__main__':
    main()

