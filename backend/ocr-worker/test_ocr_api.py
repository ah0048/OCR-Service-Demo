"""HTTP contract and real inference-thread scheduling, without models or Oracle."""
from contextlib import contextmanager, nullcontext
from io import BytesIO
from functools import partial
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference import OcrOutput, validate_image
from ocr_api import create_app
from ocr_requests import RequestError, RequestStore
from oracle_logging import capture_console
import ocr_api


def png(color='white'):
    stream = BytesIO()
    Image.new('RGB', (16, 12), color).save(stream, format='PNG')
    return stream.getvalue()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.headers = {'Authorization': 'Bearer test-only-token'}

    def app(self, **kwargs):
        self.initialize = Mock()
        self.infer = Mock(return_value=OcrOutput('LowConfidence', .7,
                                                {'fullName': 'test'}, {'review_required': True}))
        options = dict(token='test-only-token', output=self.root, concurrency=1, capacity=1,
                       initialize_models=self.initialize, infer=self.infer,
                       pools_factory=lambda _: nullcontext())
        options.update(kwargs)
        return create_app(**options)

    def post(self, client, request_id=None, job=123, image=None, headers=None):
        request_id = request_id or str(uuid4())
        return client.post('/ocr', data={'request_id': request_id, 'job_id': str(job)},
                           files={'front': ('front.png', image or png(), 'image/png'),
                                  'back': ('back.png', png(), 'image/png')},
                           headers=self.headers if headers is None else headers)

    def terminal(self, client, request_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            response = client.get('/ocr/requests/' + request_id, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            if response.json()['status'] in ('COMPLETED', 'FAILED'):
                return response.json()
            time.sleep(.01)
        self.fail('Inference did not finish')

    def test_accept_result_acknowledgement_and_capacity_release(self):
        app = self.app(retain_artifacts=False)
        with TestClient(app) as client:
            response = self.post(client)
            self.assertEqual(response.status_code, 202)
            data = response.json()
            request_id = data['requestId']
            self.assertEqual(response.headers['Location'], '/ocr/requests/' + request_id)
            result = self.terminal(client, request_id)
            self.assertEqual(result['result']['ocrOutcome'], 'LowConfidence')
            self.assertEqual(result['result']['confidence'], .7)
            store = app.state.store
            directory = store.root / request_id
            self.assertTrue((directory / 'uploads/123_1.png').is_file())
            self.assertTrue((directory / 'uploads/123_2.png').is_file())
            self.assertEqual(self.post(client).status_code, 503)  # Completed still occupies a slot.
            for _ in range(2):
                ack = client.post(f'/ocr/requests/{request_id}/ack', headers=self.headers)
                self.assertEqual(ack.json()['status'], 'ACKNOWLEDGED')
            self.assertFalse(directory.exists())
            self.assertEqual(store.health()['outstanding'], 0)
            self.assertEqual(self.post(client).status_code, 202)
        self.initialize.assert_called_once()
        self.assertFalse(store.ready)

    def test_duplicate_reuses_result_and_conflicting_images_or_job_are_rejected(self):
        app = self.app()
        request_id = str(uuid4())
        with TestClient(app) as client:
            self.post(client, request_id)
            self.terminal(client, request_id)
            self.assertEqual(self.post(client, request_id).status_code, 202)
            self.assertEqual(self.post(client, request_id, image=png('black')).status_code, 409)
            self.assertEqual(self.post(client, request_id, job=999).status_code, 409)
            client.post(f'/ocr/requests/{request_id}/ack', headers=self.headers)
            self.assertEqual(self.post(client, request_id).json()['status'], 'ACKNOWLEDGED')
            self.infer.assert_called_once()
            self.assertFalse(list(app.state.store.root.glob('upload_*')))

    def test_three_requests_run_together_and_status_remains_responsive(self):
        entered, release = threading.Barrier(4), threading.Event()
        calls = []
        lock = threading.Lock()

        def infer(front, back, *, output, job_id):
            with lock:
                calls.append((job_id, threading.current_thread()))
            entered.wait(5)
            self.assertTrue(release.wait(5))
            return OcrOutput('Success', .9, {'job': str(job_id)})

        app = self.app(concurrency=3, capacity=3, infer=infer)
        with TestClient(app) as client:
            try:
                ids = [self.post(client, job=job).json()['requestId'] for job in (1, 2, 3)]
                entered.wait(5)
                self.assertEqual(client.get('/health/ready', headers=self.headers).json()['requests']['RUNNING'], 3)
                for request_id in ids:
                    self.assertEqual(client.get('/ocr/requests/' + request_id,
                                                headers=self.headers).json()['status'], 'RUNNING')
                self.assertEqual(client.post('/ocr/requests/' + ids[0] + '/ack',
                                             headers=self.headers).status_code, 409)
                self.assertEqual(self.post(client).status_code, 503)
            finally:
                release.set()
            for job, request_id in zip((1, 2, 3), ids):
                self.assertEqual(self.terminal(client, request_id)['result']['extractedFields']['job'], str(job))
        self.assertEqual(len({thread for _, thread in calls}), 3)
        self.assertTrue(all(not thread.is_alive() for _, thread in calls))

    def test_queued_requests_are_drained_before_stage_pools_close(self):
        events = []
        release = threading.Event()

        @contextmanager
        def pools(_):
            events.append('pools-started')
            yield
            events.append('pools-stopped')

        def infer(*_, **kwargs):
            events.append('job-' + str(kwargs['job_id']))
            self.assertTrue(release.wait(5))
            return OcrOutput('Success', .9, {'name': 'test'})

        app = self.app(capacity=3, infer=infer, pools_factory=pools)
        with TestClient(app) as client:
            try:
                for job in (1, 2, 3):
                    self.assertEqual(self.post(client, job=job).status_code, 202)
                self.assertGreaterEqual(app.state.store.health()['requests']['QUEUED'], 2)
            finally:
                # Permit completion during shutdown, after leaving the client scope.
                timer = threading.Timer(.05, release.set)
                timer.start()
        timer.join()
        self.assertEqual(events, ['pools-started', 'job-1', 'job-2', 'job-3', 'pools-stopped'])

    def test_runtime_failure_is_terminal_and_can_be_acknowledged(self):
        app = self.app(infer=Mock(side_effect=RuntimeError('sensitive contents')))
        with TestClient(app) as client:
            with self.assertLogs('ocr-api', level='INFO') as captured:
                request_id = self.post(client).json()['requestId']
                result = self.terminal(client, request_id)
            self.assertEqual(result['status'], 'FAILED')
            self.assertEqual(result['errorCode'], 'INFERENCE_ERROR')
            self.assertNotIn('sensitive contents', '\n'.join(captured.output))
            self.assertEqual(client.post(f'/ocr/requests/{request_id}/ack',
                                         headers=self.headers).status_code, 200)

    def test_authentication_invalid_images_and_input_validation(self):
        app = self.app()
        with TestClient(app) as client:
            self.assertEqual(client.get('/health/live').status_code, 200)
            self.assertEqual(client.get('/health/ready').status_code, 401)
            self.assertEqual(self.post(client, headers={}).status_code, 401)
            self.assertEqual(self.post(client, image=b'not an image').status_code, 422)
            self.assertEqual(app.state.store.health()['outstanding'], 0)
            self.assertFalse(list(app.state.store.root.iterdir()))
            self.assertEqual(self.post(client, job=0).status_code, 422)
            self.assertEqual(self.post(client, request_id='../../invalid').status_code, 422)
            missing = client.get('/ocr/requests/' + str(uuid4()), headers=self.headers)
            self.assertEqual(missing.status_code, 404)
            self.assertIn('serviceInstanceId', missing.json())

    def test_restart_has_new_instance_id_and_does_not_claim_files_are_recovered(self):
        request_id = str(uuid4())
        with TestClient(self.app()) as client:
            old = self.post(client, request_id).json()['serviceInstanceId']
            self.terminal(client, request_id)
        with TestClient(self.app()) as client:
            response = client.get('/ocr/requests/' + request_id, headers=self.headers)
            self.assertEqual(response.status_code, 404)
            self.assertNotEqual(response.json()['serviceInstanceId'], old)

    def test_oversized_upload_releases_reservation_and_removes_partial_files(self):
        store = RequestStore(self.root, 1, 1, Mock(), validate_image, 4)
        try:
            with self.assertRaises(RequestError) as error:
                store.submit(str(uuid4()), 1, BytesIO(b'12345'), BytesIO(b'12'))
            self.assertEqual(error.exception.status, 413)
            self.assertEqual(store.health()['outstanding'], 0)
            self.assertFalse(list(store.root.iterdir()))
        finally:
            store.close()

    def test_upload_in_progress_is_not_accepted_twice_and_reservation_is_bounded(self):
        entered, release = threading.Event(), threading.Event()

        class SlowStream(BytesIO):
            def read(self, size=-1):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError()
                return super().read(size)

        store = RequestStore(self.root, 1, 1,
                             Mock(return_value=OcrOutput('Success', .9, {'name': 'test'})),
                             validate_image, 1024)
        from concurrent.futures import ThreadPoolExecutor
        request_id = str(uuid4())
        try:
            with ThreadPoolExecutor(1) as uploads:
                future = uploads.submit(store.submit, request_id, 123, SlowStream(png()), BytesIO(png()))
                try:
                    self.assertTrue(entered.wait(5))
                    with self.assertRaises(RequestError) as duplicate:
                        store.submit(request_id, 123, BytesIO(png()), BytesIO(png()))
                    self.assertEqual(duplicate.exception.code, 'REQUEST_UPLOADING')
                    with self.assertRaises(RequestError) as full:
                        store.submit(str(uuid4()), 124, BytesIO(png()), BytesIO(png()))
                    self.assertEqual(full.exception.code, 'CAPACITY_EXHAUSTED')
                finally:
                    release.set()
                future.result(timeout=5)
        finally:
            release.set()
            store.close()

    def test_invalid_configuration_fails_before_model_loading(self):
        for options in ({'token': ''}, {'concurrency': 0}, {'capacity': 0}, {'capacity': 65}):
            with self.assertRaises(ValueError):
                self.app(**options)

    def test_launcher_captures_api_and_job_logs_in_console_style_file(self):
        app = self.app()

        def run(application, **options):
            self.assertEqual(options['workers'], 1)
            self.assertFalse(options['access_log'])
            with TestClient(application) as client:
                request_id = self.post(client).json()['requestId']
                self.terminal(client, request_id)
                client.post(f'/ocr/requests/{request_id}/ack', headers=self.headers)

        with patch.object(sys, 'argv', ['ocr_api.py']), \
             patch.dict('os.environ', {'OCR_API_TOKEN': 'test-only-token'}), \
             patch.object(ocr_api, 'create_app', return_value=app), \
             patch.object(ocr_api, 'capture_console', partial(capture_console, directory=self.root / 'logs')), \
             patch('uvicorn.run', side_effect=run):
            ocr_api.main()
        files = list((self.root / 'logs').glob('ocr_api_*_api_*.log'))
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding='utf-8')
        for value in ('launcherPid=', 'role=api', 'API launcher started', 'Accepted job=123',
                      'Processing job=123', 'thread=ocr-job_0', 'Acknowledged job=123',
                      'Shutdown requested', 'API launcher stopped'):
            self.assertIn(value, text)
        self.assertNotIn('test-only-token', text)


if __name__ == '__main__':
    unittest.main()
