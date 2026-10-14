# Architecture

```
Client ──► OcrDemo.Api ──1.save job──► PostgreSQL
              │ 2.publish (persistent, confirmed)
              ▼
        [ocr-jobs] ──prefetch 1──► ocr-worker (Python) ×N
                                        │ │ (a) when it STARTS a job: best-effort event
                                        │ └──────────► [job-events] ──► OcrDemo.BusinessWorker ×M
                                        │                              (ProgressConsumer: Queued → OcrInProgress)
                                        │ (b) when it FINISHES: publish result, then ack the job
                                        ▼
        [ocr-results] ──prefetch 1──► OcrDemo.BusinessWorker ×M ──txn──► PostgreSQL
                                      (ResultConsumer: rules → Completed + outcome)
Client ◄── GET /api/jobs/{id} ◄── OcrDemo.Api ◄── PostgreSQL
```

`ocr-jobs` → `ocr-results` is the **main path**. `job-events` is a **side channel**: it carries no business data and nothing depends on it, it only lets the dashboard show a job as "OCR processing" while it runs.

## Projects
| Project | Responsibility |
|---|---|
| `OcrDemo.Api` | Minimal API: submit/batch/status/stats. Saves the job, publishes to `ocr-jobs`. No consumers. |
| `OcrDemo.BusinessWorker` | Two `BackgroundService`s in one process: `ResultConsumer` (`ocr-results`: applies business rules, persists the outcome) and `ProgressConsumer` (`job-events`: marks a job `OcrInProgress`). |
| `OcrDemo.Shared` | Message contracts, queue names/declaration, EF Core `DbContext`, entity, migrations. |
| `ocr-worker` | Python. `worker.py` = messaging; `inference.py` = isolated mock `run_ocr()` to replace with the real model. |

## Queues
| Queue | Producer | Consumer | Purpose |
|---|---|---|---|
| `ocr-jobs` | API | Python OCR workers | work to do (main path) |
| `ocr-results` | Python OCR workers | `ResultConsumer` (.NET) | OCR output to validate (main path) |
| `job-events` | Python OCR workers | `ProgressConsumer` (.NET) | "OCR started" notifications (side channel, dashboard only) |

All three are durable classic queues, persistent messages, manual ack, `prefetch=1`. Each has `x-dead-letter-exchange=""` and `x-dead-letter-routing-key=<queue>.dlq`, with `<queue>.dlq` durable. All three apps declare queues with identical arguments (a mismatch makes RabbitMQ reject the declare).

## Message contracts (JSON, camelCase)
`ocr-jobs`: `{ jobId, correlationId, clientReference, imageReference }` (no image bytes).
`ocr-results`: `{ jobId, correlationId, ocrOutcome: Success|Unreadable|LowConfidence, extractedFields{nationalId,fullName,birthDate,expiryDate}|null, confidence, processingMs, startedAt, modelVersion, workerId }`.
`job-events`: `{ jobId, workerId, startedAt }`, published by the Python worker right before it runs OCR.
AMQP `message_id` = jobId, `correlation_id` = correlationId.

## State
`Status`: `Queued` → `OcrInProgress` → `Completed`. `OcrInProgress` is set by `ProgressConsumer` from a `job-events` message, only `WHERE Status = 'Queued'`, so it can never move a job backwards. It is optional: if the event is lost or arrives after the result, the job simply goes `Queued` → `Completed`. `Completed` is terminal and is set once, in the business worker's transaction.
`Outcome` (business result, set with Completed): `Succeeded` | `ValidationFailed` | `ManualReview` | `OcrFailed` | (`TechnicalError` reserved).
Rules (`BusinessRules.cs`): Unreadable → `OcrFailed`; LowConfidence or confidence < threshold (0.80) → `ManualReview`; else check name present, birth date valid, 14-digit national ID consistent with birth date, expiry not in the past → `Succeeded` / `ValidationFailed` with reason codes.
Business rejections are final results (acked), not errors.

## Failure handling
| Situation | Behavior |
|---|---|
| Malformed message / unknown job | `nack requeue=false` → `<queue>.dlq` |
| Exception in consumer (e.g. DB down) | sleep 3 s, `nack requeue=true` (no attempt limit) |
| Worker crash before ack | broker redelivers to another consumer |
| Duplicate result | row lock + `Status==Completed` check → ignored, `ResultDeliveries++` |
| Publish fails in API | job stays `Queued` in DB, API returns 503 (no outbox) |
| `job-events` message lost, malformed, or fails to apply | logged and acked; never retried or dead-lettered (informational only) |
| Python can't publish the started event | warning logged, the job is processed normally |

## Scaling
Competing consumers: run more OCR or business worker processes against the same queues; `prefetch=1` gives fair dispatch. Throughput ≈ workers ÷ delay (3 workers × 2 s ≈ 1.5 jobs/s measured 1.4). Instance counts are not configured in code.

## Dashboard support (added with the frontend)
- The `job-events` queue is described above (diagram, queue table, contracts, state).
- `jobs` gained `BatchId`, `BatchName`, `OcrWorkerId`, `BusinessWorkerId` (migration `AddBatchAndWorkerTracking`). The result message carries `workerId`.
- `DashboardService` builds a snapshot per second from PostgreSQL aggregates plus the RabbitMQ management API (`/api/queues`, `/api/channels`, `/api/consumers`, `/api/connections`; connections are mapped back to the names workers register, e.g. `ocr-HOST-1234`). Queue rates are computed from counter deltas over 10 s because the management plugin publishes counters in bursts.
- Worker lists merge live consumers with workers seen in the DB, so stopped workers show as offline with their processed counts.
