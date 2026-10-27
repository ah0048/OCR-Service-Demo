# RTX 3070 (8 GB) version

The old merged Qari 2B remains the OCR model. CUDA mode places YOLO and Qari
on the GPU; the small ID digit recognizer stays on CPU. Qari uses FP16 on CUDA,
SDPA attention, at most 200704 input pixels by default, and 128 generated tokens.
The API processes one image at a time and caches the models.

## Offline prerequisites

Use Windows x64 / Python 3.12 and an NVIDIA driver compatible with your chosen
CUDA PyTorch build. The existing machine has torch 2.13.0+cpu: copying that
installation will not enable CUDA. Do not assume the bundled wheels include CUDA.

On an approved connected machine, obtain a matching CUDA-enabled torch and
torchvision pair plus their dependencies for Windows/Python 3.12 from the
official PyTorch distribution. Transfer the approved wheel set to `wheels_cuda/`.
Keep the existing CPU environment; create a separate environment for GPU work.
The wheel set must also include the application's transformers, accelerate,
qwen_vl_utils, Pillow, ultralytics, opencv-python and their dependencies. Keep the
working application's versions where compatible. This repository does not
download packages automatically.

```bat
py -3.12 -m venv .venv-cuda
.venv-cuda\Scripts\activate
python -m pip install --no-index --find-links wheels_cuda torch torchvision transformers accelerate qwen_vl_utils Pillow ultralytics opencv-python
python -m pip check
python -c "import torch; print(torch.__version__, torch.version.cuda); assert torch.cuda.is_available(), 'CUDA unavailable'; print(torch.cuda.get_device_name(0))"
```

Restore model assets with `git lfs pull` from the internal repository, or copy
the original model folders using approved offline transfer. A small text file
beginning with `version https://git-lfs...` is a pointer, not usable weights.

## Run

From the project root with the CUDA environment activated:

```bat
api\start_cuda.cmd
```

Equivalent: `python -m api.server --device cuda --port 8000`.
Open http://127.0.0.1:8000; `/health` reports `device: cuda` and OCR load status.
Stop any CPU API using port 8000 first, or pass `--port 8002`.

Single image:

```bat
python -m pipeline.front_id "C:\path\front.jpg" --qari --device cuda --qari-device cuda
```

CPU remains available with `python -m api.server --device cpu --fast`.
`--device auto` selects CUDA when available; explicit CUDA fails if unavailable.

## Memory and speed

Keep one API process on the GPU. Close other GPU model processes. If memory
is insufficient, restart with `--fast` (100352 pixels); this can reduce small-text
accuracy. Do not raise QARI_MAX_PIXELS/QARI_MAX_TOKENS until testing memory and
accuracy on real crops. Keep the server running to avoid reloading weights.
No quantization package or FlashAttention installation is required.

GPU latency and peak memory must be measured on the RTX machine. This update
was developed on CPU-only PyTorch; it does not claim a completed GPU benchmark.

Measure full HTTP response latency after starting the API:
the API with a representative input image. Benchmark utilities are archived separately.
The first request is excluded as warm-up. Check p95 and incomplete requests,
then repeat with representative images and manually verify OCR accuracy.
Each request saves normal pipeline output. A sub-second result on one image
does not guarantee a sub-second service under concurrent load.
