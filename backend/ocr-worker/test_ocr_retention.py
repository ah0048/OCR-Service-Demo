"""Retention boundaries and API backpressure, using only disposable directories."""
from contextlib import nullcontext
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from inference import OcrOutput
from ocr_api import create_app
from ocr_log_retention import ConsoleLogFile, prune_closed_logs
from ocr_retention import ArtifactRetention, MARKER


class ArtifactRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.policy = ArtifactRetention(self.root, hours=1, min_free_mb=0)

    def request(self):
        directory = self.root / str(uuid4()) / str(uuid4())
        directory.mkdir(parents=True)
        (directory / 'image.jpg').write_bytes(b'original image')
        return directory

    def test_only_expired_explicitly_acknowledged_directories_are_removed(self):
        old, fresh, unacknowledged = self.request(), self.request(), self.request()
        now = time.time()
        with patch('ocr_retention.time.time', return_value=now - 7200):
            self.policy.acknowledge(old)
        self.policy.acknowledge(fresh)
        # An old mtime alone is not permission to delete an unacknowledged result.
        os.utime(unacknowledged, (now - 7200, now - 7200))
        pending = self.root / 'pending-results' / 'job.json'
        pending.parent.mkdir()
        pending.write_text('must remain')
        self.assertEqual(self.policy.sweep(now=now), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unacknowledged.exists())
        self.assertTrue(pending.exists())

    def test_restart_policy_can_clean_acknowledged_artifacts_from_previous_instance(self):
        directory = self.request()
        self.policy.acknowledge(directory)
        restarted = ArtifactRetention(self.root, hours=1, min_free_mb=0)
        self.assertEqual(restarted.sweep(now=time.time() + 7200), 1)
        self.assertFalse(directory.exists())

    def test_invalid_marker_and_unrelated_folder_are_protected(self):
        directory = self.request()
        data = {'version': 1, 'requestId': str(uuid4()),
                'serviceInstanceId': directory.parent.name, 'acknowledgedAt': 1}
        (directory / MARKER).write_text(json.dumps(data))
        unrelated = self.root / 'other-team'
        unrelated.mkdir()
        (unrelated / MARKER).write_text('{}')
        self.assertEqual(self.policy.sweep(now=time.time() + 7200), 0)
        self.assertTrue(directory.exists())
        self.assertTrue(unrelated.exists())

    def test_symlink_to_external_directory_is_not_followed(self):
        outside = self.root / 'outside'
        outside.mkdir()
        protected = outside / 'keep.txt'
        protected.write_text('keep')
        instance = self.root / str(uuid4())
        instance.mkdir()
        link = instance / str(uuid4())
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symlinks unavailable for this Windows account')
        self.assertEqual(self.policy.sweep(now=time.time() + 7200), 0)
        self.assertTrue(protected.exists())

    def test_zero_retention_disables_sweeping_and_background_thread_stops(self):
        directory = self.request()
        self.policy.acknowledge(directory)
        disabled = ArtifactRetention(self.root, hours=0)
        self.assertEqual(disabled.sweep(now=time.time() + 7200), 0)
        disabled.start()
        disabled.close()
        self.policy.interval = .01
        self.policy.start()
        self.policy.close()
        self.assertFalse(self.policy._thread.is_alive())


class LogRetentionTests(unittest.TestCase):
    def test_rotation_keeps_console_text_chunks_and_bounded_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ocr_api_20261117_120000_123456_api_123.log'
            with ConsoleLogFile(path, max_bytes=30, backups=2, prefix='ocr_api') as stream:
                for number in range(8):
                    stream.write(f'line {number} data\n')
            self.assertTrue(path.is_file())
            self.assertTrue(path.with_name(path.name + '.1').is_file())
            self.assertTrue(path.with_name(path.name + '.2').is_file())
            self.assertFalse(path.with_name(path.name + '.3').exists())
            self.assertIn('line 7', path.read_text(encoding='utf-8'))
            self.assertTrue(path.with_name(path.name + '.closed').is_file())

    def test_closed_family_limits_leave_active_and_unrelated_logs_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = []
            for number in range(3):
                path = root / f'ocr_api_20261117_120000_12345{number}_api_{number + 1}.log'
                path.write_text('closed log')
                marker = path.with_name(path.name + '.closed')
                marker.write_text('closed')
                os.utime(marker, (time.time() - number * 60, time.time() - number * 60))
                paths.append(path)
            active = root / 'ocr_api_20261117_120000_654321_api_999.log'
            active.write_text('active without a closed marker')
            unrelated = root / 'other-team.log'
            unrelated.write_text('keep')
            self.assertEqual(prune_closed_logs(root, 'ocr_api', keep_runs=1), 2)
            self.assertTrue(paths[0].exists())
            self.assertFalse(paths[1].exists())
            self.assertTrue(active.exists())
            self.assertTrue(unrelated.exists())


class ApiDiskTests(unittest.TestCase):
    def test_low_disk_pauses_uploads_but_preserves_results_and_acknowledgement(self):
        content = BytesIO()
        Image.new('RGB', (12, 12), 'white').save(content, format='PNG')
        image = content.getvalue()
        token = {'Authorization': 'Bearer test-token'}
        request_id = str(uuid4())
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(token='test-token', output=directory, concurrency=1, capacity=1,
                             initialize_models=Mock(), pools_factory=lambda _: nullcontext(),
                             infer=Mock(return_value=OcrOutput('Success', .9, {'name': 'test'})))
            with TestClient(app) as client:
                response = client.post('/ocr', headers=token, data={'request_id': request_id, 'job_id': 1},
                                       files={'front': ('front.png', image), 'back': ('back.png', image)})
                self.assertEqual(response.status_code, 202)
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    result = client.get('/ocr/requests/' + request_id, headers=token)
                    if result.json()['status'] == 'COMPLETED':
                        break
                    time.sleep(.01)
                self.assertEqual(result.json()['status'], 'COMPLETED')
                with patch('ocr_retention.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
                    self.assertEqual(client.get('/health/ready', headers=token).status_code, 503)
                    response = client.post('/ocr', headers=token,
                                           data={'request_id': str(uuid4()), 'job_id': 2},
                                           files={'front': ('f.png', image), 'back': ('b.png', image)})
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(response.json()['errorCode'], 'LOW_DISK_SPACE')
                    self.assertEqual(client.get('/ocr/requests/' + request_id, headers=token).status_code, 200)
                    marker_writer = app.state.store.retention
                    with patch.object(marker_writer, 'acknowledge', side_effect=OSError('disk full')):
                        self.assertEqual(client.post('/ocr/requests/' + request_id + '/ack', headers=token).status_code, 503)
                    self.assertEqual(client.get('/ocr/requests/' + request_id, headers=token).json()['status'], 'COMPLETED')
                    self.assertEqual(client.post('/ocr/requests/' + request_id + '/ack', headers=token).status_code, 200)


if __name__ == '__main__':
    unittest.main()
