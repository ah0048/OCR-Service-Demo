"""Coordinator recovery with fake Oracle state and actual HTTP API request handling."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from io import BytesIO
from functools import partial
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference import OcrOutput
from ocr_api import create_app
from ocr_http_client import ApiMissing, ApiProtocolError, ApiUnavailable, OcrClient
import oracle_coordinator as coordinator
from oracle_requests import TrackedJob, claim_request, clear_ack, mark_submitted
from oracle_worker import ShutdownRequest, mapped_result, save_result
from oracle_logging import capture_console


def image_bytes():
    stream = BytesIO()
    Image.new('RGB', (12, 12), 'white').save(stream, format='PNG')
    return stream.getvalue()


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pending = coordinator.PendingResults(self.root / 'pending')
        self.rows, self.waiting, self.uploaded, self.saved = {}, [], [], []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.save_failure = None

        def claim(*_):
            if not self.waiting:
                return None
            job_id = self.waiting.pop(0)
            row = TrackedJob(job_id, str(1000 + job_id), str(uuid4()))
            self.rows[job_id] = row
            return deepcopy(row)

        def inspect(_, job_id):
            return deepcopy(self.rows.get(job_id))

        def submitted(_, job, instance):
            self.rows[job.job_id].instance_id = instance
            job.instance_id = instance

        def images(_, nin, directory, job_id):
            self.uploaded.append(job_id)
            paths = [directory / f'{job_id}_{side}.png' for side in (1, 2)]
            for path in paths:
                path.write_bytes(image_bytes())
            return paths

        def save(conn, driver, job_id, out, threshold, storage, request_id):
            row = self.rows[job_id]
            self.assertEqual(row.request_id, request_id)
            self.assertTrue(self.pending.exists(row))
            if self.save_failure == 'before':
                self.save_failure = None
                raise RuntimeError('DATABASE_DISCONNECTED')
            values, _ = mapped_result(out, threshold)
            row.status, row.completed, row.ack_pending = values['STATUS'], True, True
            self.saved.append(job_id)
            if self.save_failure == 'after':
                self.save_failure = None
                raise RuntimeError('COMMIT_CONFIRMATION_LOST')
            return row.status

        def ack(_, job):
            self.assertTrue(self.rows[job.job_id].completed)
            self.rows[job.job_id].ack_pending = False

        mocks = {'claim_request': claim, 'inspect_request': inspect,
                 'mark_submitted': submitted, 'read_images': images, 'save_result': save,
                 'clear_ack': ack,
                 'outstanding': lambda _: [deepcopy(r) for r in self.rows.values()
                                          if r.status == 'UNDER_PROCESSING' or r.ack_pending]}
        for name, function in mocks.items():
            self.stack.enter_context(patch.object(coordinator, name, side_effect=function))

    def api(self, infer=None, lose_post_response=False):
        infer = infer or Mock(return_value=OcrOutput('Success', .9, {'fullName': 'test'}))
        app = create_app(token='test-token', output=self.root / 'spark', concurrency=3,
                         capacity=3, initialize_models=Mock(), infer=infer,
                         pools_factory=lambda _: nullcontext())
        server = self.stack.enter_context(TestClient(app))
        lost = False

        def transport(request):
            nonlocal lost
            response = server.request(request.method, request.url.path,
                                      headers=request.headers, content=request.read())
            if lose_post_response and request.url.path == '/ocr' and not lost:
                lost = True
                raise httpx.ReadTimeout('response lost', request=request)
            return response

        client = OcrClient('http://spark', 'test-token', transport=httpx.MockTransport(transport))
        self.stack.callback(client.close)
        return client, app, infer

    def worker(self, api):
        return coordinator.Coordinator(Mock(), Mock(), api, self.pending, {}, max_outstanding=3)

    def drain(self, worker):
        deadline = time.monotonic() + 5
        while worker.jobs and time.monotonic() < deadline:
            worker.cycle(allow_claim=False)
            time.sleep(.01)
        self.assertFalse(worker.jobs)

    def reconnecting_run(self, api, error_class):
        """Run the real supervisor and coordinator over the fake DB fixture."""
        args = SimpleNamespace(check_connection=False, db_retries=3, db_retry_seconds=2,
                               db_retry_max_seconds=30, max_outstanding=1,
                               confidence_threshold=.5, poll_seconds=.01, retry_seconds=.01,
                               drain_seconds=5, pending_dir=self.pending.directory)
        shutdown = ShutdownRequest()
        connections, delays = [], []
        driver = SimpleNamespace(Error=error_class)

        def connect(_):
            conn = MagicMock()
            connections.append(conn)
            return conn, driver

        def wait(*args):
            delays.append(args[2])
            if len(delays) > 100:
                raise AssertionError('Recovery loop did not finish')
            if self.rows and not self.waiting and not list(self.pending.directory.glob('*.json')) and all(
                    row.completed and not row.ack_pending for row in self.rows.values()):
                shutdown.handle()
            time.sleep(.001)

        with patch.dict(sys.modules, {'oracledb': driver}), \
             patch.object(coordinator, 'connect', side_effect=connect), \
             patch.object(coordinator, 'load_column_storage', return_value={}), \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=shutdown), \
             patch.object(coordinator, 'wait_for_stop', side_effect=wait):
            coordinator.run_database(args, api)
        self.assertGreaterEqual(len(connections), 2)
        for conn in connections:
            conn.close.assert_called_once()
        return delays

    def test_reconnect_reconciles_result_save_before_and_after_uncertain_commit(self):
        class LostConnection(Exception):
            pass

        original_save = coordinator.save_result.side_effect
        for position in ('before', 'after'):
            with self.subTest(position=position):
                self.rows.clear()
                self.uploaded.clear()
                self.saved.clear()
                self.waiting = [31]
                api, _, infer = self.api()
                failed = False

                def save(*args, **kwargs):
                    nonlocal failed
                    if not failed:
                        failed = True
                        if position == 'after':
                            original_save(*args, **kwargs)
                        raise LostConnection(SimpleNamespace(code=3113))
                    return original_save(*args, **kwargs)

                with patch.object(coordinator, 'save_result', side_effect=save):
                    delays = self.reconnecting_run(api, LostConnection)
                self.assertIn(2, delays)
                self.assertEqual(self.saved, [31])
                self.assertEqual(self.uploaded, [31])
                self.assertFalse(list(self.pending.directory.glob('*.json')))
                infer.assert_called_once()

    def test_reconnect_discovers_a_claim_whose_commit_confirmation_was_lost(self):
        class LostConnection(Exception):
            pass

        original_claim = coordinator.claim_request.side_effect
        first_request = None

        def claim(*args):
            nonlocal first_request
            job = original_claim(*args)
            if job and first_request is None:
                first_request = job.request_id  # Claim committed, reply never reached caller.
                raise LostConnection(SimpleNamespace(code=3113))
            return job

        self.waiting = [32]
        api, _, infer = self.api()
        with patch.object(coordinator, 'claim_request', side_effect=claim):
            self.reconnecting_run(api, LostConnection)
        self.assertEqual(self.rows[32].request_id, first_request)
        self.assertEqual(self.uploaded, [32])
        self.assertEqual(self.saved, [32])
        infer.assert_called_once()

    def test_reconnect_after_ack_flag_commit_loss_does_not_repeat_ocr_or_save(self):
        class LostConnection(Exception):
            pass

        original_clear = coordinator.clear_ack.side_effect
        failed = False

        def clear(*args):
            nonlocal failed
            original_clear(*args)
            if not failed:
                failed = True
                raise LostConnection(SimpleNamespace(code=3113))

        self.waiting = [33]
        api, _, infer = self.api()
        with patch.object(coordinator, 'clear_ack', side_effect=clear):
            self.reconnecting_run(api, LostConnection)
        self.assertEqual(self.saved, [33])
        self.assertFalse(list(self.pending.directory.glob('*.json')))
        infer.assert_called_once()

    def test_collected_result_is_saved_before_submission_tracking_can_fail(self):
        job = TrackedJob(34, '1034', str(uuid4()))
        self.rows[34] = deepcopy(job)
        api = Mock()
        api.get.return_value = {'requestId': job.request_id, 'jobId': 34,
                                'serviceInstanceId': str(uuid4()), 'status': 'COMPLETED',
                                'result': {'ocrOutcome': 'Success', 'confidence': .9,
                                           'extractedFields': {'fullName': 'retained'}, 'details': {}}}
        worker = self.worker(api)
        with patch.object(coordinator, 'mark_submitted', side_effect=RuntimeError('DB_DISCONNECTED')):
            with self.assertRaises(RuntimeError):
                worker.advance(job)
        self.assertTrue(self.pending.exists(job))
        self.assertFalse(self.saved)
        api.acknowledge.assert_not_called()

    def test_three_remote_jobs_complete_without_local_model_loading(self):
        release = threading.Event()
        entered = threading.Barrier(4)

        def infer(front, back, *, output, job_id):
            entered.wait(5)
            self.assertTrue(release.wait(5))
            return OcrOutput('Success', .9, {'fullName': 'test-' + str(job_id)})

        api, app, _ = self.api(infer)
        self.waiting = [1, 2, 3, 4]
        worker = self.worker(api)
        try:
            worker.cycle()
            entered.wait(5)
            self.assertEqual(len(worker.jobs), 3)
            self.assertEqual(self.waiting, [4])
            self.assertEqual(app.state.store.health()['requests']['RUNNING'], 3)
        finally:
            release.set()
        self.drain(worker)
        self.assertEqual(sorted(self.saved), [1, 2, 3])
        self.assertFalse(list(self.pending.directory.glob('*.json')))
        self.assertTrue(all(not row.ack_pending for row in self.rows.values()))

    def test_lost_submission_response_is_recovered_without_duplicate_upload_or_inference(self):
        api, _, infer = self.api(lose_post_response=True)
        self.waiting = [1]
        worker = self.worker(api)
        with self.assertRaises(ApiUnavailable):
            worker.cycle()
        self.assertEqual(len(worker.jobs), 1)
        self.drain(worker)
        self.assertEqual(self.uploaded, [1])
        infer.assert_called_once()
        self.assertEqual(self.saved, [1])

    def test_pending_result_replays_after_database_failure_before_or_after_commit(self):
        for position in ('before', 'after'):
            with self.subTest(position=position):
                self.rows.clear()
                self.saved.clear()
                api, _, infer = self.api()
                job = TrackedJob(10, '1010', str(uuid4()))
                self.rows[10] = deepcopy(job)
                worker = self.worker(api)
                worker.jobs[job.request_id] = job
                self.pending.write(job, OcrOutput('Success', .9, {'fullName': 'retained'}))
                self.save_failure = position
                with self.assertRaises(RuntimeError):
                    worker.advance(job)
                self.assertTrue(self.pending.exists(job))
                if position == 'after':
                    self.rows[10].status = 'FINISHED'  # SP may already have validated it.
                restarted = self.worker(api)
                restarted.restore()
                self.drain(restarted)
                self.assertEqual(self.saved, [10])
                self.assertFalse(self.pending.exists(job))
                infer.assert_not_called()

    def test_spark_restart_resubmits_images_but_connection_failure_does_not(self):
        new_api, new_app, infer = self.api()
        job = TrackedJob(20, '1020', str(uuid4()), instance_id=str(uuid4()))
        self.rows[20] = deepcopy(job)
        worker = self.worker(new_api)
        worker.restore()
        worker.cycle(allow_claim=False)
        self.drain(worker)
        self.assertEqual(self.uploaded, [20])
        self.assertEqual(self.rows[20].instance_id, new_app.state.store.instance_id)
        infer.assert_called_once()
        # A transport error is propagated for retry, never treated as a lost request.
        job = TrackedJob(21, '1021', str(uuid4()), instance_id=str(uuid4()))
        self.rows[21] = deepcopy(job)
        disconnected = self.worker(Mock(get=Mock(side_effect=ApiUnavailable())))
        disconnected.jobs[job.request_id] = job
        with self.assertRaises(ApiUnavailable):
            disconnected.cycle(allow_claim=False)
        self.assertEqual(self.uploaded, [20])
        self.assertEqual(self.rows[21].status, 'UNDER_PROCESSING')

    def test_missing_request_in_same_instance_is_not_blindly_resubmitted(self):
        api, app, _ = self.api()
        job = TrackedJob(30, '1030', str(uuid4()), instance_id=app.state.store.instance_id)
        self.rows[30] = deepcopy(job)
        worker = self.worker(api)
        with self.assertRaises(ApiProtocolError):
            worker.advance(job)
        self.assertFalse(self.uploaded)

    def test_stale_pending_result_does_not_overwrite_new_attempt(self):
        job = TrackedJob(40, '1040', str(uuid4()))
        self.pending.write(job, OcrOutput('Success', .9, {'name': 'old'}))
        self.rows[40] = TrackedJob(40, '1040', str(uuid4()))
        worker = self.worker(Mock())
        with self.assertRaisesRegex(RuntimeError, 'OWNERSHIP_CHANGED'):
            worker.restore()
        self.assertFalse(self.saved)
        self.assertTrue(self.pending.exists(job))

    def test_api_outage_prevents_new_claims(self):
        self.waiting = [1, 2, 3]
        worker = self.worker(Mock(ready=Mock(side_effect=ApiUnavailable())))
        with self.assertRaises(ApiUnavailable):
            worker.cycle()
        self.assertEqual(self.waiting, [1, 2, 3])

    def test_lost_ack_response_recovers_without_resaving_or_repeating_inference(self):
        api, _, infer = self.api()
        acknowledge = api.acknowledge

        def lose_response(*args):
            acknowledge(*args)
            raise ApiUnavailable('ACK_RESPONSE_LOST')

        self.waiting = [1]
        worker = self.worker(api)
        with patch.object(api, 'acknowledge', side_effect=lose_response):
            with self.assertRaises(ApiUnavailable):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    worker.cycle()
                    time.sleep(.01)
        self.assertEqual(self.saved, [1])
        self.assertTrue(self.rows[1].ack_pending)
        restarted = self.worker(api)
        restarted.restore()
        self.drain(restarted)
        self.assertEqual(self.saved, [1])
        infer.assert_called_once()
        self.assertFalse(self.rows[1].ack_pending)

    def test_coordinator_import_does_not_load_model_or_api_dependencies(self):
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import oracle_coordinator; "
                "print([p for p in ('torch','PIL','fastapi','transformers','ultralytics','oracledb') "
                "if p in sys.modules])")
        result = subprocess.run([sys.executable, '-c', code, str(Path(__file__).resolve().parent)],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(result.stdout.strip(), '[]')

    def test_api_only_check_never_connects_to_oracle_and_logs_launcher_pid(self):
        client = Mock()
        instance_id = str(uuid4())
        client.ready.return_value = {'serviceInstanceId': instance_id}
        with patch.object(sys, 'argv', ['oracle_coordinator.py', '--check-api']), \
             patch.dict('os.environ', {'OCR_API_TOKEN': 'private-test-token'}), \
             patch.object(coordinator, 'OcrClient', return_value=client), \
             patch.object(coordinator, 'connect') as connect_db, \
             patch.object(coordinator, 'capture_console', partial(capture_console, directory=self.root / 'logs')):
            coordinator.main()
        connect_db.assert_not_called()
        client.ready.assert_called_once()
        client.close.assert_called_once()
        files = list((self.root / 'logs').glob('oracle_coordinator_*_coordinator_*.log'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding='utf-8')
        for expected in ('launcherPid=', 'role=coordinator', 'Coordinator launcher started',
                         'OCR API ready', instance_id, 'Coordinator launcher stopped'):
            self.assertIn(expected, text)
        self.assertNotIn('private-test-token', text)


class TrackingSqlTests(unittest.TestCase):
    def connection(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        return conn, cur

    def test_claim_records_uuid_in_same_transaction_as_under_processing(self):
        conn, cur = self.connection()
        cur.var.side_effect = [SimpleNamespace(getvalue=lambda: 7), SimpleNamespace(getvalue=lambda: 1007)]
        job = claim_request(conn, SimpleNamespace(DB_TYPE_NUMBER='number'))
        sql, = cur.execute.call_args.args
        self.assertIn('FOR UPDATE SKIP LOCKED', sql)
        self.assertEqual(sql.count('FETCH jobs INTO'), 1)
        self.assertIn('OCR_REQUEST_ID = :request_id', sql)
        self.assertEqual(cur.execute.call_args.kwargs['request_id'], job.request_id)
        self.assertEqual(len(job.request_id), 36)
        conn.commit.assert_called_once()

    def test_submit_and_ack_updates_check_ownership(self):
        job = TrackedJob(7, '1007', str(uuid4()))
        for operation in (lambda c: mark_submitted(c, job, str(uuid4())), lambda c: clear_ack(c, job)):
            conn, cur = self.connection()
            cur.rowcount = 1
            operation(conn)
            self.assertIn('OCR_REQUEST_ID = :request_id', cur.execute.call_args.args[0])
            conn.commit.assert_called_once()
            cur.rowcount = 0
            with self.assertRaisesRegex(RuntimeError, 'OWNERSHIP_CHANGED'):
                operation(conn)
            conn.rollback.assert_called_once()

    def test_guarded_save_commits_ack_flag_with_result_and_keeps_legacy_default(self):
        conn, cur = self.connection()
        cur.fetchone.return_value = (None,) * 7
        cur.rowcount = 1
        driver = SimpleNamespace(DB_TYPE_CLOB='clob', DB_TYPE_NCLOB='nclob',
                                 DB_TYPE_DATE='date', DB_TYPE_NVARCHAR='nvarchar')
        request_id = str(uuid4())
        save_result(conn, driver, 7, OcrOutput('Success', .9, {'fullName': 'test'}),
                    .5, request_id=request_id)
        sql, binds = cur.execute.call_args.args
        self.assertIn('OCR_ACK_PENDING = 1', sql)
        self.assertIn('AND OCR_REQUEST_ID = :request_id', sql)
        self.assertEqual(binds['request_id'], request_id)
        conn.commit.assert_called_once()

    def test_http_client_rejects_mismatched_responses(self):
        request_id = str(uuid4())
        response = {'requestId': str(uuid4()), 'jobId': 7, 'status': 'RUNNING',
                    'serviceInstanceId': str(uuid4())}
        client = OcrClient('http://localhost:18001', 'test', transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=response)))
        try:
            with self.assertRaises(ApiProtocolError):
                client.get(request_id, 7)
        finally:
            client.close()


if __name__ == '__main__':
    unittest.main()
