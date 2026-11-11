"""Real job threads, with fake Oracle connections and inference (no live jobs)."""
from collections import deque
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_worker as worker


class Connection:
    def __init__(self):
        self.owner = threading.get_ident()
        self.closed = False
        self.autocommit = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def cursor(self):
        return Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))


class ConcurrencyTests(unittest.TestCase):
    def test_single_job_default_reuses_startup_connection(self):
        args = SimpleNamespace(job_concurrency=1, check_connection=False, poll_seconds=2)
        conn, driver, storage = Connection(), SimpleNamespace(), {}
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {'oracledb': driver}), \
             patch.object(worker, 'OUTPUT_ROOT', Path(directory)), \
             patch.object(worker, 'connect', return_value=(conn, driver)), \
             patch.object(worker, 'load_column_storage', return_value=storage), \
             patch.object(worker, 'initialize') as initialize, \
             patch.object(worker, 'consume_jobs') as consume, \
             patch.object(worker, 'ThreadPoolExecutor') as pool:
            stop, request = threading.Event(), worker.ShutdownRequest()
            worker._run_worker(args, stop, request, Mock())
            consume.assert_called_once_with(args, stop, request, conn, driver, storage)
            initialize.assert_called_once_with()
            pool.assert_not_called()
        self.assertTrue(conn.closed)

    def test_connection_check_never_loads_models_or_starts_threads(self):
        args = SimpleNamespace(job_concurrency=2, check_connection=True)
        conn = Connection()
        conn.version = '11.2.0.4'
        driver = SimpleNamespace(is_thin_mode=lambda: False)
        with patch.dict(sys.modules, {'oracledb': driver}), \
             patch.object(worker, 'connect', return_value=(conn, driver)), \
             patch.object(worker, 'load_column_storage', return_value={}), \
             patch.object(worker, 'initialize') as initialize, \
             patch.object(worker, 'ThreadPoolExecutor') as pool:
            worker._run_worker(args, threading.Event(), worker.ShutdownRequest(), Mock())
            initialize.assert_not_called()
            pool.assert_not_called()
        self.assertTrue(conn.closed)

    def exercise_threads(self, *, fail_save=False, request_shutdown=False):
        args = SimpleNamespace(job_concurrency=2, check_connection=False, user='test',
                               password='test', dsn='test', poll_seconds=.01,
                               confidence_threshold=.5)
        stop, request = threading.Event(), worker.ShutdownRequest()
        startup = Connection()
        connections, claims, saved, results = [], [], [], []
        jobs = deque([(1, 101), (2, 102)])
        mutex, both_inferring = threading.Lock(), threading.Barrier(2)

        def connect(**_):
            self.assertTrue(startup.closed)
            conn = Connection()
            with mutex:
                connections.append(conn)
            return conn

        def claim(conn, _):
            self.assertEqual(conn.owner, threading.get_ident())
            with mutex:
                if not jobs:
                    stop.set()
                    return None
                job = jobs.popleft()
                claims.append(job[0])
                return job

        def infer(front, back, *, output, job_id):
            both_inferring.wait(timeout=5)
            if request_shutdown:
                request.handle()  # Both active jobs must still save their result.
            return worker.OcrOutput('Success', .9, {'job': str(job_id)})

        def save(conn, driver, job_id, out, *rest):
            self.assertEqual(conn.owner, threading.get_ident())
            self.assertEqual(out.fields['job'], str(job_id))
            if fail_save and job_id == 1:
                raise RuntimeError('save failed')
            if fail_save:
                self.assertTrue(stop.wait(5))  # Sibling failure must signal stop.
            with mutex:
                saved.append(job_id)
            return 'OCR_SUCCESS'

        driver = SimpleNamespace(connect=connect)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(sys.modules, {'oracledb': driver}), \
             patch.object(worker, 'OUTPUT_ROOT', Path(directory)), \
             patch.object(worker, 'connect', return_value=(startup, driver)), \
             patch.object(worker, 'load_column_storage', return_value={}), \
             patch.object(worker, 'initialize') as initialize, \
             patch.object(worker, 'claim_job', side_effect=claim), \
             patch.object(worker, 'read_images', return_value=(Path('front'), Path('back'))), \
             patch.object(worker, 'run_ocr_paths', side_effect=infer), \
             patch.object(worker, 'save_result', side_effect=save), \
             patch.object(worker, 'write_job_output', side_effect=lambda *a: results.append(a)):
            if fail_save:
                with self.assertRaisesRegex(RuntimeError, 'save failed'):
                    worker._run_worker(args, stop, request, Mock())
            else:
                worker._run_worker(args, stop, request, Mock())
            initialize.assert_called_once_with()
        self.assertEqual(sorted(claims), [1, 2])
        self.assertEqual(len(connections), 2)
        self.assertEqual(len({c.owner for c in connections}), 2)
        self.assertTrue(all(c.closed for c in connections))
        self.assertEqual(sorted(saved), [2] if fail_save else [1, 2])
        # Both initial summaries retained, including the job whose DB save failed.
        self.assertEqual(sorted(a[1] for a in results if len(a) == 3), [1, 2])

    def test_parallel_jobs_load_once_and_never_share_connections(self):
        self.exercise_threads()

    def test_database_failure_stops_claims_and_drains_other_active_job(self):
        self.exercise_threads(fail_save=True)

    def test_shutdown_drains_all_active_jobs(self):
        self.exercise_threads(request_shutdown=True)

    def test_cli_concurrency_default_and_bounds(self):
        with patch.dict('os.environ', {'ORACLE_DSN': 'test', 'ORACLE_PASSWORD': 'test'}):
            with patch.object(sys, 'argv', ['worker']):
                self.assertEqual(worker.parse_args().job_concurrency, 1)
            with patch.object(sys, 'argv', ['worker', '--job-concurrency', '2']):
                self.assertEqual(worker.parse_args().job_concurrency, 2)
            for value in ('0', '9'):
                with patch.object(sys, 'argv', ['worker', '--job-concurrency', value]), \
                     patch('sys.stderr'), self.assertRaises(SystemExit):
                    worker.parse_args()


if __name__ == '__main__':
    unittest.main()
