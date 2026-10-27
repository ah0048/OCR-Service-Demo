# API

Double-click `start.cmd` to start the Docker image in CPU mode using
`compose.yaml`. Docker Desktop must be running. The launcher reuses the existing
image, builds it if missing, and shows container logs. After source changes, run
`api\start.cmd --build` to rebuild. Closing the log window leaves the container
running; use `docker compose -f compose.yaml stop` from the project root to stop it.
Double-click `start_docker_cuda.cmd` on an NVIDIA GPU machine to start Docker
with `compose.cuda.yaml`. Use `api\start_docker_cuda.cmd --build` after code
changes. This requires working NVIDIA drivers and Docker GPU support.
`start_cuda.cmd` remains the native Python CUDA launcher.

Implementation: `server.py`. From the project root:

```text
python -m api.server --port 8000 --fast
```

The API binds to localhost. Open http://127.0.0.1:8000 for the upload page.
`GET /health` reports the selected model, loaded state and whether inference
is busy. The active default is always the old `qwen2_vl` merged model; an old
`QARI_ENGINE=v4` environment variable no longer switches the application.

```text
curl.exe --max-time 600 -X POST "http://127.0.0.1:8000/predict" -F "image=@C:\path\front_id.jpg"
```

`POST /predict` accepts one multipart `image` field or raw image bytes with
an `image/*` content type. Add `?qari=false` for numeric ID extraction only.
HTTP 200 returns `fields`, `result`, `result_file`, `request_id`, and a
`complete`/`partial` status. Field OCR failures remain explicit.

Uploads: <=20 MB, <=25 million pixels, single-frame JPEG/PNG/WEBP/BMP/TIFF.
One inference at a time; a concurrent request receives HTTP 503. Invalid
images return 400/415; oversized requests return 413. Qari and detector weights
are cached. CPU OCR may take minutes. This is a local development server,
not a public authenticated service.

Files are retained under `outputs/api/`. `--output` and `--port` override
defaults. `--fast` uses fewer input pixels; accuracy can decrease. Restart
the server after code/settings changes. Stop with Ctrl+C.


