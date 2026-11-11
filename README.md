# Oracle OCR worker

Standalone Python service for Oracle 11.2.0.4. It claims pending requests from
`BRK_COD.CLIENT_IMAGE_OCR_JOB`, reads front/back BLOBs from
`CLIENTSOWNER.CLIENT_IMAGE_OCR`, runs the offline OCR models, and saves typed
fields plus extracted/detailed JSON back into the job row. Their validation
stored procedure owns FINISHED. Every result is still subject to human review.

This branch contains the Oracle worker and inference pipeline. The previous
frontend, .NET services, RabbitMQ/PostgreSQL integration, Docker and SSH launchers
have been removed. Oracle connections use the network directly.

## Layout

- `backend/ocr-worker/`: Oracle entry point, configuration/logging, inference and tests.
- `try1/core/` and `try1/pipeline/`: inference code and model assets.
- `database/oracle/`: schema, optional migrations, and the detailed run guide.
- `scripts/package_oracle_worker.py`: creates a transfer archive and checksums.

## Run locally

From Git Bash on the current Windows machine, the existing working environment is:

```bash
./try1/.venv/Scripts/python.exe backend/ocr-worker/oracle_worker.py --check-connection
./try1/.venv/Scripts/python.exe backend/ocr-worker/oracle_worker.py --workers 1
```

Copy `.env.example` to `.env` on each new machine and supply local settings.
No real credentials or environments are included in transfer packages.
See [the run guide](database/oracle/RUN_WORKER.md) for DSN/SID syntax, Oracle Client
19c setup, Unicode columns, logs, worker counts and graceful shutdown.

Defaults: one model-owning process, two-second idle polling, 80% word-classifier
threshold and 50% overall acceptance threshold. Agreed/reconciled IDs continue;
unresolved conflicts/unverified IDs stop. There are no automatic job retries or
crash recovery. Each process loads its own models.

For shared-model concurrency on CUDA, start one process with two job threads:

```bash
.venv/bin/python backend/ocr-worker/oracle_worker.py --workers 1 --job-concurrency 2
```

`--workers` controls model-owning processes; `--job-concurrency` controls jobs per
process (1..8, default 1). Each job thread owns its Oracle connection. Models load
once before threads start. CUDA/CPU pipelines overlap while YOLO access per model
and Qari generation remain locked. Metal retains whole-document serialization.
Qari batching remains separately configurable with `QARI_BATCH_SIZE`; this change
does not batch crops across jobs. Start with concurrency 2 and compare throughput
and peak memory on the same batch. Active tensors still consume extra memory.

## Package for another machine

Use Python's standard library (no installed OCR dependencies needed):

```bash
python scripts/package_oracle_worker.py
```

This creates `.data/dist/oracle-ocr-<timestamp>.tar.gz` and its `.sha256` file.
The package includes an explicit allowlist of code, SQL, docs, focused tests,
the six `.pt` checkpoints, and the complete merged Qari model/tokenizer folder.
It excludes `.env`, Git metadata, Python environments, caches, logs, uploaded
client images, result folders, benchmarks and local test artifacts. A per-file
SHA-256 manifest is embedded in the archive. Do not zip the whole workspace.

```bash
python scripts/package_oracle_worker.py --verify .data/dist/ARCHIVE_NAME.tar.gz
```

Use `--code-only` only when the target already has the same weights at the same
relative paths. Model files are ignored by Git; a clone alone is incomplete.

## Receiving Linux GPU machine

1. Transfer the archive and checksum. Verify with `sha256sum -c ARCHIVE_NAME.tar.gz.sha256`.
2. Extract into a new empty project directory: `tar -xzf /path/to/ARCHIVE_NAME.tar.gz`.
3. Create a native environment: `python3 -m venv .venv`. Do not copy a Windows venv.
4. Install dependencies: `.venv/bin/python -m pip install -r backend/ocr-worker/requirements.oracle.txt`.
5. Verify the NVIDIA driver and CUDA-enabled PyTorch; follow [CUDA notes](try1/CUDA_README.md).
6. Install compatible Oracle Client 19c libraries and configure their Linux search
   path before launching Python; the library/OS dependencies are in the run guide.
7. Copy `.env.example` to `.env`, set the approved Oracle DSN/user/password and
   `OCR_DEVICE=cuda`. Leave ORACLE_CLIENT_LIB_DIR empty on Linux.
8. Run `.venv/bin/python backend/ocr-worker/oracle_worker.py --check-connection`.
9. Start with `.venv/bin/python backend/ocr-worker/oracle_worker.py --workers 1`.

Confirm the host's OS/architecture, GPU driver, Oracle network reachability and
package access with its administrator. Oracle Client and Python dependencies are
not bundled; a machine without internet needs matching offline wheels and the
approved Oracle Client package transferred separately. Inspect its existing
Python/CUDA setup before installing a second stack.

## Checks

```bash
python -m unittest discover -s backend/ocr-worker -p 'test_*.py'
python -m unittest discover -s scripts -p 'test_*.py'
```

The focused tests do not claim real Oracle jobs or load model weights. Local
ignored environments, outputs, credentials and experiment folders may remain
on the development machine; the allowlist prevents packaging them.
