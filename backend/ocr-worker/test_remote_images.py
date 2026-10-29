"""HTTP transfer and cleanup checks; no model weights or RabbitMQ needed."""
import io
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference

JOB = {'jobId': 'b6975489-81b6-40bb-adb7-aeec55f0c0aa', 'imageReference': '../../ignored'}


def response(data=b'image', **headers):
    result = io.BytesIO(data)
    result.headers = headers
    return result


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / 'images'
        self.url = patch.object(inference, 'IMAGE_API_URL', 'http://127.0.0.1:15125')
        self.url.start()
        self.addCleanup(self.url.stop)
        self.opener = Mock()
        opener_patch = patch.object(inference, 'build_opener', return_value=self.opener)
        opener_patch.start()
        self.addCleanup(opener_patch.stop)

    def test_downloads_fixed_job_routes_and_preserves_bytes(self):
        self.opener.open.side_effect = [response(b'front'), response(b'back')]
        front, back = inference.download_image_pair(JOB, self.directory)
        self.assertEqual(front.read_bytes(), b'front')
        self.assertEqual(back.read_bytes(), b'back')
        self.assertEqual([c.args[0] for c in self.opener.open.call_args_list],
                         [f'http://127.0.0.1:15125/api/jobs/{JOB["jobId"]}/images/{s}' for s in ('front', 'back')])

    def test_invalid_job_id_cannot_change_route(self):
        with self.assertRaises(ValueError):
            inference.download_image_pair({'jobId': '../other'}, self.directory)
        self.opener.open.assert_not_called()

    def test_rejects_oversized_declared_and_streamed_images(self):
        for headers in ({'Content-Length': '6'}, {}):
            with self.subTest(headers=headers), tempfile.TemporaryDirectory() as directory:
                self.opener.open.return_value = response(b'123456', **headers)
                with patch.object(inference, 'MAX_IMAGE_BYTES', 5), self.assertRaises(ValueError):
                    inference.download_image_pair(JOB, Path(directory) / 'images')

    def test_missing_image_is_permanent_and_not_retried(self):
        self.opener.open.side_effect = HTTPError('url', 404, 'missing', {}, None)
        with self.assertRaises(ValueError):
            inference.download_image_pair(JOB, self.directory)
        self.assertEqual(self.opener.open.call_count, 1)

    def test_auth_errors_and_redirects_are_technical_failures(self):
        for status in (302, 401, 403):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                self.opener.open.side_effect = HTTPError('url', status, 'rejected', {}, None)
                with self.assertRaises(inference.ImageDownloadError):
                    inference.download_image_pair(JOB, Path(directory) / 'images')

    def test_network_failures_retry_then_return_technical_error(self):
        self.opener.open.side_effect = URLError('connection refused')
        with patch.object(inference.time, 'sleep') as sleep, self.assertRaises(inference.ImageDownloadError):
            inference.download_image_pair(JOB, self.directory)
        self.assertEqual(self.opener.open.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [1, 2])

    def test_server_error_retries_and_recovers(self):
        self.opener.open.side_effect = [HTTPError('url', 503, 'busy', {}, None), response(), response()]
        with patch.object(inference.time, 'sleep'):
            inference.download_image_pair(JOB, self.directory)
        self.assertEqual(self.opener.open.call_count, 3)

    def test_truncated_response_retries_without_accepting_partial_file(self):
        self.opener.open.side_effect = [response(b'x', **{'Content-Length': '2'}), response(b'front'), response(b'back')]
        with patch.object(inference.time, 'sleep'):
            front, _ = inference.download_image_pair(JOB, self.directory)
        self.assertEqual(front.read_bytes(), b'front')

    def test_rejects_credentials_query_and_non_http_configuration(self):
        for url in ('file:///tmp', 'http://u:p@localhost', 'http://localhost?q=x', 'http://localhost#x'):
            with self.subTest(url=url), patch.object(inference, 'IMAGE_API_URL', url), self.assertRaises(ValueError):
                inference.validate_image_api_url()

    def test_download_failures_have_separate_error_code_and_cleanup(self):
        paths = []
        def fail(job, directory):
            directory.mkdir()
            (directory / 'front').write_bytes(b'partial')
            paths.append(directory)
            raise inference.ImageDownloadError('offline')
        with patch.object(inference, 'download_image_pair', side_effect=fail):
            result = inference.run_ocr(JOB)
        self.assertEqual(result.outcome, 'TechnicalError')
        self.assertEqual(result.details['errorCode'], 'IMAGE_DOWNLOAD_FAILED')
        self.assertFalse(paths[0].parent.exists())


class HttpIntegrationTests(unittest.TestCase):
    def test_real_http_download_image_validation_and_temporary_cleanup(self):
        from PIL import Image
        content = io.BytesIO()
        Image.new('RGB', (10, 10)).save(content, 'PNG')
        data = content.getvalue()
        paths = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                paths.append(self.path)
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        used_paths = []
        def run_document(front, back, **kwargs):
            used_paths.extend([front, back])
            self.assertEqual(front.read_bytes(), data)
            self.assertEqual(back.read_bytes(), data)
            return None, {'sides': {}}
        try:
            with patch.object(inference, 'IMAGE_API_URL', f'http://127.0.0.1:{server.server_port}'), \
                 patch.dict(sys.modules, {'pipeline.document': SimpleNamespace(run_document=run_document)}):
                result = inference.run_ocr(JOB)
            self.assertEqual(result.outcome, 'Unreadable')
            self.assertEqual(len(paths), 2)
            self.assertTrue(used_paths)
            self.assertTrue(all(not path.exists() for path in used_paths))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
