# Async Spark OCR API — first implementation step

This adds a standalone inference API alongside the existing Oracle worker. It
does not change Oracle SQL, job statuses, thresholds or the OCR algorithms. The
Oracle worker still performs inference locally: the remote coordinator, Oracle
request-tracking migration and pending-result replay will be separate changes.
No existing Spark service is stopped or replaced by this change.

## Install and start

Run from the project root, using the machine's own Python environment:

```bash
python -m pip install -r backend/ocr-worker/requirements.api.txt
```

On a configured Spark installation use its `.venv/bin/python`. Keep the existing
CUDA-enabled PyTorch build; inspect dependency resolution before changing it.
The API requirements do not require `oracledb` or Oracle Client libraries.

Set these values in the project-root `.env` or service environment:

```dotenv
OCR_DEVICE=cuda
OCR_API_TOKEN=replace-with-a-long-random-service-secret
```

Start on a spare port while the old worker is in use:

```bash
python backend/ocr-worker/ocr_api.py --port 8001 --job-concurrency 3 --capacity 6
```

The CLI defaults to loopback (`127.0.0.1`). For access from another server, configure
approved network access and HTTPS via a reverse proxy, or use `--host` with the
approved private interface. Bearer tokens and images must not cross an untrusted
network over plain HTTP. Apply a request-body limit at the proxy (allow multipart
overhead above the two 20 MiB per-image limits) and bounded connection/upload
timeouts. Framework multipart parsing occurs before the submission handler;
the internal request capacity is not a substitute for ingress upload limits.

This launcher explicitly uses one Uvicorn process, without reload. Do not launch
multiple model-owning processes unintentionally. The lifespan loads models once,
creates persistent detection/field pools, then creates the bounded request store
and inference executor. Shutdown stops accepting submissions, drains accepted
jobs, and only then closes the stage pools.

## API contract

Except `/health/live`, endpoints require:

```http
Authorization: Bearer YOUR_SERVICE_TOKEN
```

| Endpoint | Behavior |
|---|---|
| `GET /health/live` | Process responsiveness; does not establish model health |
| `GET /health/ready` | Instance ID, readiness, request counts and capacity |
| `POST /ocr` | Multipart fields `request_id` (UUID), `job_id` (positive integer), `front`, `back`; returns 202 and Location |
| `GET /ocr/requests/{request_id}` | Current state, plus result when complete |
| `POST /ocr/requests/{request_id}/ack` | Acknowledge only after the caller confirms Oracle persistence |

The caller must generate and durably record the request ID **before** submission.
The same request ID and job ID with identical image bytes reuse the request; it
is not enqueued again. Different images or a different job ID give 409. A
duplicate during the original upload returns 409 `REQUEST_UPLOADING`; retry
later. A new request rejected because capacity is full returns 503
`CAPACITY_EXHAUSTED` and `Retry-After: 2`.

Example from a Linux terminal (environment variables avoid placing the secret
directly in a copied command):

```bash
export OCR_API_URL=http://127.0.0.1:8001
# Set OCR_API_TOKEN securely in your shell before running these commands.
request_id=$(python -c 'from uuid import uuid4; print(uuid4())')
curl -i "$OCR_API_URL/ocr" \
  -H "Authorization: Bearer $OCR_API_TOKEN" \
  -F "request_id=$request_id" -F 'job_id=123' \
  -F 'front=@/path/to/front.jpg' -F 'back=@/path/to/back.jpg'
curl "$OCR_API_URL/ocr/requests/$request_id" \
  -H "Authorization: Bearer $OCR_API_TOKEN"
```

States are `UPLOADING`, `QUEUED`, `RUNNING`, `COMPLETED`, `FAILED`, and
`ACKNOWLEDGED`. `COMPLETED` means inference returned an `OcrOutput`, including
normal `Rejected`, `Unreadable`, `LowConfidence` or `TechnicalError` outcomes.
It does not mean the Oracle job must be `OCR_SUCCESS`. Result fields are:

```json
{
  "requestId": "UUID",
  "jobId": 123,
  "status": "COMPLETED",
  "serviceInstanceId": "UUID-FOR-THIS-STARTUP",
  "result": {
    "ocrOutcome": "LowConfidence",
    "confidence": 0.74,
    "extractedFields": {},
    "details": {}
  }
}
```

An uncaught inference exception instead produces `FAILED` and
`errorCode: INFERENCE_ERROR`, without exposing exception contents. Invalid
images return 422 before acceptance; oversized images return 413.

## Concurrency and storage

With concurrency 3, up to three documents execute concurrently through shared
models and the existing persistent stage pools/locks. API request handling is
separate from inference. Capacity 6 counts all unacknowledged requests, including
uploads, queued/running jobs and completed/failed results. The executor cannot
accumulate an unbounded accepted backlog. The future Oracle coordinator should
initially keep at most three requests outstanding, freeing a slot only after
Oracle commit.

Original image bytes are stored under:

```text
try1/outputs/ocr_api/<service-instance>/<request-id>/uploads/<job-id>_1.<format>
try1/outputs/ocr_api/<service-instance>/<request-id>/uploads/<job-id>_2.<format>
```

Pipeline results, crops and annotations are placed under that request's
`results/` directory. User-provided filenames are not used as paths. Files are
isolated by service instance and request ID; directories are private on Linux.
The input validation is the same as the local Oracle worker.

By default diagnostic artifacts remain on disk, matching current testing
behavior. `--delete-acknowledged-artifacts` deletes a request's images and
pipeline artifacts after acknowledgement. It never deletes an active request.
There is no age-based cleanup of old service-instance directories in this step.

Unacknowledged result JSON is held in memory until ACK; results are not evicted
to make room for new work. ACK releases the large result and capacity. A small
in-memory record supports repeat acknowledgements and duplicate submissions for
the latest 1024 acknowledgements. This is bounded deduplication, not a permanent
history: callers must not reuse IDs for new attempts.

## Restart and recovery limits

Every startup gets a different `serviceInstanceId`. Queue, status, results and
acknowledgement history are **memory only**. Existing artifact files are not
automatically reloaded. Unknown IDs return 404 with the current instance ID.
Temporary unavailability alone does not prove a request was lost.

The future coordinator must persist request tracking in Oracle, detect a Spark
restart, and resubmit lost requests with both images read again from Oracle.
Do not point live producers at this API expecting durable accepted work or
automatic Oracle recovery yet. There is no RabbitMQ dependency and no database
connection from this service.

## Logs and checks

Console output is mirrored using the same logging helper as the Oracle worker:

```text
backend/ocr-worker/logs/ocr_api_<timestamp>_api_<PID>.log
```

Lines include timestamp, level, PID, launcher PID and role. Request events also
include job/request IDs; inference events include thread names and completion
duration. Startup, readiness, capacity rejection, failures, acknowledgements and
shutdown are logged. Tokens, image bodies, extracted values and exception
messages are not included in application request logs. Python progress output
is mirrored too; native-library stderr may bypass Python stream capture.

```bash
python -m pip install -r backend/ocr-worker/requirements.api-test.txt
python -m unittest discover -s backend/ocr-worker -p 'test_ocr_api.py'
```

Tests use small synthetic PNGs, fake inference and real job threads; they do not
load weights, contact Oracle, or claim jobs. Real GPU/field parity and sustained
memory tests are required when deploying this API on Spark. The next change is
the Oracle request-tracking migration and HTTP coordinator.
