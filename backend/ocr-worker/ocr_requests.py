"""Bounded, process-local OCR requests. Oracle remains the durable job source."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
from pathlib import Path
import shutil
import threading
import time
from uuid import UUID, uuid4

log = logging.getLogger('ocr-api')


class RequestError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass
class Request:
    request_id: str
    job_id: int
    directory: Path
    status: str = 'UPLOADING'
    hashes: tuple | None = None
    result: dict | None = None
    error: str | None = None


class RequestStore:
    def __init__(self, root, concurrency, capacity, infer, validate, max_image_bytes,
                 retain_artifacts=True, tombstone_limit=1024, retention=None):
        if not 1 <= concurrency <= 8 or not concurrency <= capacity <= 64:
            raise ValueError('Concurrency must be 1..8; capacity must be concurrency..64')
        self.instance_id = str(uuid4())
        self.root = Path(root) / self.instance_id
        self.root.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.capacity, self.infer, self.validate = capacity, infer, validate
        self.max_image_bytes = max_image_bytes
        self.retain_artifacts, self.tombstone_limit = retain_artifacts, tombstone_limit
        self._lock = threading.RLock()
        self._requests, self._acknowledged = {}, OrderedDict()
        self._pool = ThreadPoolExecutor(concurrency, thread_name_prefix='ocr-job')
        # Direct library callers retain diagnostic behavior; the API supplies policy.
        self.retention = retention
        self.ready = True

    def _view(self, item):
        data = {'requestId': item.request_id, 'jobId': item.job_id,
                'status': item.status, 'serviceInstanceId': self.instance_id}
        if item.result is not None:
            data['result'] = item.result
        if item.error:
            data['errorCode'] = item.error
        return data

    def health(self):
        with self._lock:
            counts = {state: sum(r.status == state for r in self._requests.values())
                      for state in ('UPLOADING', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')}
            disk = self.retention.disk() if self.retention else None
            return {'ready': self.ready and (disk is None or disk['acceptingUploads']),
                    'serviceInstanceId': self.instance_id, 'storage': disk,
                    'capacity': self.capacity, 'outstanding': len(self._requests),
                    'requests': counts}

    def get(self, request_id):
        with self._lock:
            item = self._requests.get(request_id) or self._acknowledged.get(request_id)
            if item is None:
                raise RequestError(404, 'REQUEST_NOT_FOUND')
            return self._view(item)

    def _copy(self, source, destination):
        digest, size = hashlib.sha256(), 0
        with destination.open('xb') as out:
            while chunk := source.read(65536):
                size += len(chunk)
                if size > self.max_image_bytes:
                    raise RequestError(413, 'IMAGE_TOO_LARGE')
                digest.update(chunk)
                out.write(chunk)
        if not size:
            raise RequestError(422, 'EMPTY_IMAGE')
        return digest.hexdigest()

    def submit(self, request_id, job_id, front, back):
        request_id = str(UUID(request_id))
        with self._lock:
            if not self.ready:
                raise RequestError(503, 'SERVICE_STOPPING')
            if self.retention and not self.retention.disk()['acceptingUploads']:
                raise RequestError(503, 'LOW_DISK_SPACE')
            existing = self._requests.get(request_id) or self._acknowledged.get(request_id)
            if existing and existing.job_id != job_id:
                raise RequestError(409, 'REQUEST_ID_CONFLICT')
            if existing and existing.status == 'UPLOADING':
                raise RequestError(409, 'REQUEST_UPLOADING')
            if not existing and len(self._requests) >= self.capacity:
                raise RequestError(503, 'CAPACITY_EXHAUSTED')
            item = existing or Request(request_id, job_id, self.root / request_id)
            if not existing:
                self._requests[request_id] = item  # Reserve before copying/validation.
        # Duplicate comparisons use a separate staging directory, never live inputs.
        directory = self.root / ('upload_' + uuid4().hex) if existing else item.directory
        queued = False
        try:
            directory.mkdir(mode=0o700)
            uploads = directory / 'uploads'
            uploads.mkdir()
            paths = [uploads / f'{job_id}_{side}.image' for side in (1, 2)]
            hashes = tuple(self._copy(stream, path) for stream, path in zip((front, back), paths))
            if existing:
                with self._lock:
                    if hashes != item.hashes:
                        raise RequestError(409, 'REQUEST_ID_CONFLICT')
                    return self._view(item)
            from PIL import Image
            for path in paths:
                try:
                    self.validate(path)
                except (ValueError, OSError, Image.DecompressionBombError,
                        Image.DecompressionBombWarning):
                    raise RequestError(422, 'INVALID_IMAGE') from None
            # Keep the familiar job_side filenames with an actual image extension.
            for index, path in enumerate(paths):
                with Image.open(path) as image:
                    suffix = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp',
                              'BMP': '.bmp', 'TIFF': '.tiff'}[image.format]
                named = path.with_suffix(suffix)
                path.rename(named)
                paths[index] = named
            with self._lock:
                if not self.ready:
                    raise RequestError(503, 'SERVICE_STOPPING')
                item.hashes, item.status = hashes, 'QUEUED'
                self._pool.submit(self._run, item, *paths)
                queued = True
                log.info('Accepted job=%s request=%s status=QUEUED', job_id, request_id)
                return self._view(item)
        except BaseException:
            if not existing:
                with self._lock:
                    self._requests.pop(request_id, None)
            raise
        finally:
            if existing or not queued:
                shutil.rmtree(directory, ignore_errors=True)

    def _run(self, item, front, back):
        started = time.monotonic()
        with self._lock:
            item.status = 'RUNNING'
        log.info('Processing job=%s request=%s thread=%s', item.job_id,
                 item.request_id, threading.current_thread().name)
        try:
            out = self.infer(front, back, output=item.directory / 'results', job_id=item.job_id)
            result = asdict(out)
            # Preserve the existing public result contract and reject invalid JSON.
            result = {'ocrOutcome': result['outcome'], 'confidence': result['confidence'],
                      'extractedFields': result['fields'], 'details': result['details']}
            json.dumps(result, allow_nan=False)
            with self._lock:
                item.result, item.status = result, 'COMPLETED'
        except Exception as exc:
            with self._lock:
                item.status, item.error = 'FAILED', 'INFERENCE_ERROR'
            # Do not log extracted values or exception messages containing image data.
            log.error('Inference failed job=%s request=%s errorType=%s',
                      item.job_id, item.request_id, type(exc).__name__)
        log.info('Completed job=%s request=%s status=%s ms=%s thread=%s',
                 item.job_id, item.request_id, item.status,
                 int((time.monotonic() - started) * 1000), threading.current_thread().name)

    def acknowledge(self, request_id):
        with self._lock:
            if request_id in self._acknowledged:
                return self._view(self._acknowledged[request_id])
            item = self._requests.get(request_id)
            if item is None:
                raise RequestError(404, 'REQUEST_NOT_FOUND')
            if item.status not in ('COMPLETED', 'FAILED'):
                raise RequestError(409, 'REQUEST_NOT_COMPLETE')
            if self.retention:
                # Persist eligibility before releasing the in-memory result. A failed
                # marker write leaves the result available for an ACK retry.
                try:
                    self.retention.acknowledge(item.directory)
                except OSError:
                    log.warning('Could not mark acknowledgement; retaining result job=%s request=%s',
                                item.job_id, request_id)
                    raise RequestError(503, 'ACK_STORAGE_UNAVAILABLE') from None
            item.result, item.status = None, 'ACKNOWLEDGED'
            self._acknowledged[request_id] = self._requests.pop(request_id)
            while len(self._acknowledged) > self.tombstone_limit:
                self._acknowledged.popitem(last=False)
            view = self._view(item)
        if not self.retain_artifacts:
            try:
                shutil.rmtree(item.directory)
            except OSError:
                log.warning('Artifact cleanup failed job=%s request=%s', item.job_id, request_id)
        log.info('Acknowledged job=%s request=%s', item.job_id, request_id)
        return view

    def close(self):
        with self._lock:
            self.ready = False
        log.info('Shutdown requested; draining accepted OCR requests')
        self._pool.shutdown(wait=True)
        log.info('Inference threads stopped; outstanding=%s', len(self._requests))
