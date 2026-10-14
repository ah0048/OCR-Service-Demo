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

## Frontend (Angular 22, `../frontend`)
Layout: `core/` (services, models), `components/<name>/` and `pages/<name>/`, each with .ts/.html/.scss. Pages: Overview, Queues & workers, Jobs (live table), Batches, plus a job drawer and a submit dialog. Data: one SSE stream (`DashboardStore`) plus REST for the jobs table/drawer. Brand images are in `frontend/public` (cover banner, EGX logo, emblem; `unnamed.png` is the reference screenshot). API URL constant: `frontend/src/app/api.ts`. Run: `cd frontend; npm install; npm start` -> http://localhost:4200.

Results page: `GET /api/results/stream` (SSE, polls the DB every 500 ms, re-checks the last 5 s and de-duplicates by job id because several business workers can commit out of timestamp order) feeds `core/results-store.ts`. Images are placeholders for now (`ImageReference` is just a string).

## Not done / outstanding
- No automated tests (integration tests were not built, to keep it simple); verification was manual.
- Not verified: DB-outage recovery and DLQ via dashboard (described in README, behavior follows from the code).
- No attempt limit on requeue; `TechnicalError` outcome is never persisted; no outbox (documented limitations).
- Containerizing the apps (Dockerfiles) is only described in the README.
- Dashboard has no automated tests; verified with headless Edge screenshots. Latency stats use the last 5 minutes of completions. Chart history starts empty on page load (client-side).
