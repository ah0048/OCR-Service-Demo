"""14-digit ID recognition using the existing CNN, with abstention on failure."""
import importlib.util
from datetime import date
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
import torch

EXPECTED_LENGTH = 14
CONSENSUS_MIN_CONFIDENCE = 0.50
CONSENSUS_MIN_VOTES = 3


def _valid_egyptian_id_structure(text):
    if len(text) != EXPECTED_LENGTH or not text.isdigit() or text[0] not in '23':
        return False
    try:
        date((1900 if text[0] == '2' else 2000) + int(text[1:3]),
             int(text[3:5]), int(text[5:7]))
    except ValueError:
        return False
    return True


@lru_cache(maxsize=1)
def _model():
    directory = Path(__file__).resolve().parent.parent / 'arabic_digit_ai'
    spec = importlib.util.spec_from_file_location('id_cnn_architecture', directory / 'model.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    model = module.ArabicDigitCNN()
    model.load_state_dict(torch.load(directory / 'models/arabic_digit_cnn.pt',
                                     map_location='cpu', weights_only=True))
    return model.eval()


def _tensor(crop):
    height, width = crop.shape
    scale = min(26 / width, 26 / height)
    width, height = max(1, round(width * scale)), max(1, round(height * scale))
    glyph = cv2.resize(crop, (width, height), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((32, 32), dtype=np.float32)
    x, y = (32 - width) // 2, (32 - height) // 2
    canvas[y:y + height, x:x + width] = glyph / 255.0
    return torch.from_numpy(canvas).unsqueeze(0)


def _merge_overlapping_boxes(boxes):
    if not boxes:
        return []
    merged = []
    for x, y, width, height in sorted(boxes):
        if merged and x < merged[-1][0] + merged[-1][2]:
            ox, oy, ow, oh = merged[-1]
            right, bottom = max(ox + ow, x + width), max(oy + oh, y + height)
            merged[-1] = (ox, min(oy, y), right - ox, bottom - min(oy, y))
        else:
            merged.append((x, y, width, height))
    return merged


def recognize(image_path, segment, deskew, min_confidence=0.90,
              consensus_min_confidence=CONSENSUS_MIN_CONFIDENCE):
    """Accept only an unambiguous, confident 14-glyph reading; never pad/truncate."""
    # imdecode supports Unicode Windows paths unlike some OpenCV imread builds.
    gray = cv2.imdecode(np.fromfile(image_path, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise ValueError(f'Cannot decode image: {image_path}')
    blurred = cv2.GaussianBlur(gray, (3, 3), 0)
    otsu = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    variants = [('otsu', otsu)]
    for offset in (10, 15, 20):
        binary = cv2.adaptiveThreshold(blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 31, offset)
        binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        variants.append((f'adaptive_{offset}', binary))
    attempts, candidates, consensus_candidates = [], [], []
    for method, binary in variants:
        binary = deskew(binary)
        boxes = segment(binary)
        raw_count = len(boxes)
        if boxes:
            # Estimate scale from the taller glyphs, not tiny background flecks.
            # Arabic zero dots remain considerably taller than these artifacts.
            reference_height = float(np.percentile([b[3] for b in boxes], 75))
            boxes = [b for b in boxes if b[3] >= reference_height * .25]
            boxes = _merge_overlapping_boxes(boxes)
        attempt = {'method': method, 'raw_glyph_count': raw_count, 'glyph_count': len(boxes)}
        attempts.append(attempt)
        if len(boxes) != EXPECTED_LENGTH:
            continue
        batch = torch.stack([_tensor(binary[y:y+h, x:x+w]) for x, y, w, h in boxes])
        with torch.inference_mode():
            probabilities = _model()(batch).softmax(1)
        confidence, digits = probabilities.max(1)
        text = ''.join(str(d) for d in digits.tolist())
        attempt.update(text=text, min_confidence=float(confidence.min()),
                       digit_confidences=confidence.tolist())
        if attempt['min_confidence'] >= min_confidence:
            candidates.append(attempt)
        if attempt['min_confidence'] >= consensus_min_confidence:
            consensus_candidates.append(attempt)
    readings = {candidate['text'] for candidate in candidates}
    early_consensus_votes = {}
    for candidate in consensus_candidates:
        early_consensus_votes[candidate['text']] = early_consensus_votes.get(candidate['text'], 0) + 1
    # A single very confident preprocessing pass can still be the familiar ٠/٥
    # error. If two other usable passes agree on a different reading, expose
    # their value for cross-side matching instead of returning the lone strict
    # result as unanimous. It remains needs_review until the other card side
    # independently confirms it.
    if len(readings) == 1 and early_consensus_votes:
        strict_text = next(iter(readings))
        winner = max(early_consensus_votes, key=early_consensus_votes.get)
        if winner != strict_text and early_consensus_votes[winner] >= 2:
            best = max((c for c in consensus_candidates if c['text'] == winner),
                       key=lambda candidate: candidate['min_confidence'])
            return {'status': 'needs_review', 'text': winner, 'suggested_text': winner,
                    'expected_length': EXPECTED_LENGTH,
                    'reason': 'Two usable methods agree against one strict reading',
                    'method': best['method'], 'min_confidence': best['min_confidence'],
                    'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                    'decision': 'two_method_over_strict_suggestion',
                    'votes': early_consensus_votes,
                    'strict_min_confidence': min_confidence,
                    'consensus_min_confidence': consensus_min_confidence}
    if len(readings) == 1:
        best = max(candidates, key=lambda candidate: candidate['min_confidence'])
        return {'status': 'extracted', 'text': best['text'], 'expected_length': EXPECTED_LENGTH,
                'method': best['method'], 'min_confidence': best['min_confidence'],
                'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                'decision': 'unanimous'}
    valid_readings = {text for text in readings if _valid_egyptian_id_structure(text)}
    if len(valid_readings) == 1:
        winner = next(iter(valid_readings))
        best = max((c for c in candidates if c['text'] == winner),
                   key=lambda candidate: candidate['min_confidence'])
        return {'status': 'extracted', 'text': winner, 'expected_length': EXPECTED_LENGTH,
                'method': best['method'], 'min_confidence': best['min_confidence'],
                'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                'decision': 'valid_egyptian_date'}
    counts = {}
    for candidate in candidates:
        counts[candidate['text']] = counts.get(candidate['text'], 0) + 1
    if counts:
        majority = max(counts, key=counts.get)
        if counts[majority] > len(candidates) / 2:
            # A strict majority of independent preprocessing variants agree on one
            # reading; trust the consensus instead of abstaining on the dissenter.
            best = max((c for c in candidates if c['text'] == majority),
                       key=lambda candidate: candidate['min_confidence'])
            return {'status': 'extracted', 'text': best['text'], 'expected_length': EXPECTED_LENGTH,
                    'method': best['method'], 'min_confidence': best['min_confidence'],
                    'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                    'decision': 'majority', 'votes': counts}
    consensus_votes = {}
    for candidate in consensus_candidates:
        consensus_votes[candidate['text']] = consensus_votes.get(candidate['text'], 0) + 1
    if consensus_votes:
        winner = max(consensus_votes, key=consensus_votes.get)
        unique_winner = list(consensus_votes.values()).count(consensus_votes[winner]) == 1
        if consensus_votes[winner] >= CONSENSUS_MIN_VOTES and unique_winner:
            best = max((c for c in consensus_candidates if c['text'] == winner),
                       key=lambda candidate: candidate['min_confidence'])
            return {'status': 'extracted', 'text': winner, 'expected_length': EXPECTED_LENGTH,
                    'method': best['method'], 'min_confidence': best['min_confidence'],
                    'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                    'decision': 'three_method_consensus', 'votes': consensus_votes,
                    'strict_min_confidence': min_confidence,
                    'consensus_min_confidence': consensus_min_confidence}
        if consensus_votes[winner] >= 2 and unique_winner:
            best = max((c for c in consensus_candidates if c['text'] == winner),
                       key=lambda candidate: candidate['min_confidence'])
            return {'status': 'needs_review', 'text': winner, 'suggested_text': winner,
                    'expected_length': EXPECTED_LENGTH,
                    'reason': 'Two methods agree, but another usable reading conflicts',
                    'method': best['method'], 'min_confidence': best['min_confidence'],
                    'digit_confidences': best['digit_confidences'], 'attempts': attempts,
                    'decision': 'two_method_suggestion', 'votes': consensus_votes,
                    'strict_min_confidence': min_confidence,
                    'consensus_min_confidence': consensus_min_confidence}
    all_readings = {candidate['text'] for candidate in consensus_candidates}
    reason = ('Conflicting 14-digit readings' if len(readings | all_readings) > 1 else
              'No sufficiently confident 14-digit reading')
    return {'status': 'needs_review', 'text': None, 'expected_length': EXPECTED_LENGTH,
            'reason': reason, 'attempts': attempts, 'votes': consensus_votes or counts,
            'strict_min_confidence': min_confidence,
            'consensus_min_confidence': consensus_min_confidence}

