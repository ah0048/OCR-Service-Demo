"""Check local-image preparation overlap and shared-model serialization."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference


class InferenceGateTests(unittest.TestCase):
    def setUp(self):
        image = SimpleNamespace(DecompressionBombError=type('BombError', (Exception,), {}),
                                DecompressionBombWarning=type('BombWarning', (Warning,), {}))
        pil = SimpleNamespace(Image=image, UnidentifiedImageError=type('InvalidImage', (OSError,), {}))
        self.pil_patch = patch.dict(sys.modules, {'PIL': pil})
        self.pil_patch.start()
        self.addCleanup(self.pil_patch.stop)

    def test_next_job_prepares_during_inference_but_model_calls_do_not_overlap(self):
        model_started = threading.Event()
        second_prepared = threading.Event()
        counter = threading.Lock()
        active = peak = 0
        outputs = []

        def validate(path):
            if path.parent.name == 'b':
                self.assertTrue(model_started.wait(timeout=5))
                if path.name == 'back':
                    second_prepared.set()

        def document(front, back, **kwargs):
            nonlocal active, peak
            with counter:
                active += 1
                peak = max(peak, active)
            outputs.append(Path(kwargs['output']))
            tag = front.parent.name
            if tag == 'a':
                model_started.set()
                self.assertTrue(second_prepared.wait(timeout=5))
            self.assertEqual(back.parent.name, tag)
            time.sleep(.03)
            with counter:
                active -= 1
            return None, {'tag': tag}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(inference, 'validate_image', side_effect=validate), \
             patch.object(inference, 'map_report', side_effect=lambda r: inference.OcrOutput('Success', fields=r)), \
             patch.dict(sys.modules, {'pipeline.document': SimpleNamespace(run_document=document)}):
            root = Path(directory)
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(inference.run_ocr_paths, root / 'a/front', root / 'a/back')
                second = pool.submit(inference.run_ocr_paths, root / 'b/front', root / 'b/back')
                results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(peak, 1)
        self.assertEqual([r.fields for r in results], [{'tag': 'a'}, {'tag': 'b'}])
        self.assertEqual(len(set(outputs)), 2)
        self.assertTrue(all(not directory.exists() for directory in outputs))

    def test_model_failure_releases_gate_and_cleans_up(self):
        outputs = []
        outcomes = iter([RuntimeError('model failed'), (None, {'ok': True})])

        def document(front, back, **kwargs):
            outputs.append(Path(kwargs['output']))
            result = next(outcomes)
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(inference, 'validate_image'), \
             patch.object(inference, 'map_report', return_value=inference.OcrOutput('Success')), \
             patch.dict(sys.modules, {'pipeline.document': SimpleNamespace(run_document=document)}):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(inference.run_ocr_paths, Path('front'), Path('back'))
                with self.assertRaisesRegex(RuntimeError, 'model failed'):
                    first.result(timeout=5)
                second = pool.submit(inference.run_ocr_paths, Path('front'), Path('back'))
                self.assertEqual(second.result(timeout=5).outcome, 'Success')
        self.assertTrue(all(not path.exists() for path in outputs))


if __name__ == '__main__':
    unittest.main()
