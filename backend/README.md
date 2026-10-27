# Egyptian ID OCR queue pipeline

The API accepts front/back card images, saves them to shared storage, and returns **202 Accepted** after publishing job references to RabbitMQ. Real OCR runs in background Python processes by calling `try1.pipeline.document.run_document` directly. No OCR HTTP server is needed.

```text
Angular upload -> .NET API -> shared image files + PostgreSQL
                        -> RabbitMQ ocr-jobs -> real Python OCR workers x N
                                                    |
                                    RabbitMQ ocr-results
                                                    |
                                      .NET business workers x M
                                                    |
                                            PostgreSQL
                                                    |
                                    API / results SSE -> Angular
```

The `job-events` queue carries best-effort OCR-started notifications. Each OCR worker has prefetch 1 and handles one document at a time. Models are loaded once per process before it consumes jobs. RabbitMQ heartbeats continue while inference runs.

## Run locally

Prerequisites: .NET 10, Node/npm for the frontend, Docker Desktop, and a Python environment compatible with the supplied offline model assets. The existing `try1/.venv` can be reused.

From the repository root, double-click `start.cmd` or run:

```cmd
start.cmd
```

This prepares missing Python/frontend dependencies, runs Docker Compose and waits for healthy PostgreSQL/RabbitMQ, starts the API (including Development migrations), starts **3 OCR workers and 2 business workers**, and starts Angular. Open http://localhost:4200. Docker Desktop must already be running; .NET 10 SDK, Node.js/npm, Python 3.11+ and the `try1` offline model assets must be installed/provided. Existing `try1/.venv` is reused; otherwise a worker environment is created at `backend/ocr-worker/.venv`.

Optional overrides:

```cmd
start.cmd -OcrWorkers 3 -BusinessWorkers 2 -Device cpu
start.cmd -OcrWorkers 2 -BusinessWorkers 2 -Device cuda -PythonExe "D:\ocr-env\Scripts\python.exe"
```

Services run in the background with PIDs and logs printed under `.data/logs`. Existing healthy API/frontend instances on ports 5125/4200 are reused. Each invocation starts a new set of workers, so stop previous worker PIDs before rerunning if you do not want additional instances. Each OCR worker loads its own model copy. Missing private model assets must be supplied separately; they are not downloaded by the launcher.

To run the components manually from the repository root:

```powershell
docker compose -f backend/docker-compose.yml up -d --wait

# Reuse the real model's environment (its model dependencies are already installed):
try1/.venv/Scripts/python.exe -m pip install -r backend/ocr-worker/requirements.txt

# API: Development applies all EF migrations, including AddOcrDetails.
dotnet run --project backend/OcrDemo.Api

# In another terminal, start background workers with logs/PIDs:
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/start-workers.ps1 -OcrWorkers 1 -BusinessWorkers 2 -Device cpu

# Frontend, in another terminal:
cd frontend
npm.cmd install
npm.cmd start
```

Open http://localhost:4200. Use **Submit jobs**, select both images for a single card, or select multiple paired images in **Batch**. Pair filenames as `3967452_1.jpg` (front) / `3967452_2.jpg` (back). Only numeric person numbers with suffix _1 / _2 are accepted for batch uploads. The dialog lists the matched pairs before submission.

For a new Python environment:

```powershell
python -m venv backend/ocr-worker/.venv
backend/ocr-worker/.venv/Scripts/python.exe -m pip install -r backend/ocr-worker/requirements.real.txt
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/start-workers.ps1 -PythonExe backend/ocr-worker/.venv/Scripts/python.exe -OcrWorkers 1 -BusinessWorkers 1
```

Model assets stay in `try1`; see [its asset manifest](../try1/ASSETS.md) and [CUDA instructions](../try1/CUDA_README.md). Missing assets, dependencies or an unavailable requested device stop the worker before it consumes any job. The standalone OCR API in `try1/api` remains available for separate use.

To run one OCR worker manually:

```powershell
$env:OCR_DEVICE = "cpu"
try1/.venv/Scripts/python.exe backend/ocr-worker/worker.py
```

To run a business worker manually, set `DOTNET_ENVIRONMENT=Development` and run `dotnet run --project backend/OcrDemo.BusinessWorker`.

The launcher prints process IDs and log locations under `.data/logs`. Stop a background instance with `taskkill /PID <PID> /T /F`. An interrupted unacknowledged job is redelivered. Each OCR instance loads a separate copy of the models: start with one, measure memory and throughput, and increase counts to fit RAM/VRAM. More processes on a saturated CPU/GPU can reduce throughput.

## Images and submission contracts

The default shared image store is `<repo>/.data/images/<opaque-reference>/front` and `back`. The API accepts JPEG, PNG, WEBP, BMP or TIFF signatures, up to 20 MB per image, 100 cards and 256 MB per upload. The worker performs full image validation (including pixel/frame limits). Corrupt images become final `OcrFailed` results rather than endless retries. Batch images are matched **by numeric filename prefix**, independently of upload order. Duplicate sides, missing partners, nonnumeric prefixes and incorrect side fields are rejected. The numeric prefix becomes the client reference; single uploads still take an explicit clientReference.

| Method | Path | Body / behavior |
|---|---|---|
| POST | `/api/jobs/upload` | Multipart: one `front` file, one `back` file, `clientReference`. Returns `jobId` and `statusUrl`. |
| POST | `/api/jobs/batch/upload` | Multipart: repeated `front` and `back` files named number_1.ext / number_2.ext, optional `batchName`. Returns `batchId`, `count`, `firstJobId`. |
| POST | `/api/jobs` | JSON `{ "clientReference": "C1", "imageReference": "<existing-upload-reference>" }`; reuses an uploaded pair. |
| POST | `/api/jobs/batch` | JSON `{ "batchName": "B1", "jobs": [{ "clientReference": "C1", "imageReference": "<existing-upload-reference>" }] }`. Up to `Batch:MaxSize` (10,000). |
| GET | `/api/jobs/{id}` | Status, business outcome, reason codes, extracted fields and detailed OCR report. |
| GET | `/api/jobs/{id}/images/front` | Original uploaded front image, with `no-store` caching. Use `back` for the other side. |
| GET | `/api/jobs` | Paged jobs; filters `page`, `pageSize`, `status`, `outcome`, `batchId`, `search`. |
| GET | `/api/results/stream` | SSE, latest `backlog=30` results first, then live completions with fields and detailed reports. |
| GET | `/api/dashboard/stream` | SSE dashboard snapshots with queue/worker/batch progress. |
| GET | `/api/dashboard/snapshot` | One dashboard snapshot. |
| GET | `/api/stats` | Counts by status/outcome. |
| GET | `/health` | API and database health. |

The old count-only synthetic batch request is replaced by real image references. Existing mock jobs will be unreadable if delivered to the real worker; finish or remove old demo work before switching running consumers. Do not run old dummy workers alongside real ones on `ocr-jobs`.

The API must receive and save uploads before returning 202, so response time includes image transfer, file storage, database write and queue publication. It never includes OCR inference.

## Results and review

The string-valued field dictionary remains compatible with business processing. It includes full name, address, national ID, expiry date, gender, occupation, spouse name, religion and marital status when present, plus each side's ID and any additional extracted fields. `birthDate` is derived from the national ID; its source is recorded in the report. Dates from the date reader use ISO format.

`ocr.details` preserves the front/back extraction statuses, review flags, recognizer evidence, ID agreement, classifier scores and pipeline verdict. Temporary crop/model paths are omitted. Crops and intermediate reports are removed after processing; source images remain in shared storage.

The worker computes a **heuristic overall confidence** from the OCR report. It averages nine required side-specific fields (front ID/name/address; back ID/expiry/gender/job/marital/religion), adding spouse only when detected/read. A field with a usable value uses the lower of selected-crop detection confidence and recognition confidence when both exist. Recognition uses digit minimum confidence, date recognition minimum confidence or classifier score. Text fields without a recognition score use detection as an explicit proxy. Missing/unreadable fields or fields with no score contribute zero. An unverified/conflicting ID gate makes the overall score zero.

The exact formula, per-field scores, recognition/detection sources and pre-gate mean are available in `ocr.details.confidenceCalculation`; `confidenceKind` is `heuristic`. This is an evidence estimate, not calibrated text accuracy. A valid score at or above Business:ConfidenceThreshold (default 0.75) is sufficient for Succeeded, regardless of model review flags or missing/invalid field and expiry checks. Below-threshold or unavailable/invalid scores require ManualReview. Rejected ID conflicts, unreadable inputs and technical errors retain their separate outcomes. Existing completed results are not recalculated automatically.

A front/back ID conflict produces a final **Rejected** business outcome with `FRONT_BACK_ID_MISMATCH`, preserving the pipeline's early stop and conflict evidence. Missing/unreadable IDs without an actual conflict retain review/failure behavior. Lifecycle status remains `Completed` for all terminal outcomes so queue/batch tracking stays consistent.

Statuses: `Queued -> OcrInProgress -> Completed`. Outcomes: `Succeeded`, `ValidationFailed`, `ManualReview`, `Rejected`, `OcrFailed`, `TechnicalError`. An unhandled model exception produces a terminal technical error; invalid image pairs produce OCR failure.

The job drawer displays a live total timer with queue and OCR times, updated every second independently of dashboard SSE and frozen at completion.

## Configuration

.NET uses `appsettings*.json` and environment variables:

- `Images__Root`: absolute shared image directory, default `<repo>/.data/images`.
- `Batch__MaxUploadSize`: max cards per multipart request, default 100.
- `Batch__MaxSize`: max references per JSON batch, default 10,000.
- `ConnectionStrings__Default`, `RabbitMq__Host/User/Password`, `Business__ConfidenceThreshold`.
- `Database__AutoMigrate`: enabled in Development. Deploy migrations before starting business workers.
- `Cors__Origins`: defaults to http://localhost:4200.

Python:

- `OCR_IMAGE_ROOT`: the **same storage** as API `Images__Root`.
- `OCR_PROJECT_ROOT`: folder containing `project_config.py`, default `<repo>/try1`.
- `OCR_DEVICE`: `cpu` (default), `cuda`, `mps` or `auto`.
- `OCR_MODEL_VERSION`: result provenance label, default `egyptian-id-try1-v1`.
- `RABBITMQ_HOST/PORT/USER/PASSWORD`, `LOG_LEVEL`.

For workers on separate machines/containers, mount the shared image directory at each host's configured path. Queue messages carry opaque references, never image bytes or host-specific absolute paths. Containers use service names for RabbitMQ/PostgreSQL, rather than localhost. Infrastructure remains in [docker-compose.yml](docker-compose.yml).

## Verification

```powershell
dotnet build backend/OcrDemo.sln --no-restore
npm.cmd --prefix frontend run build
try1/.venv/Scripts/python.exe -m unittest discover -s backend/ocr-worker -p test_worker.py
dotnet run --project backend/tests/Pipeline.Regression

# With API and workers running; submits one real card and two corrupt-image test jobs:
python backend/scripts/verify-integration.py --front try1/images/CARD_1.jpg --back try1/images/CARD_2.jpg

# Repeat real OCR on an already-uploaded pair:
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/load-test.ps1 -ImageReference <reference-from-job-detail> -Count 10
```

Regression tests cover field mapping, review preservation, ID conflicts, storage traversal, heartbeat servicing, terminal errors, malformed messages and result publication before acknowledgement. The integration check verifies real extraction, batch completion, database persistence, original image previews and SSE, without printing extracted personal data.

## Delivery guarantees and remaining limitations

Durable queues, persistent messages, publisher confirms, prefetch 1 and manual acknowledgements provide **at-least-once** delivery. OCR jobs are acknowledged only after result publication is confirmed. Business workers acknowledge only after their database transaction commits; row locking makes duplicate result delivery idempotent.

The existing API save-then-publish design still has **no transactional outbox**. A crash or broker outage between persistence and publication can strand queued jobs; a detected publication failure returns 503 and retains their images. Database/broker failures are retried through redelivery; inference exceptions are recorded as final technical errors. There is no delayed retry policy for inference failures.

This local demo retains uploaded images and extracted personal data, has no application authentication or retention policy, and uses development infrastructure credentials. Add the required access controls, storage lifecycle and durable outbox before deploying it with real customer data. The migration adds a nullable JSONB report column and preserves existing results.
