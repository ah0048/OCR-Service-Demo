"""Prevent concurrent detector calls into the affected MPS shader cache."""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'try1'))
from pipeline import document


class DetectionSchedulingTests(unittest.TestCase):
    def test_mps_serializes_detection_while_cpu_retains_parallel_detection(self):
        for device, expected in (('mps', 1), ('cpu', 2)):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as output:
                active = peak = 0
                lock = threading.Lock()
                def detect(source, **kwargs):
                    nonlocal active, peak
                    with lock:
                        active += 1
                        peak = max(peak, active)
                    time.sleep(0.05)
                    with lock:
                        active -= 1
                    path = Path(kwargs['output']) / 'result.json'
                    path.parent.mkdir(parents=True)
                    return path, {'fields': {'id': {'status': 'extracted', 'text': '29001010123456'}}}
                with patch.object(document, 'run_pipeline', side_effect=detect), \
                     patch.object(document, 'apply_expiry_date'), \
                     patch.object(document, 'apply_back_classifiers', return_value={}):
                    _, report = document.run_document('front', 'back', output=output, device=device, qari=False)
                self.assertEqual(peak, expected)
                self.assertEqual(report['pipeline']['parallel_detection'], device != 'mps')
                self.assertTrue(report['pipeline']['continued'])


if __name__ == '__main__':
    unittest.main()
