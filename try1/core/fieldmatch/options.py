"""Closed vocabularies for the fixed-choice fields on the back of an ID card.

The card prints a small fixed set of words for gender, religion and marital
status. Reading them with the general VLM OCR produces near-misses such as
"ذكك" for "ذكر", so these fields are matched as images against a bank of
reference word images and may only ever resolve to one of the strings below.

The output strings are exactly the spellings requested by the application
owner: no hamza on ``انثى``/``انسة`` and alef maqsura in ``مسيحى``. The card
prints ``أنثى`` and ``مسيحي``; :func:`normalize_text` folds those variants so a
printed form and a returned form still compare equal.
"""

GENDER = ('ذكر', 'انثى')
RELIGION = ('مسلم', 'مسلمة', 'مسيحى', 'مسيحية')
MARITAL = ('أعزب', 'عزباء', 'متزوج', 'متزوجة', 'مطلق', 'مطلقة',
           'أرمل', 'أرملة', 'انسة')

#: field name as used by the back detector -> allowed values, in printed order.
CONSTRAINED_FIELDS = {
    'gender': GENDER,
    'religion': RELIGION,
    'marital': MARITAL,
}

#: Lowercase detector label -> field name, for checkpoints that spell labels
#: with capitals ('Gender', 'Religion', 'Marital').
LABEL_TO_FIELD = {
    'gender': 'gender',
    'sex': 'gender',
    'religion': 'religion',
    'marital': 'marital',
    'maritalstatus': 'marital',
    'marital_status': 'marital',
}


def options_for(field):
    """Return the allowed values for a field, or raise for an unknown field."""
    key = LABEL_TO_FIELD.get(str(field).strip().lower())
    if key is None:
        raise KeyError(f'No constrained vocabulary for field {field!r}; '
                       f'known fields: {sorted(CONSTRAINED_FIELDS)}')
    return CONSTRAINED_FIELDS[key]


def is_constrained(field):
    """True when a field has a closed vocabulary and must not use free OCR."""
    return LABEL_TO_FIELD.get(str(field).strip().lower()) in CONSTRAINED_FIELDS


def canonical_field(field):
    """Map a detector label to its field name, or None when not constrained."""
    return LABEL_TO_FIELD.get(str(field).strip().lower())


# Arabic orthographic variants that must not create a different class.
_LETTER_FOLD = {
    '\u0623': '\u0627',  # alef with hamza above -> alef
    '\u0625': '\u0627',  # alef with hamza below -> alef
    '\u0671': '\u0627',  # alef wasla -> alef
    '\u0649': '\u064A',  # alef maqsura -> yeh
    '\u0629': '\u0647',  # teh marbuta -> hah
    '\u06A9': '\u0643',  # keheh -> kaf
    '\u06CC': '\u064A',  # farsi yeh -> yeh
    '\u0640': '',        # tatweel
}
# Harakat, sukun, shadda, dagger alef and Quranic annotation marks carry no
# class information for these words; OCR both with and without them.
_DIACRITICS = set('\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652\u0653\u0654'
                  '\u0655\u0656\u0657\u0658\u0670\u06D6\u06D7\u06D8\u06D9'
                  '\u06DA\u06DB\u06DC\u06DD\u06DE\u06DF')


def normalize_text(text):
    """Fold Arabic spelling variants so OCR output compares equal to a choice.

    Strips diacritics and tatweel, unifies alef/yeh/teh-marbuta forms and
    removes everything that is not a letter, digit or space. Empty input and
    input with no letters at all return '' so callers can reject it.
    """
    if not text:
        return ''
    kept = []
    for character in str(text):
        if character in _DIACRITICS:
            continue
        replacement = _LETTER_FOLD.get(character, character)
        if replacement.isalnum() or replacement == ' ':
            kept.append(replacement)
    return ' '.join(''.join(kept).split())


def similarity_ratio(left, right):
    """Character similarity of two Arabic strings in [0, 1], variant-insensitive.

    Uses difflib rather than an extra dependency. Not an OCR confidence: it
    only ranks how close a reading is to an allowed value.
    """
    from difflib import SequenceMatcher
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def best_option(text, options, minimum=0.6):
    """Return ``(option, score)`` for the closest allowed value, else ``(None, score)``.

    ``minimum`` guards against forcing a reading onto the nearest option when
    it matches nothing; a low score returns None so the caller can abstain
    instead of inventing a class.
    """
    normalized = normalize_text(text)
    if not normalized:
        return None, 0.0
    scored = sorted(((similarity_ratio(normalized, option), option)
                     for option in options), reverse=True)
    score, option = scored[0]
    return (option, score) if score >= minimum else (None, score)
