"""File/terminal mirroring and launcher metadata without Oracle or model loading."""
from contextlib import redirect_stderr, redirect_stdout
import io
import logging
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_logging
import oracle_worker


def emit_child_log(directory, launcher_pid, run_id):
    with oracle_logging.capture_console('worker', launcher_pid, run_id, directory):
        logging.getLogger('oracle-ocr').info('test worker PID=%s', os.getpid())
        print('test progress 100%', file=sys.stderr)


class OracleLoggingTests(unittest.TestCase):
    def test_retained_stream_can_write_after_capture_has_closed(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stdout), redirect_stderr(stderr):
            with oracle_logging.capture_console('launcher', 123, 'late', directory) as path:
                retained = sys.stdout
                print('before close')
            retained.write('late console reset')
            retained.flush()
            self.assertIn('late console reset', stdout.getvalue())
            self.assertIn('before close', path.read_text(encoding='utf-8'))
            self.assertNotIn('late console reset', path.read_text(encoding='utf-8'))

    def test_colorama_atexit_reset_has_no_closed_file_error(self):
        with tempfile.TemporaryDirectory() as directory:
            script = (
                'import sys\n'
                f'sys.path.insert(0, {str(Path(__file__).resolve().parent)!r})\n'
                'from oracle_logging import capture_console\n'
                f'with capture_console("launcher", 123, "colorama", {directory!r}):\n'
                '    import colorama\n'
                '    colorama.init()\n'
                '    print("colorama test")\n'
            )
            result = subprocess.run([sys.executable, '-c', script], capture_output=True,
                                    text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('Exception ignored in atexit', result.stderr)
            self.assertNotIn('closed file', result.stderr)

    def test_console_logs_prints_and_progress_are_mirrored_as_utf8(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        root = logging.getLogger()
        old_handlers, old_level = root.handlers[:], root.level
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(stdout), redirect_stderr(stderr):
            with oracle_logging.capture_console('launcher', 123, 'test', directory) as path:
                logging.getLogger('oracle-ocr').info('Claimed job=79')
                print('\u0639\u0645\u0627\u0631\u0629 \u0662\u0665')
                print('\rLoading weights: 100%', file=sys.stderr)
            text = path.read_text(encoding='utf-8')
            self.assertIn('Claimed job=79', text)
            self.assertIn('Claimed job=79', stderr.getvalue())
            self.assertIn('launcherPid=123 role=launcher', text)
            self.assertIn(f'pid={os.getpid()}', text)
            self.assertIn('\u0639\u0645\u0627\u0631\u0629 \u0662\u0665', text)
            self.assertIn('Loading weights: 100%', text)
            self.assertIn('Loading weights: 100%', stderr.getvalue())
            self.assertIn('\u0639\u0645\u0627\u0631\u0629', stdout.getvalue())
        self.assertEqual(root.handlers, old_handlers)
        self.assertEqual(root.level, old_level)

    def test_exception_is_retained_before_interpreter_prints_it(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, 'test failure'):
                with oracle_logging.capture_console('launcher', 123, 'error', directory) as path:
                    raise RuntimeError('test failure')
            text = path.read_text(encoding='utf-8')
            self.assertIn('Traceback', text)
            self.assertIn('RuntimeError: test failure', text)

    def test_two_processes_write_separate_files_with_same_launcher_id(self):
        ctx = multiprocessing.get_context('spawn')
        launcher_pid = os.getpid()
        with tempfile.TemporaryDirectory() as directory:
            processes = [ctx.Process(target=emit_child_log, args=(directory, launcher_pid, 'spawn')) for _ in range(2)]
            try:
                for process in processes:
                    process.start()
                for process in processes:
                    process.join(timeout=15)
                self.assertEqual([p.exitcode for p in processes], [0, 0])
                files = list(Path(directory).glob('*.log'))
                self.assertEqual(len(files), 2)
                for process in processes:
                    path = Path(directory) / f'oracle_spawn_worker_{process.pid}.log'
                    text = path.read_text(encoding='utf-8')
                    self.assertIn(f'launcherPid={launcher_pid}', text)
                    self.assertIn(f'pid={process.pid}', text)
                    self.assertIn('test progress 100%', text)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.kill()
                    if process.pid is not None:
                        process.join(timeout=2)

    def test_single_worker_launcher_logs_its_pid_and_stop(self):
        from types import SimpleNamespace
        args = SimpleNamespace(workers=1, check_connection=False)
        stdout, stderr = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            capture = oracle_logging.capture_console

            def temporary_capture(role, launcher_pid, run_id):
                return capture(role, launcher_pid, run_id, directory)

            with patch.object(oracle_worker, 'parse_args', return_value=args), \
                 patch.object(oracle_worker, 'capture_console', side_effect=temporary_capture), \
                 patch.object(oracle_worker, '_worker_main'), \
                 redirect_stdout(stdout), redirect_stderr(stderr):
                oracle_worker.main()
            files = list(Path(directory).glob('*.log'))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding='utf-8')
            for message in [f'Launcher started; launcher PID={os.getpid()}',
                            f'Worker started; worker PID={os.getpid()}',
                            f'Launcher stopped; launcher PID={os.getpid()}']:
                self.assertIn(message, text)
                self.assertIn(message, stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
