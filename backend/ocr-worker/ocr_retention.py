"""Cleanup only explicitly acknowledged OCR artifacts, never pending results."""
import json
import logging
import math
from pathlib import Path
import shutil
import threading
import time
from uuid import UUID

log = logging.getLogger('ocr-api')
MARKER = '.ocr-acknowledged.json'


def uuid_directory(path):
    try:
        return (not path.is_symlink() and path.is_dir() and
                str(UUID(path.name)) == path.name and
                not (hasattr(path, 'is_junction') and path.is_junction()))
    except (ValueError, OSError):
        return False


class ArtifactRetention:
    def __init__(self, root, hours=168, interval=60, min_free_mb=1024):
        if (not math.isfinite(hours) or hours < 0 or not math.isfinite(interval) or interval <= 0 or
                type(min_free_mb) is not int or min_free_mb < 0):
            raise ValueError('Retention must be finite and nonnegative; interval positive; disk reserve nonnegative')
        self.root = Path(root).resolve()
        self.hours, self.interval = hours, interval
        self.min_free_bytes = min_free_mb * 1024 * 1024
        self._stop = threading.Event()
        self._thread = None

    def disk(self):
        free = shutil.disk_usage(self.root).free
        return {'freeBytes': free, 'minimumFreeBytes': self.min_free_bytes,
                'acceptingUploads': free >= self.min_free_bytes}

    def acknowledge(self, directory):
        directory = Path(directory)
        if not uuid_directory(directory) or not uuid_directory(directory.parent):
            raise ValueError('Invalid artifact directory')
        if directory.resolve().parent.parent != self.root:
            raise ValueError('Artifact directory outside output root')
        data = {'version': 1, 'requestId': directory.name,
                'serviceInstanceId': directory.parent.name, 'acknowledgedAt': time.time()}
        temporary = directory / (MARKER + '.tmp')
        temporary.write_text(json.dumps(data), encoding='utf-8')
        temporary.replace(directory / MARKER)

    def sweep(self, now=None):
        if self.hours == 0:  # Explicit diagnostic opt-out.
            return 0
        cutoff = (time.time() if now is None else now) - self.hours * 3600
        removed = 0
        for instance in self.root.iterdir():
            if not uuid_directory(instance):
                continue
            try:
                requests = list(instance.iterdir())
            except OSError:
                log.warning('Artifact retention could not scan a service instance directory')
                continue
            for request in requests:
                if not uuid_directory(request):
                    continue
                marker = request / MARKER
                try:
                    if marker.is_symlink():
                        continue
                    data = json.loads(marker.read_text(encoding='utf-8'))
                    stamp = data.get('acknowledgedAt')
                    if (data.get('version') != 1 or data.get('requestId') != request.name or
                            data.get('serviceInstanceId') != instance.name or
                            type(stamp) not in (int, float) or not math.isfinite(stamp) or
                            stamp <= 0 or stamp > cutoff):
                        continue
                    if request.resolve().parent.parent != self.root:
                        continue
                    shutil.rmtree(request)
                    removed += 1
                except FileNotFoundError:
                    continue
                except (OSError, ValueError, TypeError, AttributeError):
                    log.warning('Artifact retention skipped an unreadable/invalid acknowledgement or directory')
            # Empty instance directories are harmless; leave the live instance intact.
        if removed:
            log.info('Artifact retention removed acknowledged request directories=%s', removed)
        return removed

    def start(self):
        self.sweep()
        if self.hours == 0:
            return
        self._thread = threading.Thread(target=self._run, name='ocr-artifact-retention', daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self.sweep()
            except OSError:
                log.warning('Artifact retention could not scan output root')

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
