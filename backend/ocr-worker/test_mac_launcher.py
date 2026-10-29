"""Exercise real pool process lifecycle using lightweight workers on macOS/Linux."""
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash'), 'Requires POSIX process signals and bash')
class MacLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.script = self.root / 'backend/scripts/mac-worker.sh'
        self.script.parent.mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / 'scripts/mac-worker.sh'
        self.script.write_text(source.read_text())
        self.logs = self.root / '.data/logs'
        self.logs.mkdir(parents=True)
        worker = self.root / 'backend/ocr-worker/worker.py'
        worker.parent.mkdir(parents=True)
        worker.write_text('import signal,time,sys\nsignal.signal(signal.SIGTERM,lambda *_: sys.exit(0))\nprint("READY",flush=True)\nwhile True: time.sleep(0.05)\n')
        self.worker = worker
        python = worker.parent / '.venv/bin/python'
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\nif [ "$1" = - ]; then cat >/dev/null; echo "CHECK OK"; exit 0; fi\nexec ' + shlex.quote(sys.executable) + ' "$@"\n')
        python.chmod(0o755)

    def tearDown(self):
        self.run_script('stop', check=False)
        self.wait_count(0)
        self.temp.cleanup()

    def run_script(self, *args, check=True):
        environment = os.environ.copy()
        environment.pop('OCR_WORKERS', None)
        return subprocess.run(['bash', str(self.script), *args], capture_output=True, text=True,
                              timeout=15, check=check, env=environment)

    def count(self):
        text = self.run_script('status').stdout
        return int(next(line.rsplit(':', 1)[1] for line in text.splitlines() if line.startswith('Running Mac OCR workers:')))

    def wait_count(self, expected):
        for _ in range(60):
            if self.count() == expected:
                return
            time.sleep(0.05)
        self.fail(f'Pool did not reach {expected} running workers')

    def test_default_one_idempotent_start_configurable_scale_up_and_down(self):
        self.run_script('start')
        self.wait_count(1)
        pids = [p.read_text() for p in sorted(self.logs.glob('mac-ocr-*.pid'))]
        self.run_script('start')
        self.assertEqual(pids, [p.read_text() for p in sorted(self.logs.glob('mac-ocr-*.pid'))])
        self.run_script('start', '3')
        self.wait_count(3)
        self.assertEqual(len(list(self.logs.glob('mac-ocr-*.log'))), 3)
        self.run_script('start', '1')
        self.wait_count(1)
        self.run_script('stop')
        self.wait_count(0)

    def test_invalid_count_never_starts_workers(self):
        for value in ('0', '-1', '33', '01', '3;echo', '999999999999999999999999'):
            with self.subTest(value=value):
                self.assertEqual(self.run_script('start', value, check=False).returncode, 2)
        self.assertEqual(self.count(), 0)

    def test_stale_pid_cannot_stop_an_unrelated_process(self):
        (self.logs / 'mac-ocr-1.pid').write_text(str(os.getpid()))
        self.run_script('stop')
        self.assertEqual(self.count(), 0)
        self.run_script('start', '1')
        self.wait_count(1)
        self.assertNotEqual(int((self.logs / 'mac-ocr-1.pid').read_text()), os.getpid())

    def test_legacy_worker_requires_graceful_migration(self):
        legacy = subprocess.Popen([sys.executable, str(self.worker)], stdout=subprocess.DEVNULL)
        try:
            (self.logs / 'mac-ocr.pid').write_text(str(legacy.pid))
            time.sleep(0.1)
            self.assertEqual(self.run_script('start', check=False).returncode, 1)
            self.assertEqual(self.count(), 1)
            self.run_script('stop')
            legacy.wait(timeout=5)
            self.run_script('start', '1')
            self.wait_count(1)
        finally:
            if legacy.poll() is None:
                legacy.terminate()
                legacy.wait(timeout=5)

    def test_lock_rejects_overlapping_pool_commands(self):
        lock = self.logs / 'mac-ocr-launch.lock'
        lock.mkdir()
        try:
            self.assertEqual(self.run_script('start', check=False).returncode, 1)
            self.assertEqual(self.count(), 0)
        finally:
            lock.rmdir()
