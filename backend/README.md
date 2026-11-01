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

## Run OCR on a Mac through SSH (Windows hosts the other services)

Windows keeps Angular, the .NET API/business workers, PostgreSQL and RabbitMQ.
The Mac runs one Python OCR worker by default in `backend/ocr-worker/.venv` using `mps`.
Each process loads its own model copy and consumes one job at a time from the
same `ocr-jobs` queue. RabbitMQ distributes jobs among available workers; no extra
tunnel or port is required. Three workers can process three different cards at
once, but share the Mac's GPU and memory and do not guarantee three times the throughput.
Original uploaded images remain on Windows. PostgreSQL stores job/image references
and OCR results, not image bytes. RabbitMQ carries job references and result JSON.
The Mac downloads front/back images from the existing API endpoints by job ID,
validates them, processes them and removes its temporary files after each job.

The provisioned Mac project is `/Users/entapp/ocr-service-demo-for-mac/ocr_demo`
on `entapp@192.168.171.19`. No Docker, .NET, Node, Homebrew, sudo or system service
installation is required on the Mac. The Python environment uses an existing base
interpreter at `/Users/entapp/the-eye-env/eye-env`; keep that base installation.
The service does not use `Id_OCR/Id_OCR_env` packages or `.data/python-deps`.

From a Windows terminal at the repository root, start the Windows components
without local OCR processes:

```cmd
start.cmd -OcrWorkers 0 -BusinessWorkers 2
```

The launcher does not stop older workers or business processes. If the Windows
components are already running, do not start another copy. Stop existing local
OCR consumers before switching to the Mac, preferably while the queue is idle.

Start/reuse the background SSH tunnel, start/reuse the Mac worker, and follow its log:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/connect-mac-worker.ps1
```

Override the Mac pool size with `-OcrWorkers 2`, `-OcrWorkers 3`, etc. (range 1–32; default 1):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/connect-mac-worker.ps1 -OcrWorkers 2
```

The Windows launcher still uses `start.cmd -OcrWorkers 0`; this parameter controls
the Mac pool only on `connect-mac-worker.ps1`.

The SSH password is requested for worker management/log viewing, but is not stored.
Closing log viewing does not stop either the background tunnel or Mac worker.
The tunnel maps Mac `127.0.0.1:15673` to Windows `127.0.0.1:5672` (RabbitMQ),
and Mac `127.0.0.1:15125` to Windows `127.0.0.1:5125` (API). PostgreSQL is not
forwarded. Both remote listeners bind only to loopback.

The provisioned forwarding key is Windows `.data/mac-ssh/tunnel_ed25519_user`,
with permissions limited to the Windows user, administrators and SYSTEM. Its public
key is recorded in Mac `~/.ssh/authorized_keys` with a forced failing command,
PTY/agent/X11 forwarding disabled, remote forwarding limited to the two ports,
and local forwarding limited to unused Mac loopback port 1. It cannot run a shell.
The key and tunnel PID/logs are ignored by Git. A new Windows checkout/machine
needs its own key provisioning; do not commit or share the private key.
To revoke this access, remove only the `ocr-mac-forwarding-only` entry from
`authorized_keys` and stop the tunnel.

To manage the worker from a normal Mac SSH shell:

```bash
cd ~/ocr-service-demo-for-mac/ocr_demo
bash backend/scripts/mac-worker.sh status
bash backend/scripts/mac-worker.sh stop
bash backend/scripts/mac-worker.sh start
bash backend/scripts/mac-worker.sh start 2
bash backend/scripts/mac-worker.sh logs
```

`start` defaults to 1 (or `OCR_WORKERS` when set in the Mac shell). It reuses
existing numbered workers, starts missing slots, and gracefully stops slots above
the requested count. Repeated starts do not add duplicate processes. Startup is
staggered; check each log for `OCR models ready` and `Connected, waiting for jobs`.
`status` counts live processes; readiness is established by their logs/broker consumers.
`stop` asks all managed workers to finish their current jobs and exit. Check `status`
before restarting. A lock prevents concurrent start/stop commands. A legacy
single worker must be stopped before starting the numbered pool.

Each worker uses `nohup`, validates its PID against the actual worker command,
and writes `.data/logs/mac-ocr-1.log` / `mac-ocr-1.pid`, then `-2`, `-3`, etc.
The `logs` command follows all existing numbered log files. Pools do not
automatically start at Mac reboot. Ultralytics automatic dependency
installation is disabled in this launcher; macOS requirements include `pi-heif`.

The current M4 has 16 GB unified memory. Multiple full model copies can create
memory pressure and swap activity. Watch memory/throughput under real load and
use `start 1` or `start 2` if three workers are slower or unstable. Increasing the
accepted count to 32 does not mean this hardware can support 32 OCR processes.

Validation on 2026-10-27: all three Mac workers completed a three-card batch
successfully, with 12 extracted fields per card. Processing took approximately
15, 85 and 89 seconds; the batch finished in 90 seconds. Swap use was around
14–16 GB. Earlier single-worker tests took 8–11 seconds per card, so this
three-worker trial did not demonstrate a throughput improvement on the 16 GB Mac.
The default is now one. A subsequent seven-card test took 58.3 seconds with one
worker and 3 minutes 17 seconds with three. Explicitly select more workers only
when measuring a benefit on the available hardware.

To restart just the Windows tunnel, or stop it after draining active jobs:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/start-mac-tunnel.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File backend/scripts/start-mac-tunnel.ps1 -Stop
```

Windows, Docker infrastructure, API, business workers and the tunnel must remain
running while OCR jobs are submitted. The worker retries broker connections; the
tunnel must be restarted after a Windows reboot or an SSH connection failure.
If image downloads fail transiently, each image gets three attempts with a
30-second socket timeout. Exhausted downloads finish as `TechnicalError` with
`IMAGE_DOWNLOAD_FAILED`; missing/invalid/oversized images finish as `OcrFailed`.
Downloads are limited to 20 MiB each, bypass system proxies and reject redirects.

Configuration for other remote setups: set `OCR_IMAGE_API_URL` to an HTTP(S) API
base URL, `RABBITMQ_HOST/PORT/USER/PASSWORD` as needed, and `OCR_DEVICE=mps`.
If `OCR_IMAGE_API_URL` is unset, direct worker invocation retains the original
local/shared-filesystem behavior. The Mac launcher sets the tunnel defaults.
On MPS, front/back detection is serialized to avoid the concurrent Metal shader
cache failure observed with PyTorch 2.13; CPU/CUDA detection stays parallel.

Run worker regression checks with either platform's environment:

```bash
backend/ocr-worker/.venv/bin/python -m unittest discover -s backend/ocr-worker -p 'test_*.py'
```

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

## Linux / H100 deployment and site-visit plan

This is a deployment plan, not a claim of a completed H100 installation or benchmark.
The commands assume Ubuntu 24.04, x86_64, CPython 3.12, a dedicated worker account
called `ocrworker`, and an application directory `/srv/ocr-service`. Confirm the
actual distribution, architecture, GPU allocation and network path before the visit.
On RHEL, ARM servers or scheduler-managed clusters, adapt installation/allocation
with the site's administrator instead of running Ubuntu/dedicated-server commands.

### Scope and responsibilities

Keep the Windows API, Angular, RabbitMQ, PostgreSQL and business workers. Install
only the Python OCR worker, Python packages and offline model assets on Linux.
Images are downloaded from the Windows API into temporary Linux files. Queue
messages contain references and results, and Linux does not connect to PostgreSQL.

| Item | Owner | Required? |
|---|---|---|
| Working NVIDIA data-center driver and assigned GPU access | Site administrator | Yes |
| Python 3.12 and venv support | Site administrator | Yes |
| SSH server, account/key access, VPN or bastion path | Site administrator/network team | Yes for SSH workflow |
| User-owned Python environment, packages, code and model assets | Application team | Yes |
| tmux for initial operation, or an approved persistent service/scheduler job | Application team/site administrator | Choose an operating method |
| Fabric Manager on applicable HGX/DGX NVSwitch platforms | Site administrator | Platform dependent |
| Full CUDA Toolkit / nvcc / compiler toolchain | Site administrator | Not needed for normal prebuilt PyTorch inference |
| Docker, NVIDIA Container Toolkit, .NET, Node.js, RabbitMQ or PostgreSQL on Linux | — | Not needed for this worker-only deployment |

### Before the visit: collect these facts

Ask the site administrator for the Linux distribution/version and architecture,
NVIDIA driver version, H100 count and available GPU memory, host RAM/disk quota,
and whether access is to whole GPUs, MIG slices, a VM or a Slurm allocation.
Confirm whether pip/OS-package downloads are allowed, the approved proxy or
internal mirror if relevant, and whether administrator assistance/reboots can
be scheduled if the driver/Python/SSH prerequisites are missing.

Confirm the SSH hostname/port/user, host-key fingerprint, VPN/bastion route, and
whether remote loopback forwarding on ports 15673 and 15125 is permitted. Physical
access to the server does not establish a remote route between company networks.
The forwarding endpoint must be the host running the GPU worker, not just a login
node. If forwarding is unavailable, arrange approved private connectivity from
the worker to the Windows API and RabbitMQ and configure their private addresses.
Do not expose the development API/broker publicly to solve connectivity.

For planning, reserve about 50 GiB of working disk quota initially for the code,
roughly 4–5 GiB of current model assets, a CUDA Python environment, offline wheels,
transfer archives, logs and temporary images. This is a planning allowance, not
a measured minimum; measure the completed bundle/environment before handover.

### Dependency explanation and CUDA selection

`backend/ocr-worker/requirements.real.txt` includes `requirements.txt` (pika),
`try1/requirements.production.txt` (the OCR stack), and a macOS-only extra.

| Package | Role |
|---|---|
| pika | RabbitMQ AMQP client |
| torch | Tensor operations, model execution and CUDA acceleration |
| torchvision | Vision operations used by the detector/vision stack |
| transformers | Qwen text OCR model, processor, tokenizer and generation |
| accelerate | Model loading/device placement support |
| ultralytics | YOLO field detectors |
| numpy | Array operations |
| opencv-python-headless | Image processing and digit-extraction operations |
| pillow | Image decoding, validation and manipulation |
| safetensors | Reading model weight files |
| qwen-vl-utils | Qwen vision utilities included in the pinned OCR stack |
| pi-heif (macOS only in our requirements) | HEIF/HEIC decoding support for Pillow |

`pi-heif==1.4.0; sys_platform == "darwin"` was pinned after Ultralytics requested
it during the Mac validation. The marker means pip skips that requirement on
Linux and Windows. It is not a GPU/RabbitMQ dependency and the current upload API
does not accept HEIF/HEIC. No Linux installation of this package is required by
our declared dependencies. pip also installs transitive dependencies; the table
is not an exhaustive list of wheels to carry for an offline installation.

Use the existing model/package versions first. A proposed initial Linux build is
torch 2.13.0 + torchvision 0.28.0 from PyTorch's CUDA 12.6 (`cu126`) index.
Have the administrator approve a supported production driver compatible with
that runtime; a conservative baseline is driver 560.28.03 or newer. Older CUDA
12.x drivers may work under minor-version compatibility with limitations, so do
not replace a working shared-cluster driver blindly. If site constraints require
another runtime, select the matching official wheel pair and validate it first.

The GPU driver is a host prerequisite. Prebuilt PyTorch wheels bring CUDA runtime
dependencies into the Python environment; normal inference does not need a full
system CUDA Toolkit, nvcc, custom FlashAttention build or a separate cuDNN install.
The CUDA version displayed by `nvidia-smi` describes driver capability and need
not exactly equal `torch.version.cuda`.

### Prepare the transfer before travelling

Create a clean archive of the current working source and required assets. Do not
copy Windows/Mac virtual environments or installed CUDA/CPU packages. Preserve
the relative `backend/ocr-worker`, `backend/scripts` and `try1` paths. The following
Windows Command Prompt command includes local uncommitted files and ignored model
weights, unlike a bare clone or `git archive HEAD`:

```cmd
tar -cf "%TEMP%\ocr-h100-worker.tar" --exclude=.git --exclude=".venv*" --exclude=__pycache__ --exclude="*.pyc" --exclude=outputs --exclude=wheels_cuda --exclude=dataset --exclude=images --exclude=.env --exclude=".env.*" -C "D:\Ahmed Harhash\OCR-Demo\OCR-Service-Demo" backend/ocr-worker backend/scripts backend/README.md try1
certutil -hashfile "%TEMP%\ocr-h100-worker.tar" SHA256
```

Verify the archive includes the actual files listed in `try1/ASSETS.md`:

- `core/detector/weights/best.pt`
- `core/detector/id-back detect/runs_id_back/yolo26_id_back_v2/weights/best.pt`
- `core/digits/files/arabic_digit_ai/models/arabic_digit_cnn.pt`
- `core/ocr_old/bundle/` (including Qari weights, configs, processor and tokenizer)
- `core/fieldmatch/models/{gender,religion,marital}/best.pt`

Bring a recorded archive checksum, the working source revision/change snapshot,
a small approved front/back test pair and its expected extraction, and this guide.
Private assets are transferred separately from Git tracking; do not assume that a
clone or Git LFS will obtain them. Leave Windows uploads/databases, `.data`, SSH
private keys and unrelated environments out of the transfer.

If installation will be offline, prepare a wheelhouse on a connected Linux
machine/VM with the SAME OS compatibility, CPU architecture and Python version
as the target. This preparation machine does not need a GPU to download/install
the CUDA wheels. In a temporary Linux staging copy of this bundle:

```bash
python3.12 -m venv .package-env
.package-env/bin/python -m pip install --upgrade pip
.package-env/bin/python -m pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu126
.package-env/bin/python -m pip install -r backend/ocr-worker/requirements.real.txt
.package-env/bin/python -m pip check
.package-env/bin/python -m pip freeze > requirements.linux-cu126.lock.txt
.package-env/bin/python -m pip download --only-binary=:all: --dest wheelhouse -r requirements.linux-cu126.lock.txt --extra-index-url https://download.pytorch.org/whl/cu126
```

Validate the wheelhouse by installing the lock into another fresh matching Linux
venv with `--no-index --find-links wheelhouse`, then run `pip check`. This tests
package completeness, not H100 hardware execution. Carry the lock and all wheels,
including NVIDIA runtime dependencies. Windows/macOS wheels are not substitutes.
Have the administrator separately stage any required offline OS packages, Python
packages at the OS level, and approved driver packages/kernel prerequisites.

### At the site: inspect and install host prerequisites

Record:

```bash
cat /etc/os-release
uname -m
nvidia-smi
nvidia-smi -L
free -h
df -h
python3.12 --version
```

If `nvidia-smi` fails, resolve the driver/GPU allocation with the administrator
before Python installation. On HGX/DGX NVSwitch systems they must also verify the
platform's Fabric Manager/fabric health. Do not change MIG layout, driver or other
tenants' GPU processes yourself.

Example host packages for Ubuntu 24.04 only, installed by the administrator:

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv ca-certificates openssh-server tmux rsync lsof curl libgl1 libglib2.0-0t64
sudo systemctl enable --now ssh
```

The OpenCV-related shared libraries also cover cases where Ultralytics resolves
the regular `opencv-python` package alongside our headless dependency. Git is
optional when deploying an archive. A compiler is only a fallback if no compatible
binary wheel exists; identify that before the visit rather than planning to build
the entire CUDA/PyTorch stack on site.

Have the administrator create the worker account and writable application
directory, for example `/srv/ocr-service` owned by `ocrworker`. Perform the
remaining application commands as that account. Compare the transferred archive's
`sha256sum` with the recorded Windows checksum and extract:

```bash
cd /srv/ocr-service
tar -xf /path/to/ocr-h100-worker.tar
python3.12 -m venv backend/ocr-worker/.venv
```

Connected installation:

```bash
backend/ocr-worker/.venv/bin/python -m pip install --upgrade pip
backend/ocr-worker/.venv/bin/python -m pip install torch==2.13.0 torchvision==0.28.0 --index-url https://download.pytorch.org/whl/cu126
backend/ocr-worker/.venv/bin/python -m pip install -r backend/ocr-worker/requirements.real.txt
backend/ocr-worker/.venv/bin/python -m pip check
```

Alternatively, offline installation using the validated lock/wheelhouse:

```bash
backend/ocr-worker/.venv/bin/python -m pip install --no-index --find-links /path/to/wheelhouse -r /path/to/requirements.linux-cu126.lock.txt
backend/ocr-worker/.venv/bin/python -m pip check
```

Installing requirements after the CUDA pair should retain its matching versions.
Always verify the result; a successful pip installation alone does not prove CUDA
is enabled. Save the final `pip freeze` and host/GPU details with the deployment.

### Verify GPU execution and the actual models

Use only an allocated GPU. For a dedicated server the administrator can specify
`CUDA_VISIBLE_DEVICES=0` or an assigned GPU UUID. Under Slurm/MIG, preserve the
scheduler/admin assignment instead of overriding it to access another device.
Run this in that allocation:

```bash
backend/ocr-worker/.venv/bin/python - <<'PY'
import torch, torchvision
from torchvision.ops import nms
print('Torch:', torch.__version__, 'Torchvision:', torchvision.__version__)
print('Wheel CUDA runtime:', torch.version.cuda)
assert torch.cuda.is_available(), 'CUDA unavailable'
print('GPU:', torch.cuda.get_device_name(0))
print('Capability:', torch.cuda.get_device_capability(0))
x = torch.randn(256, 256, device='cuda')
y = x @ x
boxes = torch.tensor([[0., 0., 10., 10.]], device='cuda')
scores = torch.tensor([0.9], device='cuda')
assert nms(boxes, scores, 0.5).numel() == 1
torch.cuda.synchronize()
print('CUDA compute and torchvision NMS passed')
PY

PYTHONPATH="$PWD/backend/ocr-worker" OCR_DEVICE=cuda \
backend/ocr-worker/.venv/bin/python -c "from inference import initialize; initialize(); print('OCR models loaded on CUDA')"

backend/ocr-worker/.venv/bin/python -m unittest discover -s backend/ocr-worker -p 'test_*.py'
```

Model initialization exits after testing; the persistent worker loads once per
process and reuses its models. CUDA mode uses the active merged Qari model in
FP16; do not switch to the archived V4 model or copy its obsolete Windows paths.

### Establish the real network route and run one worker

On Windows, start Docker Desktop and the existing application, once:

```cmd
cd /d "D:\Ahmed Harhash\OCR-Demo\OCR-Service-Demo"
start.cmd -OcrWorkers 0 -BusinessWorkers 2
```

Do not rerun the application launcher if its Windows processes already exist.
Stop/drain other OCR consumers (including the Mac) during the H100 acceptance
test, so the test proves that the H100 processed the jobs.

For the initial SSH test, replace `USER` and `GPU_HOST` and run on Windows:

```cmd
ssh -N -T -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -R 127.0.0.1:15673:127.0.0.1:5672 -R 127.0.0.1:15125:127.0.0.1:5125 USER@GPU_HOST
```

Verify the new host fingerprint with the administrator before trusting it. Add
`-J USER@BASTION` if that is the approved path. Keep this foreground tunnel terminal
open for the initial test. Its remote listener ports exist on Linux, not on the
Mac, so the same numbers can be used on the separate host. Workers on one Linux
host share the tunnel. Do not copy/reuse the Mac's authorized-key entry or assume
its private-key/PID configuration provisions the new server.

In a second SSH session on the Linux GPU host, as the worker account:

```bash
cd /srv/ocr-service
curl --noproxy '*' --fail http://127.0.0.1:15125/health
export OCR_DEVICE=cuda
export OCR_IMAGE_API_URL=http://127.0.0.1:15125
export RABBITMQ_HOST=127.0.0.1
export RABBITMQ_PORT=15673
export RABBITMQ_USER=ocr
read -rsp 'RabbitMQ password: ' RABBITMQ_PASSWORD
export RABBITMQ_PASSWORD
printf '\n'
export YOLO_AUTOINSTALL=false
backend/ocr-worker/.venv/bin/python -u backend/ocr-worker/worker.py
```

Use the broker account/password actually configured on Windows; the account must
have permission to declare/consume/publish the application's queues and DLQs.
Choose the assigned GPU before launching if the host has multiple GPUs. For
example, prepend `CUDA_VISIBLE_DEVICES=0` to the worker command only when GPU 0
is explicitly allocated. `OCR_DEVICE=cuda` selects logical CUDA device 0 among
visible devices; it does not automatically spread processes over every H100.

Wait for `OCR models ready` with device `cuda` and imageSource `api`, followed
by `Connected, waiting for jobs`. Submit the approved card through the Windows
frontend and verify the Linux worker ID, extracted fields and final result in
PostgreSQL. If the broker connection fails, inspect worker logs; API health alone
does not test RabbitMQ authentication or queue permissions.

### Persistence, multiple GPUs and handover

For a supervised first run, use tmux on Linux: start a session with
`tmux new -s ocr-gpu0`, configure the environment and launch the worker inside it;
detach with Ctrl+B then D, and reconnect with `tmux attach -t ocr-gpu0`.
This survives an SSH shell disconnect, but not a server reboot and not the loss
of the Windows forwarding connection. The worker retries broker connections;
image-download failures after bounded retries become terminal technical errors.

On a dedicated server, arrange an administrator-managed systemd service, or use
the site's approved process manager. For scheduler-managed GPUs use a scheduled
GPU job, not a background process on the login node. A systemd example template
to review with the administrator is:

```ini
# /etc/systemd/system/ocr-worker@.service
[Unit]
Description=OCR GPU worker %i
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=ocrworker
WorkingDirectory=/srv/ocr-service
EnvironmentFile=/etc/ocr-service/worker-%i.env
ExecStart=/srv/ocr-service/backend/ocr-worker/.venv/bin/python -u /srv/ocr-service/backend/ocr-worker/worker.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=300
UMask=0077

[Install]
WantedBy=multi-user.target
```

Create a protected per-instance environment file (root-owned, worker-group-readable,
mode 0640) with the following values and actual broker credentials:

```text
# /etc/ocr-service/worker-0.env
CUDA_VISIBLE_DEVICES=0
OCR_DEVICE=cuda
OCR_IMAGE_API_URL=http://127.0.0.1:15125
RABBITMQ_HOST=127.0.0.1
RABBITMQ_PORT=15673
RABBITMQ_USER=ocr
RABBITMQ_PASSWORD=REPLACE_WITH_CONFIGURED_PASSWORD
YOLO_AUTOINSTALL=false
PYTHONUNBUFFERED=1
```

Only enable a service for a GPU assigned to this account. The administrator can
then run `systemctl daemon-reload` and `systemctl enable --now ocr-worker@0`.
Inspect `systemctl status ocr-worker@0` and `journalctl -u ocr-worker@0 -f`;
stop with `systemctl stop ocr-worker@0`. Configure journal retention and keep
the 300-second graceful-stop allowance under review for the measured workload.

Start with one worker on one allocated H100. For independent allocated GPUs,
create another instance environment file with its assigned GPU UUID/index and
start another service instance. No distributed training, model sharding or new
RabbitMQ queue is needed. Do not assume that multiple workers on one H100 are
faster; measure host RAM, GPU memory/utilization, latency, throughput and accuracy.
Do not use the Mac launcher unmodified: its defaults select MPS, its pool places
all processes on the same visible GPU, and its key/state configuration belongs
to the Mac. A Linux-specific convenience launcher can be prepared once host,
account, GPU allocation and network policy are known.

Before leaving the site, complete all of these acceptance checks:

1. CUDA tensor/NMS test and actual model initialization pass on the assigned H100.
2. A real card and representative batch finish through RabbitMQ with Linux worker
   IDs, expected fields and PostgreSQL persistence; test corrupt-image handling too.
3. Benchmark the same approved seven-card set, excluding model-loading warm-up,
   and compare text/IDs/dates as well as time. H100 timing is not yet measured.
4. Disconnect/reconnect the management shell and verify the worker persists.
5. Test tunnel reconnection and graceful worker stop/start; simulate a reboot only
   with the site administrator and confirm the chosen startup mechanism works.
6. Verify SSH access through the ACTUAL later VPN/bastion route, not only on-site LAN.
7. Record package lock, model/archive checksums, driver/runtime versions, allocated
   devices, service/config paths, log commands, host fingerprint and recovery steps.
8. Agree who keeps Windows services/tunnel available. If unattended operation is
   required, provision and test dedicated Linux-host SSH credentials and a supervised
   tunnel or approved private network path; the Mac background launcher is not that setup.

Do not leave the remote network route, driver install, Linux package availability
or restart behavior as tasks to discover after returning from the visit.

Primary references for the installation choices:

- [PyTorch official version / CUDA wheel commands](https://pytorch.org/get-started/previous-versions/)
- [NVIDIA driver installation guide](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/latest/index.html)
- [CUDA 12.6 driver versions and compatibility](https://docs.nvidia.com/cuda/archive/12.6.0/cuda-toolkit-release-notes/index.html)
- [PyTorch explanation of binary CUDA runtime dependencies](https://discuss.pytorch.org/t/should-i-install-the-extra-cudatoolkit-and-cudnn/194528/2)
- [NVIDIA Fabric Manager for applicable HGX/DGX platforms](https://docs.nvidia.com/hgx-platforms/fabric-manager-user-guide/index.html)
- [pi-heif project](https://pypi.org/project/pi-heif/)
