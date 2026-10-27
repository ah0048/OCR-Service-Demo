"""Read slash-separated printed dates with the existing ten-digit CNN.

Slash geometry is handled separately; ID weights and the 14-digit rule are
unchanged. Accept only agreeing, confident, calendar-valid date readings.
"""
from datetime import date

import cv2
import numpy as np
import torch

from core.digits.files.digit_extractor import extract_digits as digits


def normalize_date(groups):
    """Convert visual YYYY/M/D or D/M/YYYY groups to validated DD/MM/YYYY."""
    if len(groups) != 3 or not all(g.isascii() and g.isdigit() for g in groups):
        raise ValueError('Expected three ASCII digit groups')
    if len(groups[0]) == 4 and all(1 <= len(g) <= 2 for g in groups[1:]):
        year, month, day = map(int, groups)
    elif len(groups[2]) == 4 and all(1 <= len(g) <= 2 for g in groups[:2]):
        day, month, year = map(int, groups)
    else:
        raise ValueError('Expected a four-digit year and one/two-digit day and month')
    value = date(year, month, day)
    text = f'{day:02d}/{month:02d}/{year:04d}'
    return {'text': text, 'arabic_text': text.translate(str.maketrans('0123456789', '٠١٢٣٤٥٦٧٨٩')),
            'iso_date': value.isoformat(), 'format': 'DD/MM/YYYY'}


def is_slash(crop):
    """Recognize a tall straight rising stroke, unlike Arabic one or curved digits."""
    y, x = np.where(crop > 0)
    if len(x) < 8 or np.ptp(y) < 6:
        return False
    height, width = crop.shape
    if not .08 <= width / height <= .8:
        return False
    slope, intercept = np.polyfit(y, x, 1)
    residual = np.sqrt(np.mean((x - slope * y - intercept) ** 2)) / height
    correlation = np.corrcoef(y, x)[0, 1] if np.std(x) > 0 else 0
    return bool(-1.5 <= slope <= -.12 and correlation < -.75 and residual < .08)


SINGLE_VALID_MIN_CONFIDENCE = .98


def select_reading(attempts):
    usable = [a for a in attempts if a.get('text')]
    readings = {a['text'] for a in usable}
    if len(readings) == 1 and len({a['method'] for a in usable}) >= 2:
        best = max(usable, key=lambda a: a['min_confidence'])
        return {**normalize_date(best['text'].split('/')), 'status': 'extracted',
                'min_confidence': best['min_confidence'], 'method': best['method'],
                'decision': 'agreeing_valid_dates',
                'reader': 'digit_cnn_date', 'attempts': attempts}
    if len(usable) == 1 and usable[0]['min_confidence'] >= SINGLE_VALID_MIN_CONFIDENCE:
        best = usable[0]
        return {**normalize_date(best['text'].split('/')), 'status': 'extracted',
                'min_confidence': best['min_confidence'], 'method': best['method'],
                'decision': 'single_high_confidence_valid_date',
                'single_valid_min_confidence': SINGLE_VALID_MIN_CONFIDENCE,
                'reader': 'digit_cnn_date', 'attempts': attempts}
    return {'status': 'needs_review', 'text': None, 'reader': 'digit_cnn_date',
            'format': 'DD/MM/YYYY', 'attempts': attempts,
            'reason': 'Conflicting date readings' if len(readings) > 1 else
                      'Need agreement or one calendar-valid reading with confidence of at least 0.98'}


def extract_date_details(image_path, min_confidence=.90):
    gray = cv2.imdecode(np.fromfile(str(image_path), dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
    if gray is None:
        raise ValueError(f'Cannot decode date image: {image_path}')
    # Work at a useful glyph scale even for a small detector crop.
    scale = min(4., 112. / gray.shape[0])
    gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    otsu_value, otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    variants = [('otsu', otsu)]
    for offset in (-20, -10, 10):
        variants.append((f'otsu_offset_{offset}', cv2.threshold(
            gray, max(1, min(254, otsu_value + offset)), 255, cv2.THRESH_BINARY_INV)[1]))
    variants.append(('adaptive', cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                       cv2.THRESH_BINARY_INV, 31, 12)))
    attempts = []
    for method, binary in variants:
        boxes = digits._segment_glyphs(binary)
        if len(boxes) > 150:
            attempts.append({'method': method, 'reason': 'Too many components; crop the date more tightly'})
            continue
        glyphs = [binary[y:y+h, x:x+w] for x, y, w, h in boxes]
        separators = [is_slash(g) for g in glyphs]
        windows = []
        # A date has 6..8 digits plus two separators. Extra surrounding text
        # is permitted only outside a complete, structurally valid date run.
        for start in range(len(boxes)):
            for count in (8, 9, 10):
                end = start + count
                if end > len(boxes):
                    continue
                positions = [i for i, flag in enumerate(separators[start:end]) if flag]
                if len(positions) != 2:
                    continue
                a, b = positions
                lengths = (a, b-a-1, count-b-1)
                if not ((lengths[0] == 4 and all(1 <= n <= 2 for n in lengths[1:])) or
                        (lengths[2] == 4 and all(1 <= n <= 2 for n in lengths[:2]))):
                    continue
                run = boxes[start:end]
                reference = float(np.median([r[3] for r in run]))
                centers = [y+h/2 for _, y, _, h in run]
                if max(centers)-min(centers) > reference * .60:
                    continue
                if any(run[i+1][0] - (r[0]+r[2]) > reference * 1.5 for i, r in enumerate(run[:-1])):
                    continue
                # Do not truncate a two-digit day or start halfway through a
                # year to manufacture a different valid date. Neighbouring
                # components on the same line belong to the same token run.
                middle = float(np.median(centers))
                partial = False
                for neighbour in (start-1, end):
                    if not 0 <= neighbour < len(boxes):
                        continue
                    x, y, w, h = boxes[neighbour]
                    gap = run[0][0] - (x+w) if neighbour < start else x - (run[-1][0]+run[-1][2])
                    if gap < reference * 1.5 and abs(y+h/2-middle) < reference * .55 and h >= reference * .25:
                        partial = True
                if partial:
                    continue
                windows.append((start, end))
        found = False
        for start, end in windows:
            indices = [i for i in range(start, end) if not separators[i]]
            batch = torch.stack([digits._strict_module()._tensor(glyphs[i]) for i in indices])
            with torch.inference_mode():
                probabilities = digits._strict_module()._model()(batch).softmax(1)
            confidence, labels = probabilities.max(1)
            values = dict(zip(indices, map(str, labels.tolist())))
            raw = ''.join('/' if separators[i] else values[i] for i in range(start, end))
            record = {'method': method, 'raw_text': raw, 'min_confidence': float(confidence.min())}
            if record['min_confidence'] < min_confidence:
                record['reason'] = 'Low digit confidence'
            else:
                try:
                    record.update(normalize_date(raw.split('/')))
                except ValueError:
                    record['reason'] = 'Invalid calendar date or date format'
            attempts.append(record)
            found = True
        if not found:
            attempts.append({'method': method, 'reason': 'No date with two slash separators',
                             'glyph_count': len(boxes), 'slash_count': sum(separators)})
    return select_reading(attempts)

