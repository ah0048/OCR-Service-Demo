"""Read front/back ID images using digits, OCR and printed-word classifiers."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import json
from pathlib import Path
from uuid import uuid4

from pipeline.front_id import apply_text_ocr_groups, load_qari, run_pipeline
from pipeline.id_agreement import apply_to_report as reconcile_id_sides
from project_config import BACK_DETECTOR_MODEL, FIELD_CLASSIFIER_MODELS, PIPELINE_OUTPUT

BACK_FIELDS = ('expiredate', 'gender', 'hasband', 'id', 'job', 'marital', 'religion')
CONSTRAINED_BACK = ('gender', 'religion', 'marital')
BACK_TEXT_FIELDS = ('hasband', 'job')
FRONT_TEXT_FIELDS = ('name', 'address')
TEXT_FIELDS_BY_SIDE = {'front': FRONT_TEXT_FIELDS, 'back': BACK_TEXT_FIELDS}


def apply_expiry_date(fields):
    from core.digits.date_reader import extract_date_details
    entry = fields.setdefault('expiredate', {'status': 'missing', 'text': None, 'candidates': []})
    entry.pop('ocr_target', None)
    entry.update(reader='digit_cnn_date', text=None)
    candidates = list(entry.get('candidates') or [])
    if not candidates and entry.get('selected_crop'):
        candidates = [{'crop': entry['selected_crop']}]
    attempts = []
    for candidate in candidates:
        try:
            result = extract_date_details(candidate['crop'])
        except Exception as exc:
            result = {'status': 'date_error', 'text': None, 'error': f'{type(exc).__name__}: {exc}'}
        attempts.append(dict(result, crop=candidate['crop']))
        if result['status'] == 'extracted':
            entry.update(status='extracted', text=result['text'], arabic_text=result['arabic_text'],
                         iso_date=result['iso_date'], selected_crop=candidate['crop'],
                         date_recognition=result, needs_review=True)
            break
    else:
        entry.update(status='needs_review' if candidates else 'missing', text=None, needs_review=True)
    entry['date_attempts'] = attempts


@lru_cache(maxsize=6)
def load_field_classifier(checkpoint):
    from core.fieldmatch.classifier import PrintedFieldReader
    return PrintedFieldReader(checkpoint)


def apply_back_classifiers(fields, models=None, threshold=0.8):
    """Classify word crops independently; preserve failures and uncertain scores."""
    if not 0 <= threshold <= 1:
        raise ValueError('Classifier threshold must be between 0 and 1')
    models = FIELD_CLASSIFIER_MODELS if models is None else models
    reports = {}
    for name in CONSTRAINED_BACK:
        entry = fields.setdefault(name, {'status': 'missing', 'text': None, 'candidates': []})
        entry.pop('ocr_target', None)
        entry.update(reader='word_classifier', text=None)
        candidates = list(entry.get('candidates') or [])
        if not candidates and entry.get('selected_crop'):
            candidates = [{'crop': entry['selected_crop']}]
        attempts = []
        result = {'field': name, 'status': 'missing', 'text': None}
        if candidates:
            try:
                checkpoint = Path(models[name]).resolve()
                reader = load_field_classifier(checkpoint)
                if reader.field != name:
                    raise ValueError(f'Expected {name} checkpoint, got {reader.field}')
                for candidate in candidates:
                    crop = candidate['crop']
                    try:
                        prediction = dict(reader.predict(crop, threshold=threshold), crop=crop)
                        attempts.append(prediction)
                        if prediction['status'] == 'extracted':
                            result = prediction
                            break
                    except Exception as exc:
                        attempts.append({'crop': crop, 'status': 'classifier_error',
                                         'text': None, 'error': f'{type(exc).__name__}: {exc}'})
                else:
                    usable = [a for a in attempts if 'score' in a]
                    result = max(usable, key=lambda a: a['score']) if usable else attempts[-1]
                result = dict(result, model=str(checkpoint))
            except Exception as exc:
                result = {'field': name, 'status': 'classifier_error', 'text': None,
                          'error': f'{type(exc).__name__}: {exc}'}
        result = dict(result, field=name, attempts=attempts)
        entry.update(status=result['status'], text=result.get('text'), classifier=result,
                     needs_review=result['status'] != 'extracted')
        if result.get('crop'):
            entry['selected_crop'] = result['crop']
        reports[name] = result
    return reports


def run_text_ocr(side_results, qari_device='cpu'):
    """Offer text crops from both sides to Qari's configurable batching API."""
    groups = []
    for side, (_, result) in side_results.items():
        fields = result.get('fields') or {}
        names = TEXT_FIELDS_BY_SIDE.get(side, ())
        groups.append((fields, tuple(name for name in names if fields.get(name))))
    groups = [(fields, names) for fields, names in groups if names]
    if not groups:
        return
    try:
        reader = load_qari(None, qari_device)
        apply_text_ocr_groups(groups, reader)
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
        for fields, names in groups:
            for name in names:
                entry = fields.get(name) or {}
                if entry.get('selected_crop') or entry.get('candidates'):
                    entry.update(status='ocr_error', error=error)
        return error
    return None


def run_document(front, back, output=PIPELINE_OUTPUT, qari=True,
                 device='cpu', qari_device='cpu', field_bank=None,
                 classifier_models=None, classifier_threshold=0.8, document_id=None):
    # field_bank is retained for old callers; the template matcher is replaced.
    tag = f'{int(document_id)}_' if document_id is not None else ''
    directory = Path(output).resolve() / ('document_' + tag + uuid4().hex)
    directory.mkdir(parents=True)
    report = {'sides': {}, 'review_required': True}
    result_path = directory / 'result.json'
    side_results = {}

    def detect_side(side, source):
        options = {} if side == 'front' else {
            'model_path': BACK_DETECTOR_MODEL, 'field_names': BACK_FIELDS,
            'text_fields': BACK_TEXT_FIELDS, 'imgsz': 800}
        # Stage 1 detects/crops all fields but reads only the national ID. Front
        # and back use separate detector models and can therefore run together.
        return run_pipeline(
            source, output=directory / side, qari=False, device=device,
            qari_device=qari_device, **options)

    # PyTorch 2.13 MPS can race while two detectors initialize Metal shader
    # caches concurrently. Keep front/back detection serial on that backend.
    parallel_detection = device != 'mps'
    with ThreadPoolExecutor(max_workers=2 if parallel_detection else 1, thread_name_prefix='id-side') as pool:
        futures = {pool.submit(detect_side, side, source): side
                   for side, source in (('front', front), ('back', back))}
        for future in as_completed(futures):
            side = futures[future]
            try:
                path, result = future.result()
                side_results[side] = (path, result)
                report['sides'][side] = {'result_file': str(path), **result}
            except Exception as exc:
                report['sides'][side] = {'fields': {}, 'status': 'error',
                                         'error': f'{type(exc).__name__}: {exc}'}
    # Keep stable front/back ordering in JSON even though detection is parallel.
    report['sides'] = {side: report['sides'][side]
                       for side in ('front', 'back') if side in report['sides']}
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    # Continue when IDs independently agree or cross-side evidence reconciles them.
    reconcile_id_sides(report)
    ids_match = report.get('national_id', {}).get('status') in ('agreed', 'reconciled')
    if not ids_match:
        reason = ('Front/back ID agreement must be agreed or reconciled '
                  'before the remaining fields are processed')
        report['pipeline'] = {'stage': 'id_validation', 'continued': False,
                              'parallel_detection': parallel_detection, 'reason': reason}
        for side in report['sides'].values():
            for name, entry in (side.get('fields') or {}).items():
                if name != 'id' and isinstance(entry, dict):
                    entry.update(status='skipped_id_mismatch', text=None,
                                 skipped_reason=reason)
        report['status'] = ('id_mismatch' if report.get('national_id', {}).get('status')
                            == 'conflict' else 'id_unverified')
        result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return result_path, report

    report['pipeline'] = {'stage': 'field_processing', 'continued': True,
                          'parallel_detection': parallel_detection, 'parallel_fields': True,
                          'id': report['national_id']['text']}

    def process_back_fields():
        if 'back' not in side_results:
            return None
        _, result = side_results['back']
        apply_expiry_date(result['fields'])
        matches = apply_back_classifiers(result['fields'], classifier_models,
                                         classifier_threshold)
        result['constrained_fields'] = matches
        result['field_classifiers'] = {
            'status': 'ok' if all(m['status'] == 'extracted' for m in matches.values()) else 'partial',
            'threshold': classifier_threshold, 'device': 'cpu'}
        return matches

    # Stage 2 runs independent workloads together after the ID gate passes.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='document-fields') as pool:
        back_future = pool.submit(process_back_fields)
        qari_future = pool.submit(run_text_ocr, side_results, qari_device) if qari else None
        back_future.result()
        error = qari_future.result() if qari_future else None

    for side, (path, result) in side_results.items():
        if qari:
            result['name_address_ocr'] = 'unavailable' if error else 'qari'
            if error:
                result['qari_error'] = error
        else:
            result['name_address_ocr'] = 'disabled'
        Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                              encoding='utf-8')
    # The same national ID is printed on both sides, so judge the two readings
    # together. A value both sides agree on is confirmed; a disagreement is
    # reported as a conflict rather than resolved by picking a side.
    reconcile_id_sides(report)
    # Re-save each side now that cross-side agreement has amended its id entry, so
    # a result file opened directly is not missing the verdict the report carries.
    for side, (path, result) in side_results.items():
        fields = report['sides'][side].get('fields') or {}
        if any(isinstance(entry, dict) and 'id_agreement' in entry
               for entry in fields.values()):
            result['fields'] = fields
            Path(path).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
    report['status'] = 'complete' if all(
        side.get('fields') and all(f['status'] == 'extracted' for f in side['fields'].values())
        for side in report['sides'].values()) else 'partial'
    result_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return result_path, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--front', required=True, type=Path)
    parser.add_argument('--back', required=True, type=Path)
    parser.add_argument('--output', type=Path, default=PIPELINE_OUTPUT)
    parser.add_argument('--device', choices=('cpu', 'cuda', 'mps', 'auto'), default='cpu')
    parser.add_argument('--no-ocr', action='store_true')
    args = parser.parse_args()
    path, report = run_document(args.front, args.back, output=args.output,
                                device=args.device, qari_device=args.device, qari=not args.no_ocr)
    print(f'{report["status"]}: {path}')


if __name__ == '__main__':
    main()

