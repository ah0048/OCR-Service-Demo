"""Coordinate bounded async HTTP OCR requests; no model loading on this server."""
import argparse
from datetime import datetime
import getpass
import json
import logging
import math
import os
from pathlib import Path
import tempfile
import threading
import time
from uuid import UUID, uuid4

from oracle_config import load_environment

load_environment()

from inference import OcrOutput, ROOT
from ocr_http_client import (ApiInvalidImage, ApiMissing, ApiProtocolError,
                             ApiUnavailable, OcrClient, uuid_text)
from oracle_logging import capture_console
from ocr_log_retention import add_log_arguments, log_arguments
from oracle_recovery import (OracleRecoveryExhausted, RetryBudget, close_connection, safe_error_code,
                              transient_database_error)
from oracle_requests import (claim_request, clear_ack, inspect_request,
                              mark_submitted, outstanding)
from oracle_worker import (SOURCE, TABLE, connect, install_shutdown_handlers,
                           load_column_storage, read_images, save_result, wait_for_stop)

log = logging.getLogger('oracle-coordinator')
TERMINAL = ('OCR_SUCCESS', 'OCR_FAIL', 'FINISHED')


def decode_result(data):
    if not isinstance(data, dict) or data.get('ocrOutcome') not in (
            'Success', 'LowConfidence', 'Rejected', 'Unreadable', 'TechnicalError'):
        raise ApiProtocolError('INVALID_OCR_RESULT')
    confidence = data.get('confidence')
    if confidence is not None and (type(confidence) not in (int, float) or
                                   not math.isfinite(confidence) or not 0 <= confidence <= 1):
        raise ApiProtocolError('INVALID_OCR_CONFIDENCE')
    fields, details = data.get('extractedFields'), data.get('details')
    if ((fields is not None and not isinstance(fields, dict)) or
            (details is not None and not isinstance(details, dict))):
        raise ApiProtocolError('INVALID_OCR_FIELDS')
    try:
        json.dumps(data, allow_nan=False)
    except (ValueError, TypeError):
        raise ApiProtocolError('INVALID_OCR_JSON') from None
    return OcrOutput(data['ocrOutcome'], confidence, fields, details)


class PendingResults:
    """Atomic result files survive coordinator restarts; never age-delete these."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def path(self, job_id, request_id):
        return self.directory / f'{int(job_id)}_{UUID(request_id)}.json'

    def write(self, job, out):
        data = {'version': 1, 'jobId': job.job_id, 'requestId': job.request_id,
                'result': {'ocrOutcome': out.outcome, 'confidence': out.confidence,
                           'extractedFields': out.fields, 'details': out.details}}
        target = self.path(job.job_id, job.request_id)
        temporary = target.with_suffix('.' + uuid4().hex + '.tmp')
        try:
            with temporary.open('x', encoding='utf-8') as stream:
                json.dump(data, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)

    def read(self, job):
        return self._read(self.path(job.job_id, job.request_id))[2]

    def _read(self, path):
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or type(data.get('jobId')) is not int or data['jobId'] <= 0:
            raise ValueError('INVALID_PENDING_RECORD')
        request_id = uuid_text(data.get('requestId'))
        if path != self.path(data['jobId'], request_id):
            raise ValueError('PENDING_FILENAME_MISMATCH')
        return data['jobId'], request_id, decode_result(data.get('result'))

    def records(self):
        return [self._read(path) for path in sorted(self.directory.glob('*.json'))]

    def exists(self, job):
        return self.path(job.job_id, job.request_id).exists()

    def remove(self, job):
        self.path(job.job_id, job.request_id).unlink(missing_ok=True)


class Coordinator:
    def __init__(self, conn, driver, api, pending, storage, max_outstanding=3, threshold=.5):
        self.conn, self.driver, self.api = conn, driver, api
        self.pending, self.storage = pending, storage
        self.max_outstanding, self.threshold = max_outstanding, threshold
        self.jobs = {}

    def restore(self):
        for job in outstanding(self.conn):
            uuid_text(job.request_id)
            self.jobs[job.request_id] = job
        for job_id, request_id, _ in self.pending.records():
            job = inspect_request(self.conn, job_id)
            if job is None or job.request_id != request_id:
                raise RuntimeError('PENDING_RESULT_OWNERSHIP_CHANGED')
            self.jobs[request_id] = job
        log.info('Restored outstanding requests=%s; model loading is disabled', len(self.jobs))

    def _current(self, job):
        current = inspect_request(self.conn, job.job_id)
        if current is None or current.request_id != job.request_id:
            raise RuntimeError('REQUEST_OWNERSHIP_CHANGED')
        return current

    def _acknowledge(self, job):
        try:
            self.api.acknowledge(job.request_id, job.job_id)
        except ApiMissing:
            # The DB result is already confirmed; a lost Spark result needs no replay.
            log.info('Committed request absent on Spark; clearing acknowledgement job=%s request=%s',
                     job.job_id, job.request_id)
        clear_ack(self.conn, job)
        self.pending.remove(job)
        self.jobs.pop(job.request_id, None)
        log.info('Acknowledged and released job=%s request=%s', job.job_id, job.request_id)

    def _persist(self, job, out):
        current = self._current(job)
        if current.status in TERMINAL and current.completed:
            # Covers a committed UPDATE whose confirmation or ACK was lost.
            log.info('Result already committed job=%s request=%s status=%s',
                     job.job_id, job.request_id, current.status)
        elif current.status == 'UNDER_PROCESSING':
            status = save_result(self.conn, self.driver, job.job_id, out, self.threshold,
                                 self.storage, request_id=job.request_id)
            log.info('Committed job=%s request=%s status=%s', job.job_id, job.request_id, status)
        else:
            raise RuntimeError('UNEXPECTED_ORACLE_JOB_STATE')
        self._acknowledge(job)

    def _submit(self, job, missing_instance):
        if job.instance_id is not None and job.instance_id == missing_instance:
            # Unexpected loss within one API instance needs investigation, not blind OCR replay.
            raise ApiProtocolError('REQUEST_MISSING_WITHOUT_API_RESTART')
        if job.instance_id:
            log.warning('Spark restarted; resubmitting job=%s request=%s', job.job_id, job.request_id)
        try:
            with tempfile.TemporaryDirectory(prefix='ocr-upload-') as directory:
                front, back = read_images(self.conn, job.nin, Path(directory), job_id=job.job_id)
                data = self.api.submit(job.request_id, job.job_id, front, back)
        except ValueError as exc:
            allowed = {'DUPLICATE_SOURCE_IMAGE', 'MISSING_OR_OVERSIZED_IMAGE',
                       'INCOMPLETE_IMAGE_BLOB', 'MISSING_IMAGE_PAIR'}
            code = str(exc)
            if code not in allowed:
                raise
            out = OcrOutput('Unreadable', 0.0, details={'errorCode': code})
            self.pending.write(job, out)
            self._persist(job, out)
            return None
        except ApiInvalidImage as exc:
            out = OcrOutput('Unreadable', 0.0, details={'errorCode': exc.code})
            self.pending.write(job, out)
            self._persist(job, out)
            return None
        if data['status'] == 'ACKNOWLEDGED':
            raise ApiProtocolError('ACKNOWLEDGED_BEFORE_ORACLE_COMMIT')
        log.info('Submitted job=%s request=%s instance=%s',
                 job.job_id, job.request_id, data['serviceInstanceId'])
        return data

    def advance(self, job):
        if self.pending.exists(job):
            self._persist(job, self.pending.read(job))
            return
        current = self._current(job)
        if current.status in TERMINAL and current.completed:
            self._acknowledge(current)
            return
        if current.status != 'UNDER_PROCESSING':
            raise RuntimeError('UNEXPECTED_ORACLE_JOB_STATE')
        job = current
        try:
            data = self.api.get(job.request_id, job.job_id)
        except ApiMissing as exc:
            data = self._submit(job, exc.instance_id)
            if data is None:
                return
        if data['status'] == 'COMPLETED':
            out = decode_result(data.get('result'))
        elif data['status'] == 'FAILED':
            out = OcrOutput('TechnicalError', details={'errorCode': 'REMOTE_INFERENCE_ERROR'})
        elif data['status'] == 'ACKNOWLEDGED':
            raise ApiProtocolError('ACKNOWLEDGED_BEFORE_ORACLE_COMMIT')
        else:
            if job.instance_id != data['serviceInstanceId']:
                mark_submitted(self.conn, job, data['serviceInstanceId'])
            return
        # Protect a collected result before even the submission-tracking UPDATE.
        self.pending.write(job, out)
        if job.instance_id != data['serviceInstanceId']:
            mark_submitted(self.conn, job, data['serviceInstanceId'])
        self._persist(job, out)

    def cycle(self, allow_claim=True, stop_claims=lambda: False):
        for job in list(self.jobs.values()):
            self.advance(job)
        if not allow_claim or stop_claims() or len(self.jobs) >= self.max_outstanding:
            return
        self.api.ready()  # Never claim a backlog while the API/tunnel is unavailable.
        # Bound each pass even if invalid/very fast jobs finish immediately.
        for _ in range(self.max_outstanding - len(self.jobs)):
            if stop_claims():
                break
            job = claim_request(self.conn, self.driver)
            if job is None:
                break
            self.jobs[job.request_id] = job
            log.info('Claimed job=%s request=%s outstanding=%s',
                     job.job_id, job.request_id, len(self.jobs))
            self.advance(job)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn', default=os.getenv('ORACLE_DSN'))
    parser.add_argument('--user', default=os.getenv('ORACLE_USER', 'BRK_COD'))
    parser.add_argument('--client-lib-dir', default=os.getenv('ORACLE_CLIENT_LIB_DIR'))
    parser.add_argument('--thin', action='store_true')
    parser.add_argument('--api-url', default=os.getenv('OCR_API_URL', 'http://127.0.0.1:18001'))
    parser.add_argument('--max-outstanding', type=int, default=3)
    parser.add_argument('--poll-seconds', type=float, default=1)
    parser.add_argument('--retry-seconds', type=float, default=5)
    parser.add_argument('--db-retries', type=int, default=3,
                        help='Reconnect attempts per consecutive Oracle failure episode (default: 3)')
    parser.add_argument('--db-retry-seconds', type=float, default=2)
    parser.add_argument('--db-retry-max-seconds', type=float, default=30)
    parser.add_argument('--http-timeout', type=float, default=30)
    parser.add_argument('--drain-seconds', type=float, default=60)
    parser.add_argument('--confidence-threshold', type=float, default=.5)
    parser.add_argument('--pending-dir', type=Path, default=ROOT / '.data/coordinator/pending-results')
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument('--check-connection', action='store_true')
    checks.add_argument('--check-api', action='store_true')
    add_log_arguments(parser)
    args = parser.parse_args()
    try:
        log_arguments(args)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.check_api and not args.dsn:
        parser.error('Set ORACLE_DSN or pass --dsn')
    if not 1 <= args.max_outstanding <= 64:
        parser.error('--max-outstanding must be 1..64')
    if not 0 <= args.db_retries <= 20:
        parser.error('--db-retries must be 0..20')
    for name in ('poll_seconds', 'retry_seconds', 'http_timeout', 'drain_seconds',
                 'db_retry_seconds', 'db_retry_max_seconds'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            parser.error(name.replace('_', '-') + ' must be positive and finite')
    if not math.isfinite(args.confidence_threshold) or not 0 <= args.confidence_threshold <= 1:
        parser.error('--confidence-threshold must be between 0 and 1')
    args.token = os.getenv('OCR_API_TOKEN', '')
    if not args.check_connection and not args.token:
        parser.error('Set OCR_API_TOKEN')
    args.password = None if args.check_api else (os.getenv('ORACLE_PASSWORD') or getpass.getpass('Oracle password: '))
    return args


def run_database(args, api):
    shutdown, stopped = install_shutdown_handlers(), threading.Event()
    budget = RetryBudget(args.db_retries, args.db_retry_seconds, args.db_retry_max_seconds)
    deadline, unavailable = None, False
    pending = None if args.check_connection else PendingResults(args.pending_dir)
    while True:
        conn = None
        if shutdown.requested:
            log.info('Shutdown requested while disconnected; tracked jobs and pending results retained')
            return
        try:
            conn, driver = connect(args)
            with conn.cursor() as cur:
                cur.execute(f'SELECT OCR_REQUEST_ID, OCR_SERVICE_INSTANCE_ID, OCR_SUBMITTED_DATE, '
                            f'OCR_ACK_PENDING, EXTRACTED_JSON, OCR_DETAILS_JSON FROM {TABLE} WHERE 1=0')
                cur.execute(f'SELECT NIN_COD, FILE_NUMBER, DOC FROM {SOURCE} WHERE 1=0')
            storage = load_column_storage(conn)
            if args.check_connection:
                log.info('Oracle coordinator schema checks passed; database=%s', conn.version)
                return
            coordinator = Coordinator(conn, driver, api, pending,
                                      storage, args.max_outstanding, args.confidence_threshold)
            coordinator.restore()
            if budget.used:
                log.info('Oracle reconnected; reloaded tracked requests=%s; reconciling before new claims',
                         len(coordinator.jobs))
            log.info('Coordinator ready; maxOutstanding=%s pollSeconds=%s pendingDir=%s; no local OCR models',
                     args.max_outstanding, args.poll_seconds, args.pending_dir)
            while True:
                if shutdown.requested and deadline is None:
                    deadline = time.monotonic() + args.drain_seconds
                    log.info('Shutdown requested; stopping claims and draining outstanding requests')
                if deadline is not None and (not coordinator.jobs or time.monotonic() >= deadline):
                    break
                try:
                    coordinator.cycle(allow_claim=deadline is None,
                                      stop_claims=lambda: shutdown.requested)
                    if budget.used:
                        log.info('Oracle recovery confirmed; database retry budget reset')
                        budget.recovered()
                    if unavailable:
                        log.info('OCR API connection recovered')
                    unavailable = False
                    delay = args.poll_seconds
                except ApiUnavailable:
                    if not unavailable:
                        log.warning('OCR API/tunnel unavailable or busy; retaining requests and pausing claims')
                    unavailable, delay = True, args.retry_seconds
                # Use a fresh local request so draining does not spin after Ctrl+C.
                wait_for_stop(stopped, shutdown if deadline is None else type(shutdown)(), delay)
            log.info('Coordinator stopped; resumable outstanding requests=%s', len(coordinator.jobs))
            return
        except Exception as exc:
            if not transient_database_error(exc):
                raise
            # Discard this session before waiting. Rebuild state on the next one,
            # including claims/commits which may have succeeded without a reply.
            close_connection(conn)
            conn = None
            if shutdown.requested:
                log.info('Oracle unavailable during shutdown; tracked jobs and pending results retained')
                return
            try:
                delay = budget.next_delay()
            except OracleRecoveryExhausted:
                log.error('Oracle reconnect budget exhausted code=%s retries=%s; '
                          'tracked jobs and pending results retained', safe_error_code(exc), budget.used)
                raise
            log.warning('Oracle connection failed code=%s; paused claims; reconnectAttempt=%s/%s delaySeconds=%.2f',
                        safe_error_code(exc), budget.used, budget.retries, delay)
            wait_for_stop(stopped, shutdown, delay)
        finally:
            close_connection(conn)


def run(args):
    api = None
    try:
        if not args.check_connection:
            api = OcrClient(args.api_url, args.token, timeout=args.http_timeout)
        if args.check_api:
            data = api.ready()
            log.info('OCR API ready; instance=%s', data['serviceInstanceId'])
            return
        run_database(args, api)
    finally:
        if api is not None:
            api.close()


def main():
    args = parse_args()
    pid = os.getpid()
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    with capture_console('coordinator', pid, run_id, prefix='oracle_coordinator',
                         **log_arguments(args)) as logfile:
        log.info('Coordinator launcher started; launcher PID=%s; log=%s', pid, logfile)
        try:
            run(args)
        except Exception as exc:
            code = str(exc) if isinstance(exc, (ApiProtocolError, RuntimeError)) else ''
            if not code or len(code) > 80 or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for c in code):
                code = 'CHECK_CONFIGURATION_OR_DATABASE'
            oracle_code = getattr(exc.args[0], 'code', None) if exc.args else None
            log.error('Coordinator stopped on error; errorType=%s errorCode=%s; '
                      'oracleCode=%s; inspect tracking and pending results before restart',
                      type(exc).__name__, code, oracle_code if type(oracle_code) is int else None)
            raise SystemExit(1) from None
        finally:
            log.info('Coordinator launcher stopped; launcher PID=%s', pid)


if __name__ == '__main__':
    main()
