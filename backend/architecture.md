# Architecture

```
Client ──► OcrDemo.Api ──1.save job──► PostgreSQL
              │ 2.publish (persistent, confirmed)
              ▼
        [ocr-jobs] ──prefetch 1──► ocr-worker (Python) ×N
                                        │ publish result, then ack job
                                        ▼
        [ocr-results] ──prefetch 1──► OcrDemo.BusinessWorker ×M ──txn──► PostgreSQL
Client ◄── GET /api/jobs/{id} ◄── OcrDemo.Api ◄── PostgreSQL
```

## Projects
| Project | Responsibility |
|---|---|
| `OcrDemo.Api` | Minimal API: submit/batch/status/stats. Saves the job, publishes to `ocr-jobs`. No consumers. |
| `OcrDemo.BusinessWorker` | `BackgroundService` consuming `ocr-results`; applies business rules; persists outcome. |
| `OcrDemo.Shared` | Message contracts, queue names/declaration, EF Core `DbContext`, entity, migrations. |
| `ocr-worker` | Python. `worker.py` = messaging; `inference.py` = isolated mock `run_ocr()` to replace with the real model. |

## Queues
`ocr-jobs` and `ocr-results`: durable classic queues, persistent messages, manual ack, `prefetch=1`. Each has `x-dead-letter-exchange=""` and `x-dead-letter-routing-key=<queue>.dlq`, with `<queue>.dlq` durable. All three apps declare queues with identical arguments (a mismatch makes RabbitMQ reject the declare).

## Message contracts (JSON, camelCase)
`ocr-jobs`: `{ jobId, correlationId, clientReference, imageReference }` (no image bytes).
`ocr-results`: `{ jobId, correlationId, ocrOutcome: Success|Unreadable|LowConfidence, extractedFields{nationalId,fullName,birthDate,expiryDate}|null, confidence, processingMs, startedAt, modelVersion }`.
AMQP `message_id` = jobId, `correlation_id` = correlationId.

## State
`Status`: `Queued` → `Completed` (terminal; set once, in the business worker's transaction).
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

## Scaling
Competing consumers: run more OCR or business worker processes against the same queues; `prefetch=1` gives fair dispatch. Throughput ≈ workers ÷ delay (3 workers × 2 s ≈ 1.5 jobs/s measured 1.4). Instance counts are not configured in code.
