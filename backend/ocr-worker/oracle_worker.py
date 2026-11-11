"""Poll Oracle 11g jobs; job threads share models, never DB connections."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from datetime import date, datetime
import getpass
import json
import logging
import math
import multiprocessing
import os
from pathlib import Path
import signal
import threading
import time

from oracle_config import load_environment
from oracle_logging import capture_console

load_environment()  # OCR_DEVICE and model paths are read when inference is imported.

from inference import MAX_IMAGE_BYTES, MODEL_ROOT, OcrOutput, initialize, run_ocr_paths

log = logging.getLogger('oracle-ocr')
TABLE = 'BRK_COD.CLIENT_IMAGE_OCR_JOB'
SOURCE = 'CLIENTSOWNER.CLIENT_IMAGE_OCR'
OUTPUT_ROOT = Path(os.getenv('ORACLE_OUTPUT_ROOT', str(MODEL_ROOT / 'outputs/oracle_workers'))).resolve()

# Fetch inside PL/SQL so driver arraysize/prefetch cannot lock extra jobs.
# No FETCH FIRST syntax: it is not supported on Oracle 11g.
CLAIM_SQL = f"""DECLARE
  CURSOR jobs IS
    SELECT JOB_ID, NIN_CODE FROM {TABLE}
    WHERE STATUS = 'PENDING' ORDER BY INSERT_DATE, JOB_ID
    FOR UPDATE SKIP LOCKED;
BEGIN
  :job_id := NULL;
  :nin_code := NULL;
  OPEN jobs;
  FETCH jobs INTO :job_id, :nin_code;
  IF jobs%FOUND THEN
    UPDATE {TABLE} SET STATUS = 'UNDER_PROCESSING',
      STARTED_DATE = CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE)
    WHERE JOB_ID = :job_id;
  ELSE
    :job_id := NULL;
    :nin_code := NULL;
  END IF;
  CLOSE jobs;
END;
"""

TEXT_FIELDS = {
    'fullName': ('CLIENT_NAME', 100),
    'nationalId': ('ID_NUMBER', 20),
    'address': ('CLIENT_ADDRESS', 100),
    'occupation': ('JOB_DESC', 400),
    'spouseName': ('HUSBAND_NAME', 100),
    'maritalStatus': ('MARITAL_STATUS', 100),
    'religion': ('RELIGION', 100),
}


class ShutdownRequest:
    """Signal handlers must not acquire Event/Condition locks or perform logging."""
    def __init__(self):
        self.requested = False

    def handle(self, *_):
        self.requested = True


def install_shutdown_handlers():
    request = ShutdownRequest()
    for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), request.handle)
    return request


@contextmanager
def report_worker_shutdown(request, stop):
    """Acknowledge Ctrl+C outside the signal handler, even during inference."""
    finished = threading.Event()
    reported = False
    report_lock = threading.Lock()

    def notify():
        nonlocal reported
        if request.requested or stop.is_set():
            with report_lock:
                if not reported:
                    reported = True
                    log.info('Shutdown requested; finishing any active job before stopping')

    def report():
        while not finished.wait(0.1):
            if request.requested or stop.is_set():
                notify()
                return

    reporter = threading.Thread(target=report, name='oracle-shutdown-log', daemon=True)
    reporter.start()
    try:
        yield notify
    finally:
        finished.set()
        reporter.join()
        # Also cover a signal followed by a fast idle-worker exit before the poll.
        notify()


def wait_for_stop(stop, request, seconds):
    """Check local signals promptly without shortening the configured poll period."""
    deadline = time.monotonic() + seconds
    while not request.requested and not stop.is_set():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            # Windows Ctrl+C can interrupt multiprocessing.Condition.wait().
            # A short local sleep avoids that semaphore wait entirely; the
            # shared stop flag and local signal flag are checked every 0.2s.
            time.sleep(min(remaining, 0.2))
        except InterruptedError:
            continue  # Re-check shutdown; a keyboard interrupt is not a job failure.


def join_process(process, timeout=None):
    """Keyboard signals can interrupt a Windows process-handle wait as well."""
    while True:
        try:
            process.join(timeout=timeout)
            return
        except InterruptedError:
            if timeout is not None:
                return  # Let the supervisor observe the shutdown flag immediately.


def claim_job(conn, driver):
    try:
        with conn.cursor() as cur:
            job_id = cur.var(driver.DB_TYPE_NUMBER)
            nin = cur.var(driver.DB_TYPE_NUMBER)
            cur.execute(CLAIM_SQL, job_id=job_id, nin_code=nin)
            job = job_id.getvalue(), nin.getvalue()
        conn.commit()  # Never hold a row lock during inference.
        return None if job[0] is None else (int(job[0]), str(int(job[1])))
    except Exception:
        conn.rollback()
        raise


def image_suffix(path):
    """Name the original BLOB by its image signature, without re-encoding it."""
    with path.open('rb') as source:
        header = source.read(16)
    for signature, suffix in [(b'\xff\xd8\xff', '.jpg'), (b'\x89PNG\r\n\x1a\n', '.png'),
                              (b'BM', '.bmp'), (b'II*\x00', '.tiff'), (b'MM\x00*', '.tiff')]:
        if header.startswith(signature):
            return suffix
    return '.webp' if header[:4] == b'RIFF' and header[8:12] == b'WEBP' else '.bin'


def read_images(conn, nin, directory, job_id=None):
    """Read BLOB locators before closing the cursor/transaction; bound disk usage."""
    found = set()
    paths = {}
    directory.mkdir(parents=True, exist_ok=True)
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT FILE_NUMBER, DOC FROM {SOURCE} '
                        'WHERE NIN_COD = :nin AND FILE_NUMBER IN (1, 2)', nin=nin)
            for number, blob in cur:
                if number in found:
                    raise ValueError('DUPLICATE_SOURCE_IMAGE')
                found.add(number)
                if blob is None or not 0 < blob.size() <= MAX_IMAGE_BYTES:
                    raise ValueError('MISSING_OR_OVERSIZED_IMAGE')
                size = blob.size()
                name = f'{int(job_id)}_{int(number)}' if job_id is not None else ('front' if number == 1 else 'back')
                path = directory / name
                with path.open('wb') as output:
                    offset = 1  # Oracle LOB offsets are one-based.
                    while offset <= size:
                        chunk = blob.read(offset, min(65536, size - offset + 1))
                        if not chunk:
                            raise ValueError('INCOMPLETE_IMAGE_BLOB')
                        output.write(chunk)
                        offset += len(chunk)
                if job_id is not None:
                    named = path.with_suffix(image_suffix(path))
                    path.replace(named)
                    path = named
                paths[number] = path
            if found != {1, 2}:
                raise ValueError('MISSING_IMAGE_PAIR')
        return paths[1], paths[2]
    finally:
        conn.rollback()  # End read transaction; the job claim was already committed.


def write_job_output(root, job_id, out, status=None):
    """Keep a readable summary even for invalid images or failed DB persistence."""
    result = {'jobId': job_id, 'databaseStatus': status, 'ocrOutcome': out.outcome,
              'confidence': out.confidence, 'extractedFields': out.fields, 'details': out.details}
    destination = root / 'results' / f'{int(job_id)}.json'
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(destination)


def mapped_result(out, threshold):
    fields = out.fields or {}
    values = {column: str(fields[key]).strip() if fields.get(key) is not None else None
              for key, (column, _) in TEXT_FIELDS.items()}
    gender = str(fields.get('gender', '')).strip()
    values['CLIENT_GENDER'] = {'ذكر': '1', 'انثى': '2', 'أنثى': '2'}.get(gender)
    errors = []
    if gender and values['CLIENT_GENDER'] is None:
        errors.append('UNRECOGNIZED_GENDER')
    for key, column in [('birthDate', 'DATE_OF_BIRTH'), ('expiryDate', 'ID_EXPIRE_DATE')]:
        values[column] = None
        if fields.get(key):
            try:
                values[column] = date.fromisoformat(str(fields[key]))
            except ValueError:
                errors.append('INVALID_' + column)
    confidence = out.confidence
    if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence) or not 0 <= confidence <= 1):
        confidence = None
    # Review flags remain in the report; acceptance uses the overall score.
    success = (out.outcome in ('Success', 'LowConfidence') and bool(fields)
               and confidence is not None and confidence >= threshold)
    # ASCII escapes keep all Unicode intact in legacy database CLOB charsets.
    values.update(EXTRACTED_JSON=json.dumps(fields, ensure_ascii=True, allow_nan=False),
                  OCR_DETAILS_JSON=(json.dumps(out.details, ensure_ascii=True, allow_nan=False)
                                    if out.details is not None else None),
                  OCR_OUTCOME=out.outcome, CONFIDENCE=confidence,
                  STATUS='OCR_SUCCESS' if success else 'OCR_FAIL')
    if not success:
        reason = (out.details or {}).get('errorCode')
        if not reason:
            reason = (out.outcome if out.outcome != 'Success' else
                      'NO_EXTRACTED_FIELDS' if not fields else
                      'CONFIDENCE_UNAVAILABLE' if confidence is None else 'LOW_CONFIDENCE')
        errors.append(str(reason))
    return values, errors


def load_column_storage(conn):
    """Honor actual byte/character limits and Unicode types on the job table."""
    with conn.cursor() as cur:
        cur.execute("SELECT COLUMN_NAME, DATA_TYPE, CHAR_LENGTH, DATA_LENGTH, CHAR_USED "
                    "FROM ALL_TAB_COLUMNS WHERE OWNER = 'BRK_COD' "
                    "AND TABLE_NAME = 'CLIENT_IMAGE_OCR_JOB'")
        return {name: (kind, chars if char_used == 'C' else size)
                for name, kind, chars, size, char_used in cur}


def save_result(conn, driver, job_id, out, threshold, storage=None):
    values, errors = mapped_result(out, threshold)
    storage = storage or {}
    text_limits = {column: storage.get(column, ('VARCHAR2', limit))
                   for column, limit in TEXT_FIELDS.values()}
    unicode_binds = {column: driver.DB_TYPE_NVARCHAR for column, (kind, _) in text_limits.items()
                     if kind == 'NVARCHAR2'}
    json_binds = {}
    for column in ('EXTRACTED_JSON', 'OCR_DETAILS_JSON'):
        is_unicode = storage.get(column, ('CLOB', None))[0] == 'NCLOB'
        json_binds[column] = driver.DB_TYPE_NCLOB if is_unicode else driver.DB_TYPE_CLOB
        if is_unicode and values[column] is not None:
            values[column] = json.dumps(json.loads(values[column]), ensure_ascii=False, allow_nan=False)
    try:
        with conn.cursor() as cur:
            # Reject obviously oversized strings before binding them to SQL.
            for column, (_, limit) in text_limits.items():
                if values[column] is not None and len(values[column]) > limit:
                    values[column] = None
                    errors.append('FIELD_TOO_LONG_' + column)
            # Bind Unicode scalars as NVARCHAR before measuring or writing them.
            if unicode_binds:
                cur.setinputsizes(**unicode_binds)
            columns = list(text_limits)
            cur.execute('SELECT ' + ', '.join(
                f'{"LENGTH" if storage.get(c, (None,))[0] == "NVARCHAR2" else "LENGTHB"}(:{c})'
                for c in columns)
                        + ' FROM DUAL', {c: values[c] for c in columns})
            lengths = cur.fetchone()
            for (column, (_, limit)), length in zip(text_limits.items(), lengths):
                if length is not None and length > limit:
                    values[column] = None  # Original value remains in JSON.
                    errors.append('FIELD_TOO_LONG_' + column)
            # Mapping warnings remain visible without overriding score acceptance.
            values['ERROR_MESSAGE'] = '; '.join(errors)[:1000] or None
            cur.setinputsizes(**unicode_binds, **json_binds,
                              DATE_OF_BIRTH=driver.DB_TYPE_DATE,
                              ID_EXPIRE_DATE=driver.DB_TYPE_DATE)
            cur.execute(f'UPDATE {TABLE} SET ' +
                        ', '.join(f'{c} = :{c}' for c in values) +
                        ', OCR_COMPLETED_DATE = CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) '
                        "WHERE JOB_ID = :job_id AND STATUS = 'UNDER_PROCESSING'",
                        dict(values, job_id=job_id))
            if cur.rowcount != 1:
                raise RuntimeError('Job is no longer UNDER_PROCESSING; result was not saved')
        conn.commit()
        return values['STATUS']
    except Exception:
        conn.rollback()
        raise


def connect(args):
    import oracledb
    # Oracle 11g requires Thick mode with a compatible Oracle Client (19c for 11.2).
    if not args.thin:
        oracledb.init_oracle_client(lib_dir=args.client_lib_dir or None)
    conn = oracledb.connect(user=args.user, password=args.password, dsn=args.dsn)
    conn.autocommit = False
    return conn, oracledb


def worker_main(args, stop):
    launcher_pid = getattr(args, 'launcher_pid', os.getpid())
    context = (nullcontext() if launcher_pid == os.getpid() else
               capture_console('worker', launcher_pid, args.log_run_id))
    with context:
        log.info('Worker started; worker PID=%s', os.getpid())
        _worker_main(args, stop)


def _worker_main(args, stop):
    shutdown = install_shutdown_handlers()
    with report_worker_shutdown(shutdown, stop) as notify_shutdown:
        _run_worker(args, stop, shutdown, notify_shutdown)


def _run_worker(args, stop, shutdown, notify_shutdown):
    with connect(args)[0] as conn:
        import oracledb
        with conn.cursor() as cur:
            cur.execute(f'SELECT JOB_ID, NIN_CODE, STATUS, MARITAL_STATUS, RELIGION, '
                        f'EXTRACTED_JSON, OCR_DETAILS_JSON FROM {TABLE} WHERE 1=0')
            cur.execute(f'SELECT NIN_COD, FILE_NUMBER, DOC FROM {SOURCE} WHERE 1=0')
        storage = load_column_storage(conn)
        if args.check_connection:
            log.info('Oracle connection/schema read checks passed; database=%s; thick mode=%s',
                     conn.version, not oracledb.is_thin_mode())
            return
        (OUTPUT_ROOT / 'uploads').mkdir(parents=True, exist_ok=True)
        (OUTPUT_ROOT / 'results').mkdir(parents=True, exist_ok=True)
        initialize()
        log.info('Retaining Oracle job images and results in %s', OUTPUT_ROOT)
        concurrency = args.job_concurrency
        log.info('Models ready; jobConcurrency=%s; polling pending Oracle jobs every %.2fs',
                 concurrency, args.poll_seconds)
        if concurrency == 1:
            consume_jobs(args, stop, shutdown, conn, oracledb, storage)
    # Close the startup connection before opening one per job thread.
    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix='oracle-job') as pool:
            futures = [pool.submit(job_thread, args, stop, shutdown, storage)
                       for _ in range(concurrency)]
            for future in futures:
                future.result()  # A failing thread sets stop; siblings drain active jobs.
    notify_shutdown()
    log.info('Stopped after finishing current jobs')


def job_thread(args, stop, shutdown, storage):
    try:
        if shutdown.requested or stop.is_set():
            return
        # Oracle Client was initialized on the main thread before model loading.
        import oracledb
        with oracledb.connect(user=args.user, password=args.password, dsn=args.dsn) as conn:
            conn.autocommit = False
            consume_jobs(args, stop, shutdown, conn, oracledb, storage)
    except BaseException:
        stop.set()  # Stop new claims, let other threads finish and save their jobs.
        raise


def consume_jobs(args, stop, shutdown, conn, driver, storage):
    while not shutdown.requested and not stop.is_set():
        job = claim_job(conn, driver)
        if job is None:
            wait_for_stop(stop, shutdown, args.poll_seconds)
            continue
        job_id, nin = job
        log.info('Claimed job=%s thread=%s', job_id, threading.current_thread().name)
        try:
            front, back = read_images(conn, nin, OUTPUT_ROOT / 'uploads', job_id=job_id)
            out = run_ocr_paths(front, back, output=OUTPUT_ROOT / 'results', job_id=job_id)
        except ValueError as exc:
            out = OcrOutput('Unreadable', 0.0, details={'errorCode': str(exc)})
        except Exception:
            # Avoid logging driver messages which may contain sensitive SQL values.
            log.error('Image read or inference failed for job=%s', job_id)
            out = OcrOutput('TechnicalError', details={'errorCode': 'OCR_PROCESSING_ERROR'})
        write_job_output(OUTPUT_ROOT, job_id, out)
        # If DB save fails, retain the local result and exit.
        status = save_result(conn, driver, job_id, out, args.confidence_threshold, storage)
        write_job_output(OUTPUT_ROOT, job_id, out, status)
        log.info('Completed job=%s status=%s thread=%s',
                 job_id, status, threading.current_thread().name)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dsn', default=os.getenv('ORACLE_DSN'))
    parser.add_argument('--user', default=os.getenv('ORACLE_USER', 'BRK_COD'))
    parser.add_argument('--client-lib-dir', default=os.getenv('ORACLE_CLIENT_LIB_DIR'))
    parser.add_argument('--thin', action='store_true',
                        help='Use Thin mode without Oracle Client libraries (requires Oracle 12.1+)')
    parser.add_argument('--workers', type=int, default=1, help='Model-owning processes (default: 1)')
    parser.add_argument('--job-concurrency', type=int, default=1,
                        help='Job threads per process, sharing one model set (default: 1)')
    parser.add_argument('--poll-seconds', type=float, default=2)
    parser.add_argument('--confidence-threshold', type=float, default=0.50)
    parser.add_argument('--check-connection', action='store_true', help='Read-only check; no model loading or claims')
    args = parser.parse_args()
    if not args.dsn:
        parser.error('Set ORACLE_DSN or pass --dsn host:port/service_name')
    if not 1 <= args.workers <= 8:
        parser.error('--workers must be between 1 and 8')
    if not 1 <= args.job_concurrency <= 8:
        parser.error('--job-concurrency must be between 1 and 8')
    if not math.isfinite(args.poll_seconds) or args.poll_seconds <= 0:
        parser.error('--poll-seconds must be positive and finite')
    if not math.isfinite(args.confidence_threshold) or not 0 <= args.confidence_threshold <= 1:
        parser.error('--confidence-threshold must be between 0 and 1')
    args.password = os.getenv('ORACLE_PASSWORD') or getpass.getpass('Oracle password: ')
    return args


def supervise_workers(processes, stop, shutdown):
    try:
        for process in processes:
            process.start()
            log.info('Started worker PID=%s', process.pid)
        while any(p.is_alive() for p in processes):
            if shutdown.requested and not stop.is_set():
                log.info('Shutdown requested; waiting for workers to finish current jobs')
                stop.set()  # Normal execution context, never inside a signal handler.
            for process in processes:
                if process.exitcode not in (None, 0):
                    stop.set()  # Do not automatically restart or reassign crashed jobs.
            for process in processes:
                join_process(process, timeout=0.2)
    finally:
        stop.set()
        for process in processes:
            if process.pid is not None:
                join_process(process)
    if any(p.exitcode != 0 for p in processes):
        raise SystemExit('An Oracle worker failed; inspect its UNDER_PROCESSING job before restarting.')


def main():
    args = parse_args()
    args.launcher_pid = os.getpid()
    args.log_run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    with capture_console('launcher', args.launcher_pid, args.log_run_id) as logfile:
        log.info('Launcher started; launcher PID=%s; requested workers=%s; log=%s',
                 args.launcher_pid, args.workers, logfile)
        try:
            if args.workers == 1 or args.check_connection:
                worker_main(args, threading.Event())
                return
            ctx = multiprocessing.get_context('spawn')
            stop = ctx.Event()
            shutdown = install_shutdown_handlers()
            processes = [ctx.Process(target=worker_main, args=(args, stop)) for _ in range(args.workers)]
            supervise_workers(processes, stop, shutdown)
            log.info('All Oracle workers stopped')
        finally:
            log.info('Launcher stopped; launcher PID=%s', args.launcher_pid)


if __name__ == '__main__':
    main()
