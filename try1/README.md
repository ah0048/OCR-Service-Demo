# Egyptian ID extraction API

The production application uses the old merged Qari 2B OCR for name/address,
YOLO for field detection, and the strict 14-digit CNN for the ID number.

## Production Docker

Required private model files must exist at the paths listed in [ASSETS.md](ASSETS.md).
They are copied into the image during the build; the running container never
downloads packages or models. Hugging Face and Transformers offline modes are
enforced in the image.

```text
docker compose build
docker compose up -d
```

Send both card images as multipart fields `front` and `back`:

```text
curl -F "front=@front.jpg" -F "back=@back.jpg" \
  "http://localhost:8000/predict?qari=true"
```

The combined result contains front `id`, `name`, and `address`, plus back
`expiredate`, `gender`, `hasband`, `id`, `job`, `marital`, and `religion`.
The national ID is extracted independently from both sides and the remaining
fields run only after both readings agree. The legacy one-file `image` upload
continues to process a front image for older clients.

To process both sides directly without running the HTTP server:

```text
python -m pipeline.document --front front.jpg --back back.jpg --device cpu
```

Inside the production image:

```text
docker run --rm --entrypoint python \
  -v /path/to/images:/input:ro -v /path/to/results:/app/outputs \
  front-id-api:production -m pipeline.document \
  --front /input/front.jpg --back /input/back.jpg --output /app/outputs --device cpu
```

The image runs on both `linux/amd64` and `linux/arm64`. On Docker Desktop for
macOS it uses the CPU because Docker does not expose Apple's Metal/MPS GPU to
Linux containers. A built image needs network access only for clients calling
port 8000; it does not need outbound internet access.

For an NVIDIA Linux host with the NVIDIA Container Toolkit:

```text
docker compose -f compose.yaml -f compose.cuda.yaml up -d --build
```

## Native Apple GPU

To use a Mac GPU, run natively on macOS rather than inside Docker:

```text
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.production.txt
python -m api.server --device mps --port 8000
```

After installing the packages once and keeping the local model directories,
the native server also runs without internet access.

## CUDA / RTX 3070

See [CUDA_README.md](CUDA_README.md) for offline setup and memory settings.
Run `api\start_cuda.cmd` in your CUDA Python environment. CPU mode remains the default.

## Start the API

From PowerShell or CMD:

```text
cd C:\Users\Anas.Magdy\Desktop\AI\try1
python -m api.server --port 8000 --fast
```

Open http://127.0.0.1:8000. Only port **8000** is needed. Port 8001's V4
server has been stopped. `--fast` lowers image resolution and can hurt small
text accuracy; omit it for the standard resolution.

## Run one image

```text
python -m pipeline.front_id "C:\path\front.jpg" --qari --device cpu --qari-device cpu
```

Run all commands from this project root. Root compatibility launchers
`pipeline_api.py`, `front_id_pipeline.py`, and `qari_local.py` keep previous
Python import/start commands working.

## Layout

| Folder | Purpose |
|---|---|
| `api/` | HTTP server and upload page |
| `pipeline/` | Detect → crop → digit OCR → Arabic OCR orchestration |
| `core/detector/` | Deployment YOLO weights |
| `core/digits/` | Production digit extractor and CNN |
| `core/ocr_old/` | Active old OCR reader and local model bundle |
| `core/fieldmatch/` | Gender, religion and marital classifiers |
| `outputs/` | Created at runtime for results and crops; excluded from publishing |

Each application folder has a README. `project_config.py` is the canonical
path registry. No package installation or internet access is required.

OCR can misread words and numbers. Review recognized text; a successful HTTP
response is not a guarantee of transcription accuracy. API requests run one
at a time; cached models remain loaded until the process exits.

## Deployment contents

Training utilities, labeled samples, experimental checkpoints and previous outputs
are archived outside this project under `D:\ansAI`. Publish this `cuda-publish/`
folder. Only deployed model weights are required; training data is not required.
