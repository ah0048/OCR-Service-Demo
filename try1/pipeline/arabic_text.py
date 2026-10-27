"""Remove Arabic vocalization marks from OCR text without changing letters."""
import re
import unicodedata


_TASHKEEL = re.compile(
    '[\u0610-\u061a\u064b-\u0652\u0656-\u065f\u0670'
    '\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed]'
)


def remove_tashkeel(text: str | None) -> str | None:
    """Strip vowel marks, shadda and sukun; preserve hamza, letters and spacing."""
    if text is None:
        return None
    # Compose decomposed hamza/alef forms before removing vocalization marks.
    return _TASHKEEL.sub('', unicodedata.normalize('NFC', text))
