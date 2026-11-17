"""Oracle 11g request tracking. One active coordinator; no lease-based takeover."""
from dataclasses import dataclass
from uuid import uuid4

from oracle_worker import CLAIM_SQL, TABLE


@dataclass
class TrackedJob:
    job_id: int
    nin: str
    request_id: str
    instance_id: str | None = None
    status: str = 'UNDER_PROCESSING'
    ack_pending: bool = False
    completed: bool = False


TRACKED_CLAIM_SQL = CLAIM_SQL.replace(
    'STARTED_DATE = CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE)',
    'STARTED_DATE = CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE), '
    'OCR_REQUEST_ID = :request_id, OCR_SERVICE_INSTANCE_ID = NULL, '
    'OCR_SUBMITTED_DATE = NULL, OCR_ACK_PENDING = 0, OCR_COMPLETED_DATE = NULL')


def claim_request(conn, driver):
    request_id = str(uuid4())
    try:
        with conn.cursor() as cur:
            job_id, nin = cur.var(driver.DB_TYPE_NUMBER), cur.var(driver.DB_TYPE_NUMBER)
            cur.execute(TRACKED_CLAIM_SQL, job_id=job_id, nin_code=nin, request_id=request_id)
            values = job_id.getvalue(), nin.getvalue()
        conn.commit()
        return None if values[0] is None else TrackedJob(int(values[0]), str(int(values[1])), request_id)
    except Exception:
        conn.rollback()
        raise


def _job(row):
    job_id, nin, request_id, instance_id, status, ack_pending, completed = row
    return TrackedJob(int(job_id), str(int(nin)), request_id, instance_id,
                      status, bool(ack_pending), completed is not None)


SELECT_TRACKED = (f'SELECT JOB_ID, NIN_CODE, OCR_REQUEST_ID, OCR_SERVICE_INSTANCE_ID, '
                  f'STATUS, OCR_ACK_PENDING, OCR_COMPLETED_DATE FROM {TABLE} ')


def outstanding(conn):
    with conn.cursor() as cur:
        cur.execute(SELECT_TRACKED + "WHERE OCR_REQUEST_ID IS NOT NULL AND "
                    "(STATUS = 'UNDER_PROCESSING' OR OCR_ACK_PENDING = 1) ORDER BY JOB_ID")
        rows = [_job(row) for row in cur]
    conn.rollback()
    return rows


def inspect_request(conn, job_id):
    with conn.cursor() as cur:
        cur.execute(SELECT_TRACKED + 'WHERE JOB_ID = :job_id', job_id=job_id)
        row = cur.fetchone()
    conn.rollback()
    return _job(row) if row else None


def mark_submitted(conn, job, instance_id):
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE {TABLE} SET OCR_SERVICE_INSTANCE_ID = :instance_id, '
                        'OCR_SUBMITTED_DATE = CAST(SYS_EXTRACT_UTC(SYSTIMESTAMP) AS DATE) '
                        "WHERE JOB_ID = :job_id AND OCR_REQUEST_ID = :request_id "
                        "AND STATUS = 'UNDER_PROCESSING'",
                        instance_id=instance_id, job_id=job.job_id, request_id=job.request_id)
            if cur.rowcount != 1:
                raise RuntimeError('REQUEST_OWNERSHIP_CHANGED')
        conn.commit()
        job.instance_id = instance_id
    except Exception:
        conn.rollback()
        raise


def clear_ack(conn, job):
    try:
        with conn.cursor() as cur:
            cur.execute(f'UPDATE {TABLE} SET OCR_ACK_PENDING = 0 '
                        'WHERE JOB_ID = :job_id AND OCR_REQUEST_ID = :request_id '
                        "AND STATUS IN ('OCR_SUCCESS', 'OCR_FAIL', 'FINISHED') "
                        'AND OCR_COMPLETED_DATE IS NOT NULL',
                        job_id=job.job_id, request_id=job.request_id)
            if cur.rowcount != 1:
                raise RuntimeError('REQUEST_OWNERSHIP_CHANGED')
        conn.commit()
        job.ack_pending = False
    except Exception:
        conn.rollback()
        raise
