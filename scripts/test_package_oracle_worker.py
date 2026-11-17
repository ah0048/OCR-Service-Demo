"""Protect the transfer allowlist, complete model assets and checksum manifest."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from package_oracle_worker import (CHECKPOINTS, MODEL_DIRECTORY, MODEL_REQUIRED,
                                   RUNTIME_MODULES, collect_files, create_archive, verify_archive)


class PackageTests(unittest.TestCase):
    def fixture(self, root):
        names = list(RUNTIME_MODULES) + ['README.md', '.env.example', '.gitignore',
                                       'try1/ASSETS.md', 'try1/CUDA_README.md',
                                       'backend/ocr-worker/oracle_worker.py',
                                       'backend/ocr-worker/requirements.oracle.txt',
                                       'backend/ocr-worker/ocr_api.py',
                                       'backend/ocr-worker/requirements.api.txt',
                                       'backend/ocr-worker/requirements.api-test.txt',
                                       'backend/ocr-worker/requirements.coordinator.txt',
                                       'backend/ocr-worker/oracle_coordinator.py',
                                       'backend/ocr-worker/ASYNC_API.md',
                                       'backend/ocr-worker/ORACLE_COORDINATOR.md',
                                       'database/oracle/005_async_request_tracking.sql',
                                       'database/oracle/001_create_client_image_ocr_job.sql',
                                       'scripts/package_oracle_worker.py']
        names += list(CHECKPOINTS)
        names += [f'{MODEL_DIRECTORY}/{name}' for name in MODEL_REQUIRED]
        for name in names:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic test fixture')
        for name in ['.env', 'backend/ocr-worker/logs/worker.log',
                     'backend/ocr-worker/.venv/secret.py', 'try1/.git/config',
                     'try1/outputs/oracle_workers/uploads/client.jpg', '.data/images/client.jpg',
                     f'{MODEL_DIRECTORY}/.cache/private-token']:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('DO_NOT_TRANSFER')

    def test_archive_includes_required_assets_but_excludes_secrets_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            root = Path(directory)
            self.fixture(root)
            destination = root / 'package.tar.gz'
            created = create_archive(root, destination)
            checked = verify_archive(destination)
            self.assertEqual(created, checked)
            names = set(checked['files'])
            self.assertTrue(set(CHECKPOINTS).issubset(names))
            self.assertIn('.env.example', names)
            self.assertIn('backend/ocr-worker/requirements.api.txt', names)
            self.assertIn('backend/ocr-worker/ASYNC_API.md', names)
            self.assertIn('backend/ocr-worker/requirements.coordinator.txt', names)
            self.assertIn('database/oracle/005_async_request_tracking.sql', names)
            self.assertNotIn('.env', names)
            self.assertFalse(any('/outputs/' in name or '/.git/' in name or '/logs/' in name
                                 or '/.venv/' in name or '/.cache/' in name for name in names))
            self.assertTrue(destination.with_name('package.tar.gz.sha256').is_file())
            with self.assertRaises(FileExistsError):
                create_archive(root, destination)

    def test_missing_weights_and_lfs_pointers_fail_before_packaging(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            weight = root / CHECKPOINTS[0]
            weight.unlink()
            with self.assertRaises(FileNotFoundError):
                collect_files(root)
            weight.write_text('version https://git-lfs.github.com/spec/v1\noid sha256:test')
            with self.assertRaisesRegex(ValueError, 'Git LFS pointer'):
                collect_files(root)

    def test_code_only_is_explicit_and_contains_no_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            files = collect_files(root, include_models=False)
            self.assertFalse(any(path.suffix in ('.pt', '.safetensors') for path in files))

    def test_verification_rejects_tampered_content(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tampered.tar.gz'
            manifest = {'format': 1, 'files': {'README.md': {'size': 4, 'sha256': 'wrong'}}}
            with tarfile.open(path, 'w:gz') as archive:
                for name, body in [('README.md', b'test'),
                                   ('TRANSFER_MANIFEST.json', json.dumps(manifest).encode())]:
                    info = tarfile.TarInfo(name)
                    info.size = len(body)
                    archive.addfile(info, io.BytesIO(body))
            with self.assertRaises(ValueError):
                verify_archive(path)


if __name__ == '__main__':
    unittest.main()
