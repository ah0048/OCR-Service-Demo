"""Bounded reconnection policy; never automatically replay a failed transaction."""
import logging

log = logging.getLogger('oracle-coordinator')

# Oracle 11g does not support _Error.isrecoverable (requires DB 12.1+).
# Use explicit disconnect/listener/availability codes for that database.
TRANSIENT_ORACLE_CODES = frozenset({
    28, 1012, 1033, 1034, 1089, 1090, 1092, 3113, 3114, 3135,
    12170, 12535, 12537, 12541, 12547, 12570, 12571,
})
TRANSIENT_DRIVER_CODES = frozenset({'DPI-1010', 'DPI-1080', 'DPY-1001', 'DPY-4011'})


def transient_database_error(exc):
    # Import lazily: --check-api and importing the coordinator need no DB driver.
    import oracledb
    if not isinstance(exc, oracledb.Error) or not exc.args:
        return False
    error = exc.args[0]
    return (getattr(error, 'code', None) in TRANSIENT_ORACLE_CODES or
            getattr(error, 'full_code', None) in TRANSIENT_DRIVER_CODES or
            getattr(error, 'isrecoverable', False) is True)


def safe_error_code(exc):
    error = exc.args[0] if exc.args else None
    code = getattr(error, 'code', None)
    if type(code) is int and code > 0:
        return f'ORA-{code:05d}'
    full_code = getattr(error, 'full_code', '')
    if full_code in TRANSIENT_DRIVER_CODES:
        return full_code
    return 'UNKNOWN_DATABASE_ERROR'


class OracleRecoveryExhausted(RuntimeError):
    def __init__(self):
        super().__init__('ORACLE_RETRIES_EXHAUSTED')


class RetryBudget:
    def __init__(self, retries, seconds, max_seconds):
        self.retries, self.seconds, self.max_seconds = retries, seconds, max_seconds
        self.used = 0

    def next_delay(self):
        if self.used >= self.retries:
            raise OracleRecoveryExhausted()
        self.used += 1
        return min(self.max_seconds, self.seconds * 2 ** (self.used - 1))

    def recovered(self):
        # Reset only after a complete coordinator cycle, not merely a login.
        self.used = 0


def close_connection(conn):
    if conn is not None:
        try:
            conn.close()
        except Exception:
            # A dead-session close must not hide the failure being reconciled.
            log.warning('Could not close Oracle session cleanly; discarding connection')
