"""Exercise persistent stage threads with concurrent documents, without models."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'try1'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import document
import inference


class DocumentPoolTests(unittest.TestCase):
    def test_inference_forwards_owned_pools_and_releases_them_on_failure(self):
        with patch.object(document, 'run_document', return_value=(None, {})) as run, \
             patch.object(inference, 'map_report'), patch.object(inference, 'DEVICE', 'cuda'):
            with self.assertRaisesRegex(ValueError, 'stop worker'):
                with inference.document_thread_pools(2):
                    pools = inference._document_pools
                    inference._infer_pair('front', 'back', 'output', 123)
                    self.assertIs(run.call_args.kwargs['pools'], pools)
                    with self.assertRaisesRegex(RuntimeError, 'already running'):
                        with inference.document_thread_pools(2):
                            pass
                    raise ValueError('stop worker')
            self.assertIsNone(inference._document_pools)
            with self.assertRaises(RuntimeError):
                pools.fields.submit(lambda: None)

    def test_repeated_concurrent_documents_reuse_threads_and_keep_reports_separate(self):
        detection_barrier = threading.Barrier(4)
        field_barrier = threading.Barrier(4)
        detection_threads, field_threads = set(), set()
        mutex = threading.Lock()

        def detect(source, output, **kwargs):
            with mutex:
                detection_threads.add(threading.current_thread())
            detection_barrier.wait(timeout=5)
            output.mkdir(parents=True)
            return output / 'result.json', {'fields': {
                'id': {'status': 'extracted', 'text': str(source)}}}

        def reconcile(report):
            ids = [side['fields']['id']['text'] for side in report['sides'].values()]
            self.assertEqual(ids[0], ids[1])
            report['national_id'] = {'status': 'agreed', 'text': ids[0]}

        def record_field_thread():
            with mutex:
                field_threads.add(threading.current_thread())
            field_barrier.wait(timeout=5)

        def text(side_results, device):
            record_field_thread()
            for _, result in side_results.values():
                result['fields']['name'] = {
                    'status': 'extracted', 'text': result['fields']['id']['text']}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(document, 'run_pipeline', side_effect=detect), \
             patch.object(document, 'reconcile_id_sides', side_effect=reconcile), \
             patch.object(document, 'apply_expiry_date', side_effect=lambda _: record_field_thread()), \
             patch.object(document, 'apply_back_classifiers', return_value={}), \
             patch.object(document, 'run_text_ocr', side_effect=text):
            with document.DocumentPools('cuda', 2) as pools, ThreadPoolExecutor(2) as jobs:
                original_threads = None
                for batch in range(3):
                    ids = [batch * 2 + 1, batch * 2 + 2]
                    futures = [jobs.submit(document.run_document, job, job,
                                           output=directory, document_id=job, pools=pools)
                               for job in ids]
                    for job, future in zip(ids, futures):
                        path, report = future.result(timeout=10)
                        self.assertTrue(path.is_file())
                        self.assertEqual(report['sides']['front']['fields']['name']['text'], str(job))
                        self.assertEqual(report['national_id']['text'], str(job))
                    current = (set(detection_threads), set(field_threads))
                    if original_threads is None:
                        original_threads = current
                    self.assertEqual(current, original_threads)
                    self.assertEqual(tuple(map(len, current)), (4, 4))
            self.assertTrue(all(not t.is_alive() for t in detection_threads | field_threads))
            with self.assertRaises(RuntimeError):
                pools.detection.submit(lambda: None)
            with self.assertRaises(RuntimeError):
                pools.fields.submit(lambda: None)

    def test_field_failure_drains_sibling_before_job_returns(self):
        qari_started, release_qari, failed = threading.Event(), threading.Event(), threading.Event()

        def detect(source, output, **kwargs):
            output.mkdir(parents=True)
            return output / 'result.json', {'fields': {'id': {'text': 'same'}}}

        def qari(*_):
            qari_started.set()
            self.assertTrue(release_qari.wait(5))

        def fail(_):
            self.assertTrue(qari_started.wait(5))
            failed.set()
            raise ValueError('field failed')

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(document, 'run_pipeline', side_effect=detect), \
             patch.object(document, 'reconcile_id_sides', side_effect=lambda r: r.update(
                 national_id={'status': 'agreed', 'text': 'same'})), \
             patch.object(document, 'apply_expiry_date', side_effect=fail), \
             patch.object(document, 'run_text_ocr', side_effect=qari), \
             document.DocumentPools() as pools, ThreadPoolExecutor(1) as jobs:
            future = jobs.submit(document.run_document, 'front', 'back',
                                 output=directory, pools=pools)
            try:
                self.assertTrue(failed.wait(5))
                self.assertFalse(future.done())
            finally:
                release_qari.set()
            with self.assertRaisesRegex(ValueError, 'field failed'):
                future.result(timeout=5)
            # A failed document does not shut down the shared pools.
            self.assertEqual(pools.fields.submit(lambda: 'ready').result(timeout=5), 'ready')


if __name__ == '__main__':
    unittest.main()
