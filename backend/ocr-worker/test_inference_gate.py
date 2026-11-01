"""Check preparation overlap and shared-model serialization without ML packages."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference


class InferenceGateTests(unittest.TestCase):
    def setUp(self):
        # Only PIL exception types are needed: image validation is patched below.
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
        directories = []

        def download(job, directory):
            if job['tag'] == 'b':
                self.assertTrue(model_started.wait(timeout=5))
            directory.mkdir()
            directories.append(directory)
            for side in ('front', 'back'):
                (directory / side).write_text(job['tag'])
            return directory / 'front', directory / 'back'

        def validate(path):
            if path.name == 'back' and path.read_text() == 'b':
                second_prepared.set()

        def document(front, back, **kwargs):
            nonlocal active, peak
            with counter:
                active += 1
                peak = max(peak, active)
            tag = front.read_text()
            if tag == 'a':
                model_started.set()
                self.assertTrue(second_prepared.wait(timeout=5))
            self.assertEqual(back.read_text(), tag)
            time.sleep(.03)
            with counter:
                active -= 1
            return None, {'tag': tag}

        with patch.object(inference, 'IMAGE_API_URL', 'http://unused'), \
             patch.object(inference, 'download_image_pair', side_effect=download), \
             patch.object(inference, 'validate_image', side_effect=validate), \
             patch.object(inference, 'map_report', side_effect=lambda r: inference.OcrOutput('Success', fields=r)), \
             patch.dict(sys.modules, {'pipeline.document': SimpleNamespace(run_document=document)}):
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(inference.run_ocr, {'tag': 'a'})
                second = pool.submit(inference.run_ocr, {'tag': 'b'})
                results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(peak, 1)
        self.assertEqual([r.fields for r in results], [{'tag': 'a'}, {'tag': 'b'}])
        self.assertEqual(len(set(directories)), 2)
        self.assertTrue(all(not directory.parent.exists() for directory in directories))

    def test_model_failure_releases_gate_and_cleans_up(self):
        used_paths = []
        def images(job, directory):
            directory.mkdir()
            used_paths.append(directory)
            return directory / 'front', directory / 'back'
        document = Mock(side_effect=[RuntimeError('model failed'), (None, {'ok': True})])
        with patch.object(inference, 'IMAGE_API_URL', 'http://unused'), \
             patch.object(inference, 'download_image_pair', side_effect=images), \
             patch.object(inference, 'validate_image'), \
             patch.object(inference, 'map_report', return_value=inference.OcrOutput('Success')), \
             patch.dict(sys.modules, {'pipeline.document': SimpleNamespace(run_document=document)}):
            with ThreadPoolExecutor(max_workers=1) as pool:
                first = pool.submit(inference.run_ocr, {})
                with self.assertRaisesRegex(RuntimeError, 'model failed'):
                    first.result(timeout=5)
                second = pool.submit(inference.run_ocr, {})
                self.assertEqual(second.result(timeout=5).outcome, 'Success')
        self.assertTrue(all(not path.parent.exists() for path in used_paths))


if __name__ == '__main__':
    unittest.main()
