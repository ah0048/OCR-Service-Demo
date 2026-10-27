# Current project context

As of 2026-10-20, the queue pipeline uses the real offline OCR project in `../try1`, through `ocr-worker/inference.py`. The OCR API is not involved.

- API single/batch multipart uploads save front/back pairs to shared storage; JSON endpoints can requeue existing uploaded references. The old synthetic count-only batches are replaced.
- The default shared store is `<repo>/.data/images`; configure `Images__Root` and `OCR_IMAGE_ROOT` together on other hosts.
- Each worker preloads models before consuming, uses prefetch 1 and runs inference on a separate thread while Pika services heartbeats.
- Detailed reports are persisted in nullable JSONB `jobs.OcrDetails` (migration `AddOcrDetails`). All extracted string fields remain in `ExtractedFields`; birth date is derived from national ID.
- New real results have a heuristic confidence: mean field evidence, recognition/detection scores documented per field, missing required fields zero, ID gate failure zero. Acceptance uses the configured confidence threshold alone (default 75%), overriding model review flags and field/date validation; ID conflicts, unreadable images and technical failures remain separate terminal outcomes. Existing completed results keep their original confidence.
- Actual front/back ID conflicts finish as Rejected with FRONT_BACK_ID_MISMATCH (lifecycle Completed). Invalid image pairs finish as OcrFailed; unhandled inference errors finish as TechnicalError. Broker/database failures still use at-least-once delivery and duplicate result protection.
- Angular single and batch dialogs upload real pairs. Batch filenames must be numeric person-number_1.ext (front) and the same person-number_2.ext (back); both API and frontend enforce this and pair independently of selection order. The job drawer has an independent one-second elapsed/queue/OCR timer that freezes at completion. The results and business views show original images, all fields and review outcomes. The technical JSON includes extraction evidence.
- `scripts/start-workers.ps1` launches hidden background instances and prints PIDs/log locations. Its default interpreter is `../try1/.venv/Scripts/python.exe`; install pika there from `ocr-worker/requirements.txt`. Fresh environments use `requirements.real.txt`.
- Python regressions: `python -m unittest discover -s backend/ocr-worker -p test_worker.py` from repo root with an environment containing pika.
- End-to-end check: `scripts/verify-integration.py`, which accepts sample front/back paths. Verified locally: single upload 202 in ~0.55s, real CPU OCR ~123s, 12 extracted/derived fields, manual review, batch corrupt-image handling, DB persistence, previews, SSE and duplicate-result idempotency.
- Backend and Angular builds pass. Angular retains an existing navbar stylesheet budget warning.
- `try1/` is an existing nested Git checkout, currently untracked by the outer repository. Model assets are private/offline; do not delete the nested repository or commit weights as part of this integration.

Remaining limitations: no transactional outbox, no delayed inference retries, no application authentication/retention policy, and no application Dockerfiles. See README.md for current contracts, configuration and setup. Infrastructure uses RabbitMQ 4 and PostgreSQL 17 (host port 5433). Stop API/business processes before a rebuild if Windows locks their DLLs.

UI sizing remains rem-based with container queries and the A-/A+ control. The API URL is in `frontend/src/app/core/api.ts`.
