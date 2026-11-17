"""Mirror each Oracle process's Python console output to its own UTF-8 log."""
from contextlib import contextmanager
import logging
import os
from pathlib import Path
import sys
import threading
import traceback
from ocr_log_retention import ConsoleLogFile

LOG_DIRECTORY = Path(__file__).resolve().parent / 'logs'


class ConsoleTee:
    def __init__(self, console, logfile, lock):
        self.console = console
        self.logfile = logfile
        self.lock = lock

    def write(self, text):
        with self.lock:
            if self.logfile is not None and not self.logfile.closed:
                self.logfile.write(text)
                self.logfile.flush()
            return self.console.write(text)

    def flush(self):
        with self.lock:
            if self.logfile is not None and not self.logfile.closed:
                self.logfile.flush()
            self.console.flush()

    def detach_logfile(self):
        # Colorama may retain this stream for its later atexit reset callback.
        with self.lock:
            self.logfile = None

    def __getattr__(self, name):
        # Preserve terminal detection/encoding for progress bars and libraries.
        return getattr(self.console, name)


@contextmanager
def capture_console(role, launcher_pid, run_id, directory=LOG_DIRECTORY, prefix='oracle',
                    max_bytes=0, backups=3, keep_runs=20, days=7):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'{prefix}_{run_id}_{role}_{os.getpid()}.log'
    previous_stdout, previous_stderr = sys.stdout, sys.stderr
    root = logging.getLogger()
    previous_handlers, previous_level = root.handlers[:], root.level
    handler = None
    with ConsoleLogFile(path, max_bytes, backups, keep_runs, days, prefix) as logfile:
        lock = threading.RLock()
        stdout_tee = ConsoleTee(previous_stdout, logfile, lock)
        stderr_tee = ConsoleTee(previous_stderr, logfile, lock)
        sys.stdout, sys.stderr = stdout_tee, stderr_tee
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(
            f'%(asctime)s %(levelname)s pid=%(process)d launcherPid={launcher_pid} role={role} %(message)s'))
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        try:
            yield path
        except BaseException as exc:
            # The interpreter/multiprocessing bootstrap prints to the console
            # after this context exits; retain that exception in the file too.
            if isinstance(exc, SystemExit):
                if exc.code not in (None, 0):
                    logfile.write(str(exc.code) + '\n')
            else:
                traceback.print_exception(type(exc), exc, exc.__traceback__, file=logfile)
            raise
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            sys.stdout, sys.stderr = previous_stdout, previous_stderr
            root.handlers = previous_handlers
            root.setLevel(previous_level)
            handler.close()
            stdout_tee.detach_logfile()
            stderr_tee.detach_logfile()
