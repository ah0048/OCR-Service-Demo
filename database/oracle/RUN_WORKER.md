# Run the Oracle OCR worker

The standalone entry point is `backend/ocr-worker/oracle_worker.py`. It does not
start the frontend, .NET services, PostgreSQL, RabbitMQ, HTTP downloads or SSH
tunnels. Keep `try1` and its existing model weights available.

## How jobs are consumed

Workers poll BRK_COD.CLIENT_IMAGE_OCR_JOB for committed PENDING rows. Idle workers
wait two seconds by default; after completing a job they check again immediately.
No insert trigger, notification subscription or Oracle Scheduler job is needed.

A short PL/SQL cursor fetch locks one available row with FOR UPDATE SKIP LOCKED,
changes it to UNDER_PROCESSING, sets STARTED_DATE and commits. The cursor fetch
happens server-side so driver prefetch cannot claim multiple jobs accidentally.
No lock is held during OCR. Each worker has its own connection and model set.

Workers compete for available jobs, rather than following a strict round-robin
schedule. A busy worker takes no additional job; a ready worker claims another.
STARTED_DATE reflects the attempt. Results and OCR_COMPLETED_DATE are committed
together. FINISHED remains owned by the validation stored procedure.

Each NIN_CODE identifies a client/image pair. The worker converts its numeric
value to decimal text and queries CLIENTSOWNER.CLIENT_IMAGE_OCR using NIN_COD,
FILE_NUMBER 1/2. It rejects missing, duplicate, empty or oversized source images.
Confirm source NIN_COD uses the same decimal representation without leading zeros.
Image validation and front/back ID agreement checks remain in use. Both agreed
and reconciled ID verdicts now permit the remaining field readers to run;
conflicts and unverified IDs still stop before text/classifier processing.
The detailed report retains the actual agreement verdict for human review.

## Prerequisites

- Python with the existing model weights and inference dependencies.
- Network access to the Oracle listener; the GPU worker itself must reach it.
- BRK_COD SELECT/UPDATE on its job table (implicit as owner), and the existing
  SELECT grant on CLIENTSOWNER.CLIENT_IMAGE_OCR.
- Compatible Oracle Client libraries and python-oracledb **Thick mode**.

Oracle 11.2 can use Oracle Client **19c** on a supported platform. Client 21c/23c
cannot connect to that old database. Confirm the actual server release/patch
with the DBA. Native Apple Silicon client availability differs from x86-64:
do not assume the Mac's available client can connect to 11g. Windows x86-64 or
Linux x86-64 with 19c is the straightforward starting point. Python architecture
must match the client libraries; a client usable by 32-bit Toad may not work with
64-bit Python. Use the Basic package with appropriate Arabic character support.

Official references:
- https://python-oracledb.readthedocs.io/en/stable/user_guide/installation.html#supported-oracle-database-versions
- https://python-oracledb.readthedocs.io/en/stable/user_guide/initialization.html
- https://docs.oracle.com/cd/E11882_01/server.112/e41084/statements_10002.htm

## Windows test setup

For readable Arabic JSON and Arabic-Indic digits directly in address/occupation,
apply `004_unicode_storage.sql` in Toad as BRK_COD while workers/writers are stopped.
It retains original columns as *_OLD, uses NVARCHAR2 for address/occupation and
NCLOB for JSON. Restart workers so they reread column metadata. New Unicode
columns get explicit national-type binds. Without migration the worker continues
to use escaped CLOB JSON; legacy VARCHAR2 still cannot preserve all Unicode.
This migration does not backfill OCR predictions, decode older escaped JSON, or
recover characters already stored as question marks.

From the project root, using an existing Python environment with the OCR models'
dependencies installed (or install the standalone requirements into your chosen
environment):

```powershell
python -m pip install -r backend/ocr-worker/requirements.oracle.txt
Copy-Item .env.example .env
```

Edit the project-root `.env` file, replacing host/service and client-library path:

```dotenv
ORACLE_DSN=DB_HOST:1521/SERVICE_NAME
ORACLE_USER=BRK_COD
ORACLE_PASSWORD=
ORACLE_CLIENT_LIB_DIR='C:\oracle\instantclient_19_XX'
OCR_DEVICE=cpu
```

Leave ORACLE_PASSWORD empty for an interactive password prompt, or set a
single-quoted local password. `.env` and `.env.*` are ignored by Git, except the
credential-free `.env.example` template. Do not commit or share your real file.
The worker loads this specific project-root file regardless of the current
working directory, before inference reads device/model settings. Command-line
options override process environment variables, which override `.env` values.
Variable interpolation is disabled to preserve literal `${...}` in passwords.

Then run the read-only check:

```powershell
python backend/ocr-worker/oracle_worker.py --check-connection
```

To explicitly test Thin mode without loading Oracle Client libraries:

```powershell
python backend/ocr-worker/oracle_worker.py --thin --check-connection
```

This ignores ORACLE_CLIENT_LIB_DIR for that invocation and does not claim jobs.
Thin mode requires Oracle 12.1 or newer; an actual Oracle 11g endpoint is not
supported. A successful check reports the database version and thick mode=False.
Without --thin, the default remains Thick mode. Connection errors are displayed
unchanged; there is no automatic fallback between modes.

Replace the placeholders with the actual listener/service and extracted Oracle
Client folder. For a SID rather than a service name, pass a full connect
descriptor, for example:

```dotenv
ORACLE_DSN='(DESCRIPTION=(ADDRESS=(PROTOCOL=TCP)(HOST=DB_HOST)(PORT=1521))(CONNECT_DATA=(SID=DB_SID)))'
```

The user defaults to BRK_COD. If no password is configured, it is prompted without
echo and is not saved by the application. The connection check only verifies login
and read access to required source/job columns. It loads no models, claims no
jobs and does not prove UPDATE privileges or inference correctness.

Start one model-owning process:

```powershell
python backend/ocr-worker/oracle_worker.py --workers 1
```

Start two on a machine with enough memory:

```powershell
python backend/ocr-worker/oracle_worker.py --workers 2
```

Password is requested once per launch. `--workers` is bounded to 1..8. Each
process loads its own models; two processes still duplicate model memory. This
worker uses one active job per process. Start with one process on a shared GPU.

On Linux set the Oracle Client library search path before starting Python
(for example LD_LIBRARY_PATH pointing to the 19c directory), leave
ORACLE_CLIENT_LIB_DIR unset, and use `python3` with OCR_DEVICE=cuda for a supported
CUDA installation. The argument syntax is the same. GPU selection can be made
with CUDA_VISIBLE_DEVICES per launch; this launcher does not distribute processes
across GPUs automatically.

## Outcomes and stopping

Console output is also retained in `backend/ocr-worker/logs/` as UTF-8 files:

```text
oracle_<launch-timestamp>_launcher_<pid>.log
oracle_<launch-timestamp>_worker_<pid>.log
```

The launcher prints its PID, requested process count, log location and each
spawned worker PID. Structured log lines include both the current PID and the
launcher PID. Each process writes a separate file to avoid simultaneous writes
to one log. With one worker the launcher and inference share one PID/log file.
Python stdout/stderr are mirrored to terminal and file, including weight-loading
progress bars, warnings and unhandled exceptions. Native-library output written
directly to OS file descriptors may bypass Python's stream capture. Password
input occurs before capture and arguments/credentials are not dumped to logs.
Every launch creates new log filenames; there is no automatic log deletion.
The log folder is ignored by Git. Restart to enable logging for a running service.
Shutdown requests are acknowledged in both single- and multi-worker mode by
normal execution/background reporting, never by logging inside a signal handler.
Late terminal cleanup (including Colorama atexit resets) can still write to the
console after the file is detached and closed.

Oracle workers retain API-style artifacts under `try1/outputs/oracle_workers`
(or ORACLE_OUTPUT_ROOT if explicitly configured):

```text
oracle_workers/
  uploads/
    123_1.jpg                    # original front BLOB for job 123
    123_2.png                    # original back BLOB; suffix follows real format
  results/
    123.json                    # readable OCR summary, including DB status
    document_123_<unique-run>/
      result.json               # full pipeline report with local artifact paths
      front/
        123_1_<timestamp>_<run>/
          annotated.jpg
          result.json
          crops/
      back/
        123_2_<timestamp>_<run>/
          annotated.jpg
          result.json
          crops/
```

The uploads are original BLOB bytes, not re-encoded images. Invalid/unknown
formats are retained as .bin for diagnosis. Readable UTF-8 summary JSON also
exists for image/inference errors when no pipeline report could be produced.
Summary databaseStatus is null until the DB save succeeds. Inference artifacts
are retained instead of deleted with a temporary directory. Reprocessing the
same job replaces its upload/summary files but creates a new document run folder.
Artifacts from different job IDs do not collide between workers. Existing Git
ignore rules exclude try1/outputs. Previously processed jobs are not backfilled.

- OCR_SUCCESS: inference outcome Success or LowConfidence, nonempty fields, and
  finite overall confidence at least 0.50 (50%) by default. Review flags, missing
  individual fields and mapping warnings do not independently reject a result.
- OCR_FAIL: overall score below threshold or unavailable/invalid, no extracted
  fields, ID mismatch, unreadable/missing images or caught technical errors.
- Gender/religion/marital classifiers now accept text at a score of at least
  0.80 (80%). Lower-scored candidates remain in the detailed report without text.
- `--confidence-threshold` can configure the 0..1 threshold.
- Gender: ذكر -> '1'; انثى / أنثى -> '2'. Unknown values remain NULL and require
  review. Original field values remain in EXTRACTED_JSON.
- Oversized destination text is saved as NULL in its typed column and retained
  intact in JSON. ERROR_MESSAGE identifies mapping warnings even for OCR_SUCCESS;
  missing/invalid mappings do not independently change the score-based status.
- Every result, including OCR_SUCCESS, still requires human review.

`--job-concurrency 2` runs two job threads per process with one shared model set.
Use `--workers 1 --job-concurrency 2` to start with two concurrent jobs rather
than two copies of the models. Each thread opens its own Oracle connection and
claims rows with the same SKIP LOCKED SQL. Both options default to 1; the product
of their values is the maximum number of active jobs. Qari generation and YOLO
access per model remain locked; CUDA/CPU document stages can overlap. Metal keeps
whole-document serialization. This does not implement cross-job batching.

Ctrl+C stops accepting new jobs and lets all active jobs finish. In multi-worker
mode the parent logs "Shutdown requested" and notifies workers from its normal
supervisor loop; signal handlers only set local flags to avoid event-lock
deadlocks. Idle polling uses local sleeps, checking shutdown every 0.2 seconds,
instead of multiprocessing condition waits that Windows keyboard signals can
interrupt. Interrupted idle/process waits are handled without marking a normal
shutdown as a worker failure. Ctrl+Break is also
handled on Windows consoles. If Git Bash does not deliver keyboard signals to
native Python, run from PowerShell/Windows Terminal instead. Native model/Oracle
calls can delay shutdown until the current call completes. There are no
automatic retries, leases or restarts. If a worker dies or a result cannot be
saved, its row can remain UNDER_PROCESSING. A database error stops the worker
rather than claiming additional work; with --workers, the supervisor requests
the remaining workers finish and stop. Use the specific-job manual failure SQL
in README.md after confirming that worker has stopped. Do not reset live jobs
to PENDING: this simplified implementation has no claim tokens.

Monitor in Toad:

```sql
SELECT STATUS, COUNT(*) FROM BRK_COD.CLIENT_IMAGE_OCR_JOB GROUP BY STATUS;
SELECT JOB_ID, NIN_CODE, STATUS, STARTED_DATE, OCR_COMPLETED_DATE,
       CLIENT_GENDER, OCR_OUTCOME, CONFIDENCE, ERROR_MESSAGE
FROM BRK_COD.CLIENT_IMAGE_OCR_JOB ORDER BY JOB_ID;
```

If the table predates the marital-status/religion columns, first run
`002_add_extracted_fields.sql` as BRK_COD in Toad (F5). It also adds EXTRACTED_JSON
if missing. Restart workers after applying the migration. MARITAL_STATUS and
RELIGION hold raw Arabic labels; EXTRACTED_JSON holds the complete field dictionary.
Existing completed rows are not automatically backfilled. Also apply
`003_add_ocr_details_json.sql` before starting the updated worker.
OCR_DETAILS_JSON stores the inference adapter's detailed report (scores, field
evidence, review flags and ID agreement) as CLOB alongside EXTRACTED_JSON. Local
file paths are omitted by the existing adapter, and images remain in the source
table. Use NIN_CODE to correlate the report with source images and human review.
This worker does not deduplicate client requests: if two job rows share a NIN_CODE they are two
separate requests. It does not change CLIENT_IMAGE_OCR or CLIENTS_OCR.
