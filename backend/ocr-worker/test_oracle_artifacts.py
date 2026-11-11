"""Verify retained Oracle job images/reports without model inference or Oracle."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference
import oracle_worker as worker


class OracleArtifactTests(unittest.TestCase):
    def test_uploads_use_job_id_side_and_actual_format_without_changing_bytes(self):
        conn = MagicMock()
        cursor = conn.cursor.return_value.__enter__.return_value
        front_bytes = b'\xff\xd8\xfforiginal JPEG blob'
        back_bytes = b'\x89PNG\r\n\x1a\noriginal PNG blob'

        def blob(data):
            return SimpleNamespace(size=lambda: len(data), read=lambda offset, length: data[offset-1:offset-1+length])

        with tempfile.TemporaryDirectory() as directory:
            uploads = Path(directory) / 'uploads'
            for job_id in [123, 124]:
                cursor.__iter__.return_value = iter([(2, blob(back_bytes)), (1, blob(front_bytes))])
                front, back = worker.read_images(conn, 'same-client', uploads, job_id=job_id)
                self.assertEqual(front.name, f'{job_id}_1.jpg')
                self.assertEqual(back.name, f'{job_id}_2.png')
                self.assertEqual(front.read_bytes(), front_bytes)
                self.assertEqual(back.read_bytes(), back_bytes)
            self.assertEqual(len(list(uploads.iterdir())), 4)

    def test_persistent_inference_passes_output_and_job_id_without_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'results'

            def infer(front, back, output, job_id=None):
                evidence = Path(output) / f'document_{job_id}_test'
                evidence.mkdir()
                (evidence / 'result.json').write_text('{}')
                return inference.OcrOutput('Success', .8, {'name': 'test'})

            with patch.object(inference, 'validate_image'), \
                 patch.object(inference, '_infer_pair', side_effect=infer) as mocked:
                inference.run_ocr_paths(Path('123_1.jpg'), Path('123_2.jpg'), output=root, job_id=123)
            self.assertTrue((root / 'document_123_test/result.json').is_file())
            self.assertEqual(mocked.call_args.kwargs, {'job_id': 123})

    def test_summary_json_is_readable_and_retained_for_failed_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            out = inference.OcrOutput('Unreadable', 0, details={'errorCode': 'INVALID_IMAGE_PAIR'})
            worker.write_job_output(root, 123, out)
            path = root / 'results/123.json'
            self.assertIsNone(json.loads(path.read_text(encoding='utf-8'))['databaseStatus'])
            worker.write_job_output(root, 123, out, 'OCR_FAIL')
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['databaseStatus'], 'OCR_FAIL')
            self.assertFalse(path.with_suffix('.json.tmp').exists())
            out = inference.OcrOutput('LowConfidence', .6, {'address': '\u0639\u0645\u0627\u0631\u0629 \u0662\u0665'})
            worker.write_job_output(root, 124, out, 'OCR_SUCCESS')
            text = (root / 'results/124.json').read_text(encoding='utf-8')
            self.assertIn(out.fields['address'], text)

    def test_image_signature_extensions(self):
        signatures = [(b'BM', '.bmp'), (b'II*\x00', '.tiff'), (b'MM\x00*', '.tiff'),
                      (b'RIFF1234WEBP', '.webp'), (b'invalid', '.bin')]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image'
            for data, suffix in signatures:
                path.write_bytes(data)
                self.assertEqual(worker.image_suffix(path), suffix)


if __name__ == '__main__':
    unittest.main()
