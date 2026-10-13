# Context (for resuming development)

## Decisions (user-approved, intentionally simple)
No outbox (API saves to DB then publishes directly), no retry/delay queues, no metrics stack, classic durable queues, persistent messages, manual acks, `prefetch=1` fair dispatch. All backend code under `backend/`. Frontend (`../frontend`, only image assets so far) comes later. DLQ args kept (cheap) for malformed/unknown-job messages.

## Status: backend implemented and verified locally
- Docker Compose (RabbitMQ 4 mgmt, Postgres 17, volumes, healthchecks). **Postgres is published on 5433** because a native Postgres owns 5432 on this machine.
- `OcrDemo.Shared` (contracts, EF Core, migration `InitialCreate`), `OcrDemo.Api` (http://localhost:5125), `OcrDemo.BusinessWorker`, `ocr-worker` (Python, pika 1.3.2, venv in `ocr-worker/.venv`), `scripts/start-workers.ps1`, `scripts/load-test.ps1`.
- Verified: 202 on submit; batch of 100 accepted in ~0.6 s; 3 OCR + 2 business workers finished 100 jobs in 72 s; restart recovery (unprocessed messages/rows survive); duplicate result ignored (`resultDeliveries` increments only).

## Commands
See README.md. Dev API port comes from `OcrDemo.Api/Properties/launchSettings.json` (5125).

## Gotchas
- Npgsql needs `EnableDynamicJson()` for Dictionary/List jsonb (done in `DbRegistration.AddOcrDb`).
- Message `startedAt` is `DateTimeOffset` (Python sends an offset-aware ISO string).
- Rebuilding while workers run fails (DLL locks); stop them first.
- In this environment, long bash heredocs failed once; use the Write tool for files, and PowerShell `Start-Process` for detached processes.

## Not done / outstanding
- No automated tests (integration tests were not built, to keep it simple); verification was manual.
- Not verified: DB-outage recovery and DLQ via dashboard (described in README, behavior follows from the code).
- No attempt limit on requeue; `TechnicalError` outcome is never persisted; no outbox (documented limitations).
- Containerizing the apps (Dockerfiles) is only described in the README.
- Frontend not started.
