"""Mock OCR inference. Replace `run_ocr` with the real model; the messaging code (worker.py) does not change.

Contract: run_ocr(job) -> OcrOutput. Must be deterministic only for this mock; a real model need not be.
All data is synthetic, derived from a hash of the job id. No real personal data.
"""
import hashlib
import os
import time
from dataclasses import dataclass, field
from datetime import date

MODEL_VERSION = "mock-ocr-1.0"
PROCESSING_DELAY_SECONDS = float(os.getenv("OCR_PROCESSING_DELAY_SECONDS", "2"))

FIRST_NAMES = ["Ahmed", "Mohamed", "Mahmoud", "Omar", "Youssef", "Fatma", "Mona", "Sara", "Nour", "Hana"]
LAST_NAMES = ["Hassan", "Ibrahim", "Ali", "Saleh", "Mostafa", "Farouk", "Adel", "Nabil", "Samir", "Kamal"]


@dataclass
class OcrOutput:
    outcome: str  # Success | Unreadable | LowConfidence
    confidence: float
    fields: dict[str, str] | None = field(default=None)


def run_ocr(job: dict) -> OcrOutput:
    """Simulate inference. `job` has jobId, clientReference, imageReference (no image bytes)."""
    time.sleep(PROCESSING_DELAY_SECONDS)

    h = hashlib.sha256(job["jobId"].encode()).digest()
    bucket = h[0] % 100
    confidence_unit = h[1] / 255  # 0..1, deterministic

    if bucket >= 92:  # ~8% unreadable
        return OcrOutput("Unreadable", round(0.05 + 0.2 * confidence_unit, 3))

    fields = _mock_fields(h)
    if bucket >= 85:  # ~7% low confidence
        return OcrOutput("LowConfidence", round(0.40 + 0.35 * confidence_unit, 3), fields)
    if bucket >= 78:  # ~7% extracted fine, but the data breaks a business rule
        if h[2] % 2:
            fields["expiryDate"] = "2020-01-01"  # expired ID
        else:
            fields["nationalId"] = fields["nationalId"][:-3]  # wrong length
    return OcrOutput("Success", round(0.86 + 0.13 * confidence_unit, 3), fields)


def _mock_fields(h: bytes) -> dict[str, str]:
    year = 1960 + h[3] % 45
    month = 1 + h[4] % 12
    day = 1 + h[5] % 28
    century = "2" if year < 2000 else "3"
    governorate = f"{1 + h[6] % 27:02d}"
    serial = int.from_bytes(h[7:10], "big") % 100000
    national_id = f"{century}{year % 100:02d}{month:02d}{day:02d}{governorate}{serial:05d}"
    return {
        "nationalId": national_id,
        "fullName": f"{FIRST_NAMES[h[11] % 10]} {LAST_NAMES[h[12] % 10]}",
        "birthDate": date(year, month, day).isoformat(),
        "expiryDate": date(2030 + h[13] % 5, 1 + h[14] % 12, 1 + h[15] % 28).isoformat(),
    }
