# Oracle HTTP coordinator

`oracle_coordinator.py` runs on the database-connected application server. One
process uses one coordinating loop and one Oracle connection to submit/poll up
to three outstanding requests. Spark performs inference concurrently. The
original `oracle_worker.py` still runs local inference; choose one mode for the
test workload. Do not run multiple coordinators against the same tracked rows.

This increment adds HTTP polling/retry, durable request IDs, guarded result
saves, local pending-result replay, restart resubmission and commit-before-ACK.
It does not add automatic Oracle reconnection or lease-based takeover of jobs.

## 1. Database migration

Connect as BRK_COD and review/run
`database/oracle/005_async_request_tracking.sql` once in Toad (F5). It adds:

| Column | Purpose |
|---|---|
| `OCR_REQUEST_ID` | UUID for the current processing attempt; committed with the claim |
| `OCR_SERVICE_INSTANCE_ID` | Spark startup ID, recorded when the request is found/accepted |
| `OCR_SUBMITTED_DATE` | UTC submission/tracking date |
| `OCR_ACK_PENDING` | Result committed to Oracle, but Spark acknowledgement not yet confirmed |

Existing data, original image BLOBs and the five business statuses are preserved.
The validation procedure still owns `FINISHED`. Preserve these tracking columns
when the procedure updates a row: in particular, do not clear the request ID or
ACK flag before the coordinator confirms acknowledgement. DDL commits implicitly.

No migration is executed automatically by Python. `--check-connection` verifies
the required schema by reading it; it does not claim jobs.

Stop other coordinators and isolate the test workload before switching modes.
Old `UNDER_PROCESSING` rows with no request ID are not automatically recovered.
Repeated submissions of a client should be separate Oracle jobs/attempts; do
not manually reset a tracked row while it has an outstanding request or file.

## 2. Install on the application server

Create a native Python environment and install:

```bash
python -m pip install -r backend/ocr-worker/requirements.coordinator.txt
```

Only oracledb, python-dotenv and httpx are required (plus their dependencies).
Oracle 11g still requires compatible Oracle Client 19c Thick-mode libraries on
this machine. No model weights, CUDA, PyTorch, Pillow, FastAPI or GPU are needed
for the coordinator. Reused local modules import model libraries only when
local inference is explicitly called; this coordinator never calls it.

Use the project-root `.env` for Oracle credentials and these additional values:

```dotenv
OCR_API_URL=http://127.0.0.1:18001
OCR_API_TOKEN=the-same-service-secret-configured-on-spark
```

Protect the real `.env` and pending-result directory; they must not be copied
into transfer archives. A retained pending result includes personal data.

## 3. SSH forwarding when direct HTTP is blocked

On Spark, install API dependencies and start its API bound to loopback using
the [API guide](ASYNC_API.md). Use its native environment and configured library
paths. Oracle Client paths are not needed for the inference API itself.

```bash
python backend/ocr-worker/ocr_api.py --host 127.0.0.1 --port 8001 --job-concurrency 3 --capacity 6
```

On the application server, keep this SSH tunnel running:

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:18001:127.0.0.1:8001 tradingteam@172.17.30.11
```

Here `18001` is the application server's local entrance, and `8001` is the API
port on Spark's loopback interface. The coordinator speaks HTTP through the
encrypted SSH connection. This is independent of the earlier reverse Oracle
tunnel and requires SSH reachability from the application server. No SSH password
is stored by this code. Production tunnel supervision/keys are operational setup.

## 4. Check and run

From the project root on the application server:

```bash
python backend/ocr-worker/oracle_coordinator.py --check-connection
python backend/ocr-worker/oracle_coordinator.py --check-api
python backend/ocr-worker/oracle_coordinator.py --max-outstanding 3
```

`--check-api` does not connect to Oracle, load models or claim jobs. It verifies
authenticated readiness through the configured URL. Direct internal HTTPS is
also supported with `--api-url https://approved-host/path`; proxy environment
variables are ignored and redirects are not followed, avoiding accidental token
forwarding. TLS verification stays enabled.

Useful options: `--poll-seconds 1`, `--retry-seconds 5`, `--http-timeout 30`,
`--confidence-threshold 0.5`, `--drain-seconds 60`, and `--pending-dir PATH`.
There are no model-worker or inference-thread settings on this process.

## 5. Processing and recovery

1. Startup reloads tracked `UNDER_PROCESSING` rows and committed rows with ACK
   pending. It also checks local pending files, including files left after a
   commit whose confirmation was lost. Ownership conflicts stop the coordinator
   without discarding the file or overwriting the row.
2. A claim records a UUID and `UNDER_PROCESSING` in the same Oracle transaction,
   using the existing single-fetch `FOR UPDATE SKIP LOCKED` pattern. Commit
   releases the row lock before network calls or inference.
3. The coordinator looks for that request on Spark before uploading. Missing,
   never-submitted requests read both original BLOBs into a temporary directory
   and upload them. Files are removed after the submission attempt.
4. If the submission response is lost, the next poll finds the same request ID;
   the job is not blindly submitted under a new ID. Busy/transport errors pause
   claims and retry after the configured delay. They do not create OCR_FAIL.
5. If Spark's instance changes and the request is missing, both images are read
   and posted again with the tracked ID. Some OCR can repeat. Missing requests
   within the same known instance stop for investigation rather than silently
   repeating work. A connection timeout alone is not evidence of request loss.
6. A completed result is written atomically to
   `.data/coordinator/pending-results/<job>_<request>.json`, then saved to Oracle.
   The UPDATE matches the job ID, `UNDER_PROCESSING`, and current request ID;
   it sets ACK pending together with the extracted fields and completion date.
7. After confirmed commit, the coordinator acknowledges Spark, clears the Oracle
   ACK flag, removes the local pending file and releases its outstanding slot.
   Until then the slot stays occupied. Terminal rows already committed by that
   same attempt are recognised, including `FINISHED` rows updated by the SP.
8. Oracle exceptions stop this version. After access is restored, restarting the
   coordinator replays collected results from files without redoing inference.
   Automatic reconnect/backoff within the same process is deferred. Keep pending
   files on persistent storage and exclude them from retention cleanup.

An unknown Spark request during acknowledgement is harmless once the result is
confirmed in Oracle: there is no result left to release. Invalid source pairs
and API-rejected image contents produce an ordinary Unreadable/OCR_FAIL result.
Malformed API responses, authentication failures and ownership conflicts stop
for investigation instead of writing misleading OCR failures.

On Ctrl+C/SIGTERM, stop claiming and drain tracked requests for up to the chosen
drain window. HTTP calls already in progress can add their own timeout. Remaining
requests stay tracked for the next startup; they are not reset to PENDING.

Only one active coordinator is supported in this increment. There is no lease,
heartbeat or automatic takeover from another coordinator. A supervised restart
of the same coordinator can resume its tracked jobs. Multiple coordinators need
explicit ownership/lease handling before deployment.

## Logs and tests

Logs mirror the existing worker's console/file format:

```text
backend/ocr-worker/logs/oracle_coordinator_<timestamp>_coordinator_<PID>.log
```

Entries include timestamp, severity, PID, launcher PID, role, job/request IDs,
claims, submissions, commits, acknowledgements, connection pauses and shutdown.
Credentials, images and extracted values are not printed. Oracle errors log a
numeric Oracle code when available, without raw SQL/driver messages.

The focused tests use fake Oracle state and HTTP requests against the actual API
application with fake inference. They cover concurrency, submission-response
loss, Spark restarts, pending replay before/after commit, SP-owned FINISHED state,
stale ownership, capacity gating and guarded SQL. No real Oracle jobs are claimed.

```bash
python -m unittest discover -s backend/ocr-worker -p 'test_oracle_coordinator.py'
```

Install the API test extras in a development/test environment for these
integration tests; they are not coordinator runtime requirements. Live Oracle,
SSH tunnel and GPU integration must be tested separately after the migration.
