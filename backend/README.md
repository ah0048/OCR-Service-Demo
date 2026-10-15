# Mock OCR Pipeline Demo (Egyptian National ID)

A proof-of-concept showing how thousands of ID-image OCR jobs can be processed asynchronously by independently scalable workers. All OCR output is **synthetic**; there are no real images or personal data.

```
POST /api/jobs ─► OcrDemo.Api ─► PostgreSQL (job saved first)
                      │
                      └─► RabbitMQ [ocr-jobs] ─► Python OCR worker ×N ─► RabbitMQ [ocr-results]
                                                     │                          │
                                  [job-events] ◄─────┘ (side channel:           │
                                       │        "OCR started", dashboard only)  │
                                       ▼                                        ▼
GET /api/jobs/{id} ◄── PostgreSQL ◄── OcrDemo.BusinessWorker ×M (consumes both queues)
```

Queues: `ocr-jobs` (work), `ocr-results` (OCR output), and `job-events` (best-effort "OCR started" notices so the dashboard can show a job as in progress; nothing depends on it).

See [architecture.md](architecture.md) for details and [context.md](context.md) for project state.

## Prerequisites
.NET 10 SDK, Python 3.12+ (tested on 3.14), Docker Desktop, `dotnet-ef` tool (`dotnet tool install -g dotnet-ef`).

## Run it

All commands from `backend/`.

**1. Infrastructure** (RabbitMQ 4 + PostgreSQL 17, persistent volumes, health checks):
```powershell
docker compose up -d --wait
```
- RabbitMQ dashboard: http://localhost:15672 (user `ocr` / password `ocr_dev_pw`)
- PostgreSQL is published on host port **5433** (not 5432, to avoid clashing with a local Postgres). Change with `POSTGRES_PORT`.
- Credentials are dev defaults, overridable through `RABBITMQ_USER/PASSWORD`, `POSTGRES_USER/PASSWORD/DB` env vars (or a `.env` file). Never reuse them in production.

**2. Python OCR worker setup (once):**
```powershell
cd ocr-worker
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
cd ..
```

**3. API** (applies the EF migration on startup in Development):
```powershell
dotnet run --project OcrDemo.Api
```
Swagger: http://localhost:5125/swagger

**4. Workers**, any number of each, in separate windows:
```powershell
./scripts/start-workers.ps1 -OcrWorkers 3 -BusinessWorkers 2          # helper
# or manually:
cd ocr-worker; .venv\Scripts\python worker.py                          # OCR worker
dotnet run --project OcrDemo.BusinessWorker                            # business worker (set DOTNET_ENVIRONMENT=Development)
```
Instance counts are not configured anywhere; start as many processes as you want.

## API
| Method | Path | Description |
|---|---|---|
| POST | `/api/jobs` | `{ "clientReference": "C1", "imageReference": "id-001.jpg" }` → `202` + `jobId` + `statusUrl` |
| POST | `/api/jobs/batch` | `{ "count": 1000, "clientReferencePrefix": "LOAD" }` → `202` (max 10,000, config `Batch:MaxSize`) |
| GET | `/api/jobs/{id}` | status, outcome, reason codes, OCR output, timestamps, `resultDeliveries` |
| GET | `/api/jobs` | paged table: `?page=&pageSize=&status=&outcome=&batchId=&search=` |
| GET | `/api/stats` | counts by status and outcome |
| GET | `/api/dashboard/stream` | server-sent events: a full snapshot every second (totals, throughput, queues, workers, batches) |
| GET | `/api/dashboard/snapshot` | the same snapshot, once |
| GET | `/api/results/stream` | server-sent events: one message per finished job (OCR output, outcome, reason codes); starts with the latest `?backlog=30` results (sent as `event: backlog`, so the UI can tell them from live ones), then pushes new ones as they complete |
| GET | `/health` | API + DB health |

Status: `Queued` → `OcrInProgress` (an OCR worker picked it up) → `Completed`. Outcome: `Succeeded`, `ValidationFailed`, `ManualReview`, `OcrFailed`, `TechnicalError` (reserved; see Limitations).

## Dashboard
The Angular dashboard in `../frontend` reads the stream above. Queue and worker data come from the RabbitMQ management HTTP API (`RabbitMq:ManagementUrl`, default `http://localhost:15672`, same credentials), proxied by the API so the browser never talks to RabbitMQ. CORS allows `http://localhost:4200` (`Cors:Origins`).
A third queue, `job-events`, carries best-effort "OCR started" events from the Python workers; the business worker consumes it and moves the job to `OcrInProgress`. Losing one is harmless.

## Load test / scaling demo
```powershell
./scripts/start-workers.ps1 -OcrWorkers 1 -BusinessWorkers 1
./scripts/load-test.ps1 -Count 100        # ~200 s with 1 OCR worker (2 s per job)
# stop workers, then:
./scripts/start-workers.ps1 -OcrWorkers 3 -BusinessWorkers 2
./scripts/load-test.ps1 -Count 100        # ~70 s: throughput scales with worker count
```
The script prints how long the API took to accept the batch (hundreds of ms) versus how long processing took, which shows submission never waits for OCR. Watch the queues in the RabbitMQ dashboard (Queues tab: ready/unacked, consumers). The mock OCR delay is `OCR_PROCESSING_DELAY_SECONDS` (default 2, `-OcrDelaySeconds` in the helper).

Measured here: 100 jobs accepted in ~0.6 s; processed in 72 s with 3 OCR workers.

## Verify behaviors
- **Persistence/restart:** stop the workers mid-batch and restart them; unacked messages are redelivered, queued ones survive in the durable queues, and job rows survive in the Postgres volume. `docker compose restart` also keeps data.
- **Duplicates:** republish a result message for a completed job. The job is unchanged and only `resultDeliveries` increments.
- **Dead-letter:** publish garbage to `ocr-jobs` from the RabbitMQ dashboard. It lands in `ocr-jobs.dlq`.
- **Recovery:** stop PostgreSQL while jobs flow (`docker compose stop postgres`); the business worker logs errors and requeues, then catches up after `docker compose start postgres`.

## Migrations
Migrations live in `OcrDemo.Shared/Migrations`. The API applies them at startup when `Database:AutoMigrate=true` (Development default). Manually:
```powershell
dotnet ef database update --project OcrDemo.Shared --startup-project OcrDemo.Api
dotnet ef migrations add <Name> --project OcrDemo.Shared --startup-project OcrDemo.Api --output-dir Migrations
```

## Configuration
.NET: `appsettings*.json` or env vars (`ConnectionStrings__Default`, `RabbitMq__Host`, `RabbitMq__User`, `RabbitMq__Password`, `Business__ConfidenceThreshold`).
Python: `RABBITMQ_HOST/PORT/USER/PASSWORD`, `OCR_PROCESSING_DELAY_SECONDS`, `LOG_LEVEL`.

## Delivery guarantees and limitations
- **Messages:** durable classic queues, persistent messages, publisher confirms, manual acks, `prefetch=1` (fair dispatch: a worker gets one message only after acking the previous one).
- **At-least-once** end to end. Python acks the job only after the result is published and confirmed; the business worker acks only after the DB transaction commits. Duplicates are therefore possible and handled: the business worker locks the job row (`SELECT ... FOR UPDATE`) and ignores results for jobs already `Completed`. The mock OCR is deterministic, so a re-run produces the same result.
- **No outbox (deliberate):** the API saves the job, then publishes. If the process crashes or RabbitMQ is down between those two steps, the job stays `Queued` in the DB and is never published (the API returns 503 when it detects the failure). A production system should use a transactional outbox or a reconciliation sweep.
- **No retry strategy:** a failure in a consumer (exception) requeues the message after a short sleep, which can loop if the failure is permanent and transient-looking. Malformed messages and unknown job IDs are rejected without requeue and go to `<queue>.dlq`. There is no attempt limit; add a retry-count/delay-queue scheme if needed.
- `TechnicalError` exists as an outcome but is never persisted, because technical failures are retried rather than recorded.
- Single RabbitMQ and Postgres node, classic queues (not quorum), no auth beyond dev credentials, no metrics endpoint (use the RabbitMQ dashboard, worker logs and `/api/stats`).
- Ordering across jobs is not guaranteed. Logs contain job IDs and outcomes only, never extracted field values.

## Containerizing later
Inside Docker Compose, containers reach each other by **service name**, not `localhost` (which would be the container itself). For containerized apps set `RabbitMq__Host=rabbitmq`, `ConnectionStrings__Default=Host=postgres;Port=5432;...` (the internal port 5432, not the published 5433), and `RABBITMQ_HOST=rabbitmq` for Python. Add a `Dockerfile` per app, add them as services with `depends_on: condition: service_healthy`, and scale with `docker compose up --scale ocr-worker=3 --scale business-worker=2`. Apps running on the Windows host keep using `localhost` and the published ports.
