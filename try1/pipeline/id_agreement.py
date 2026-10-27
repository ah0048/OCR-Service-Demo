"""Cross-side ID agreement: one 14-digit ID read from both the front and the back.

The card prints the same national ID twice, once on each side. Reading each side
independently throws away the strongest evidence available: if both sides produce
the same 14 digits by different preprocessing, that is two independent readings
of the same printed number, and disagreement is real evidence of a misread rather
than something to average away.

This module compares the two sides and reports one of:

``agreed``      both sides produced the same 14 digits. The value is confirmed and
                no longer needs review.
``reconciled``  the sides differ, but one reading was produced by both sides'
                preprocessing and the difference is small. That reading is the
                printed number surviving two photos, so it is settled and written
                into both sides.
``single``      only one side produced a usable reading. That reading stands, but it
                is unconfirmed because nothing corroborates it.
``conflict``    both sides produced usable readings and they differ. Neither is
                declared correct; both are returned with the disagreeing digit
                positions so a human can look at two crops side by side.
``unreadable``  neither side produced a usable reading.
``invalid_length`` a value that is not exactly 14 digits reached the settled-write
                path; refused rather than returned.

The format rule is exactly 14 ASCII digits, no more and no fewer, and it is
enforced by :func:`is_valid_id` before any value can become a side's ``text`` or
the reconciled ``national_id.text``. A rejected reading is kept as
``rejected_text`` with a ``length_violation`` reason rather than discarded, and
the verdict names the offending side in ``front_rejected`` / ``back_rejected``.

A disagreement is never resolved by confidence alone. Both readings in the two
measured conflicts were ``unanimous`` at 0.999+, so the per-digit confidence the
CNN reports does not discriminate; and the standard ID check digit is useless
here because this dataset's IDs are synthetic (many begin with a birth year of
3081), so a checksum test would reject almost every card. The evidence that does
settle a conflict is a reading that **both** sides independently produced.
"""
from core.fieldmatch.options import normalize_text

#: Fields compared across the two sides.
ID_FIELD = 'id'

#: The national ID is exactly this many digits. No more, no less.
#:
#: This is a hard format rule, not a heuristic: a reading of 13 or 15 digits is
#: not a damaged national ID, it is a segmentation that grabbed or dropped a
#: glyph, so it can never be returned as an ID no matter how confident the digits
#: look. Every candidate is put through :func:`is_valid_id` before it is allowed
#: to become a side's ``text`` or the reconciled ``national_id.text``.
EXPECTED_LENGTH = 14

#: How many digits may differ between the two sides and still count as "similar".
#:
#: A disagreement about one or two digits between two photos of the same printed
#: number is a character-level misread, not two different cards. A larger
#: difference means the two crops are not the same printed number at all, or one
#: side latched onto the wrong region, and no amount of voting should merge them.
MAX_SIMILAR_DIFFERENCES = 2


def is_valid_id(text):
    """True only for a string of exactly 14 ASCII digits.

    The single place the format rule lives. Anything else - 13 or 15 digits, a
    stray letter, Arabic-Indic digits, ``None`` - is not an ID.
    """
    return (isinstance(text, str) and len(text) == EXPECTED_LENGTH
            and text.isascii() and text.isdigit())


def reject_reason(entry):
    """Why this side's reading cannot be an ID, or None if it can.

    The reason is reported rather than swallowed so a reviewer sees the offending
    value and its length instead of a bare ``None``.
    """
    if not isinstance(entry, dict):
        return None
    text = entry.get('text')
    if text is None:
        recognition = entry.get('ocr') or {}
        text = recognition.get('text')
        if text is None:
            return None
    if not isinstance(text, str):
        return f'ID is {type(text).__name__}, not text'
    cleaned = normalize_text(text).replace(' ', '')
    if cleaned.isascii() and cleaned.isdigit() and len(cleaned) != EXPECTED_LENGTH:
        return (f'{len(cleaned)} digits read, but a national ID is exactly '
                f'{EXPECTED_LENGTH} (no more, no less)')
    if not cleaned.isascii():
        return 'digits are not ASCII, so they are not the printed ID'
    if not cleaned.isdigit():
        return 'contains characters that are not digits'
    return None


def usable(entry):
    """Return the 14-digit text of a side's id field, or None.

    A length check is not correctness, so this only filters out readings that
    cannot be an ID at all.
    """
    if not isinstance(entry, dict):
        return None
    text = entry.get('text')
    if not isinstance(text, str):
        return None
    text = normalize_text(text).replace(' ', '')
    return text if is_valid_id(text) else None


def _alternatives(entry):
    """Every 14-digit reading this side's OCR attempts produced, with confidence.

    ``strict_id`` records all four preprocessing variants, including ones it
    rejected for low confidence. A reading rejected on one side but produced on
    the other is exactly the case cross-side comparison can settle, so the
    rejected variants are kept as evidence rather than discarded.
    """
    if not isinstance(entry, dict):
        return {}
    found = {}
    recognition = entry.get('ocr') or {}
    attempts = recognition.get('attempts') or []
    for attempt in attempts:
        text = usable({'text': attempt.get('text')})
        if text is None:
            continue
        confidence = attempt.get('min_confidence')
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        previous = found.get(text)
        if previous is None or confidence > previous[1]:
            found[text] = (attempt.get('method'), confidence)
    primary = usable(entry)
    if primary and primary not in found:
        recognition_confidence = recognition.get('min_confidence')
        found[primary] = (
            recognition.get('method'),
            float(recognition_confidence) if isinstance(recognition_confidence, (int, float))
            else 0.0)
    return found


def _cross_side_votes(front_entry, back_entry):
    """Count raw preprocessing attempts, per reading, across both sides.

    Counted per attempt rather than per distinct reading, so a side that read
    the same value from three of its four variants outranks one that produced it
    once. Variants that did not yield 14 glyphs contribute nothing, since they
    produced no reading at all.
    """
    tally = {}
    total = 0
    for entry in (front_entry, back_entry):
        for attempt in ((entry or {}).get('ocr') or {}).get('attempts') or []:
            text = usable({'text': attempt.get('text')})
            if text is None:
                continue
            tally[text] = tally.get(text, 0) + 1
            total += 1
    return dict(sorted(tally.items(), key=lambda item: (-item[1], item[0]))), total


def _attempt_count(entry, text):
    """Number of preprocessing variants on one side that produced ``text``."""
    return sum(1 for attempt in ((entry or {}).get('ocr') or {}).get('attempts') or []
               if usable({'text': attempt.get('text')}) == text)


def _only_zero_five_differences(front_text, back_text, positions):
    """True when every disagreement is the known Arabic ٠/٥ print ambiguity."""
    return bool(positions) and all(
        {front_text[index], back_text[index]} == {'0', '5'} for index in positions)


def differing_positions(left, right):
    """Indices where two equal-length readings differ."""
    return [index for index in range(min(len(left), len(right)))
            if left[index] != right[index]]


def _cross_side_winner(front_text, back_text, corroborated, tally, differences):
    """Return the reading both sides independently produced, else None.

    The only evidence that settles a disagreement is a value that came out of
    **both** sides' preprocessing. A reading only one side saw is that side's
    own misread repeated; a reading both sides saw is the printed number
    surviving two different photos.

    Rejected when the readings differ in more than
    :data:`MAX_SIMILAR_DIFFERENCES` digits: that is not one number read two ways,
    and no corroboration should merge two different numbers.
    """
    if differences > MAX_SIMILAR_DIFFERENCES:
        return None
    candidates = [text for text in corroborated if text in tally]
    if len(candidates) != 1:
        # No shared reading, or several: ambiguity stays a conflict.
        return None
    winner = candidates[0]
    if winner not in (front_text, back_text):
        # Neither side's own answer is the shared one. That can happen when a side
        # demoted the reading it saw; it is still corroborated, but be explicit.
        return winner
    return winner


def _weak_side_corroboration(strong_text, strong_entry, weak_entry):
    """Corroborate a strong side's settled ID from the weak side's noisy attempts.

    On low-contrast back prints the four preprocessing variants often return
    three different *low-confidence* 14-digit strings, so the weak side abstains
    (``text`` stays None). If any of those readings equals the strong side's
    unanimously settled value, or differs only at up to
    ``MAX_SIMILAR_DIFFERENCES`` positions, the weak side *is* reading the same ID
    — accept the strong value instead of leaving the document as a one-sided
    ``single``.

    A confident dissent (any attempt with ``min_confidence >= 0.90`` that reads
    three or more positions differently) marks a real conflict and fusion is
    refused.
    """
    weak = _alternatives(weak_entry)
    if not weak:
        return None
    strong_entry_is_clean = strong_entry is not None
    if not strong_entry_is_clean:
        return None
    matches = []
    for text, (method, confidence) in weak.items():
        differences = differing_positions(strong_text, text)
        if len(differences) <= MAX_SIMILAR_DIFFERENCES or \
                _only_zero_five_differences(strong_text, text, differences):
            matches.append({
                'text': text, 'method': method,
                'min_confidence': round(confidence, 4),
                'differing_positions': differences,
            })
    if not matches:
        return None
    for text, (method, confidence) in weak.items():
        differences = differing_positions(strong_text, text)
        if confidence >= 0.90 and len(differences) > MAX_SIMILAR_DIFFERENCES:
            # Confident and unrelated: different print region, not noise.
            return None
    return matches


def reconcile(front_entry, back_entry):
    """Compare the two sides' id readings.

    Returns a dict with ``status``, ``text``, ``front_text``, ``back_text`` and,
    for a conflict, ``differing_positions`` plus any reading the other side also
    produced. ``text`` is only set when the value is confirmed by agreement or is
    the single side's only reading; a conflict never sets a winner.

    A reading that is not exactly 14 ASCII digits is not a reading at all: it is
    dropped by :func:`usable` and the reason is reported in ``front_rejected`` or
    ``back_rejected``, so the surviving value can never be a 13- or 15-digit
    string and a dropped glyph is visible instead of silent.
    """
    front_text = usable(front_entry)
    back_text = usable(back_entry)
    report = {
        'status': 'unreadable', 'text': None,
        'front_text': front_text, 'back_text': back_text,
        'front_decision': (front_entry or {}).get('ocr', {}).get('decision'),
        'back_decision': (back_entry or {}).get('ocr', {}).get('decision'),
        'expected_length': EXPECTED_LENGTH,
    }
    for label, entry, kept in (('front_rejected', front_entry, front_text),
                               ('back_rejected', back_entry, back_text)):
        reason = reject_reason(entry) if not kept else None
        if reason:
            report[label] = reason
    if front_text and back_text:
        if front_text == back_text:
            report.update(status='agreed', text=front_text,
                          confirmed_by='front and back read the same 14 digits')
            return report
        positions = differing_positions(front_text, back_text)
        report.update(status='conflict', differing_positions=positions)
        # On the back of these cards the small, low-resolution Arabic ٥ is often
        # reduced to a dot and classified as ٠. The front uses a clearer print.
        # Resolve only this exact ambiguity, only for at most two positions, and
        # only when at least three independent front variants agree. This avoids
        # turning a generic side disagreement into an automatic front-side win.
        front_votes = _attempt_count(front_entry, front_text)
        if (len(positions) <= MAX_SIMILAR_DIFFERENCES
                and _only_zero_five_differences(front_text, back_text, positions)
                and front_votes >= 3):
            report.update(
                status='reconciled', text=front_text,
                confirmed_by=(f'front ID was produced by {front_votes} preprocessing '
                              f'variants; back differs only by the known Arabic ٥/٠ '
                              f'ambiguity at position(s) {positions}'),
                resolution='front_zero_five_typography')
            return report
        # Evidence for a dispute, not a decision: a reading the other side also
        # produced outranks a reading only that side saw.
        front_options = _alternatives(front_entry)
        back_options = _alternatives(back_entry)
        corroborated = sorted(set(front_options) & set(back_options))
        report['corroborated'] = corroborated
        report['front_alternatives'] = {text: {'method': method, 'min_confidence': round(conf, 4)}
                                        for text, (method, conf) in front_options.items()}
        report['back_alternatives'] = {text: {'method': method, 'min_confidence': round(conf, 4)}
                                       for text, (method, conf) in back_options.items()}
        tally, total = _cross_side_votes(front_entry, back_entry)
        report['cross_side_votes'] = tally
        winner = _cross_side_winner(front_text, back_text, corroborated, tally,
                                    len(positions))
        if winner is not None:
            # The two sides disagree, but the same reading came out of both
            # sides' preprocessing. Two independent photos of the same printed
            # number agreeing on 14 digits is real corroboration, not a tiebreak,
            # so the value is settled and written to both sides.
            report.update(status='reconciled', text=winner,
                          confirmed_by=(f'a preprocessing variant on each side produced '
                                        f'{winner}; the sides differ only at position(s) '
                                        f'{positions}'))
            return report
        if total:
            leader, votes = max(tally.items(), key=lambda item: (item[1], item[0] == front_text))
            if votes > 1:
                # A suggestion, never a decision: both sides read different values,
                # so ``text`` stays None and the field keeps needs_review. The count
                # is how many independent preprocessing variants across both sides
                # produced this reading, which is the evidence a reviewer wants.
                report['suggested_text'] = leader
                report['suggested_by'] = (f'{votes} of {total} preprocessing variants '
                                           f'across both sides agree')
                report['suggestion_caveat'] = (
                    'Leading candidate only, not a decision. The count is biased by '
                    'how many variants of each side happened to segment 14 glyphs: on '
                    'a 40-card sample this tally agreed with the front reading in 0 of '
                    '2 conflicts, because the back produced more variants. Compare '
                    'the two crops instead of trusting this number.')
        return report
    # One side may abstain because its own preprocessing variants conflict even
    # though one of those valid 14-digit attempts exactly matches the other
    # side's settled reading. The same number appearing in two independent card
    # photos is stronger evidence than the abstaining side's internal conflict.
    if back_text and not front_text and back_text in _alternatives(front_entry):
        report.update(status='reconciled', text=back_text,
                      confirmed_by=(f'back produced {back_text} as its settled ID and '
                                    'a front preprocessing attempt produced the exact same 14 digits'))
        report['corroborated'] = [back_text]
        return report
    if front_text and not back_text and front_text in _alternatives(back_entry):
        report.update(status='reconciled', text=front_text,
                      confirmed_by=(f'front produced {front_text} as its settled ID and '
                                    'a back preprocessing attempt produced the exact same 14 digits'))
        report['corroborated'] = [front_text]
        return report
    # The "detected once on the back" case: the weak side has no settled ID and
    # its own OCR reads differ, but the strong side's reading is confirmed by
    # at least one of those 14-digit detections standing within the tight
    # similarity window. On low-contrast back prints the crops often fragment or
    # merge exactly one glyph to a dot; that is corrected to the front's reading,
    # which is independently unanimous. A confident contradictory read still
    # abstains, so two printed different IDs never gets fused.
    if (front_text and not back_text) or (back_text and not front_text):
        strong_text = front_text or back_text
        weak_entry = back_entry if front_text else front_entry
        weak_name = 'back' if front_text else 'front'
        matches = _weak_side_corroboration(strong_text,
                                           front_entry if front_text else back_entry,
                                           weak_entry)
        if matches is not None:
            report.update(status='reconciled', text=strong_text,
                          confirmed_by=(
                              f'{weak_name} has no settled ID, but its preprocessing produced '
                              f'{len(matches)} 14-digit detection(s) within '
                              f'{MAX_SIMILAR_DIFFERENCES} digits of the strong-side reading, '
                              'so the two sides are supporting the same printed ID'))
            report['corroborated'] = [strong_text]
            report['matching_weak_candidates'] = matches
            return report
    if front_text or back_text:
        report.update(status='single', text=front_text or back_text,
                      confirmed_by='only one side produced a readable ID')
        return report
    return report


def apply_to_report(report):
    """Write the cross-side verdict onto a :func:`pipeline.document.run_document` report.

    The per-side readings are left untouched, so a human reviewing a conflict can
    still see what each side claimed. Only a settled ID is promoted: an agreed ID
    clears ``needs_review`` on both sides because it is now corroborated, and a
    conflicted ID is marked on both sides so neither side looks authoritative.
    """
    sides = (report or {}).get('sides') or {}
    front_entry = (sides.get('front') or {}).get('fields', {}).get(ID_FIELD)
    back_entry = (sides.get('back') or {}).get('fields', {}).get(ID_FIELD)
    if front_entry is None and back_entry is None:
        return None
    verdict = reconcile(front_entry, back_entry)
    report['national_id'] = verdict
    settled = verdict['status'] in ('agreed', 'reconciled') and verdict['text']
    if settled and not is_valid_id(verdict['text']):
        # Defence in depth: nothing that is not exactly 14 ASCII digits may reach
        # a side's text, whatever produced it.
        settled = False
        verdict.update(status='invalid_length', text=None,
                       confirmed_by=f'rejected: a national ID is exactly '
                                    f'{EXPECTED_LENGTH} digits')
    for entry in (front_entry, back_entry):
        if not isinstance(entry, dict):
            continue
        entry['id_agreement'] = verdict['status']
        if settled:
            # Write the cross-checked value into both sides so downstream consumers
            # reading either side see the same confirmed ID. The original per-side
            # reading is kept as id_text_before_agreement rather than discarded, so a
            # reviewer can still see what each side claimed on its own.
            if entry.get('text') != verdict['text']:
                original = entry.get('text')
                entry['id_text_before_agreement'] = original
                entry['text'] = verdict['text']
                recognition = entry.get('ocr')
                if isinstance(recognition, dict):
                    # The entry text is what the field reported, so it is the value
                    # a reviewer is comparing; mirror it into the OCR block rather
                    # than trusting a possibly absent nested text.
                    recognition['text_before_cross_side_agreement'] = original
                    recognition['text'] = verdict['text']
                    recognition['status_before_cross_side_agreement'] = recognition.get('status')
                    recognition['decision_before_cross_side_agreement'] = recognition.get('decision')
                    recognition['status'] = 'extracted'
                    recognition['decision'] = 'cross_side_agreement'
            entry['status_before_cross_side_agreement'] = entry.get('status')
            entry['status'] = 'extracted'
            entry['has_14_digits'] = True
            entry['needs_review'] = False
        elif verdict['status'] == 'conflict':
            entry['needs_review'] = True
        # Hard format rule, applied last so nothing can bypass it: a value that is
        # not exactly 14 ASCII digits never stays in a side's text. It is moved to
        # ``rejected_text`` with the reason, so the crop can still be judged.
        if not is_valid_id(entry.get('text')):
            raw = entry.get('text')
            if raw is not None:
                entry['rejected_text'] = raw
                entry['length_violation'] = reject_reason({'text': raw})
            entry['text'] = None
            entry['has_14_digits'] = False
            entry['needs_review'] = True
            if entry.get('status') == 'extracted':
                entry['status'] = 'needs_review'
    return verdict

