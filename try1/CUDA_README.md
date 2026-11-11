# Running the Oracle worker on NVIDIA CUDA

The models and Oracle integration are the same as the tested CPU worker.
Set `OCR_DEVICE=cuda` in the project-root .env on the NVIDIA host. The detector
and Qari use CUDA; the digit and printed-word classifier models stay on CPU.
Start with one process per selected GPU: each worker process owns its model copy.

Use a native Linux Python environment. Verify the installed driver with
`nvidia-smi`, and select compatible CUDA-enabled torch/torchvision builds using
the official installer: https://pytorch.org/get-started/locally/

The requirements pin the tested torch/torchvision versions. Check that installing
them has not selected a CPU-only build. If the target requires different package
versions, validate them in a separate environment before changing the pins.

```bash
.venv/bin/python -c "import torch; print(torch.__version__, torch.version.cuda); assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0))"
.venv/bin/python -m pip check
.venv/bin/python backend/ocr-worker/oracle_worker.py --check-connection
.venv/bin/python backend/ocr-worker/oracle_worker.py --workers 1
```

On a host with several GPUs, select one with CUDA_VISIBLE_DEVICES per launch.
The --workers option does not assign processes to different GPUs automatically.
Do not install a full CUDA toolkit solely for inference unless the chosen
PyTorch build or other host requirements call for it.

Oracle 11.2 needs Thick mode with compatible Oracle Client 19c. Configure
ldconfig or LD_LIBRARY_PATH before starting Python, and install the client's
OS libraries as required by that Linux distribution. Leave ORACLE_CLIENT_LIB_DIR
empty on Linux. See database/oracle/RUN_WORKER.md for connection setup.

For an offline target, transfer matching Linux/Python/CUDA wheels separately.
The source/model archive does not bundle Python environments, NVIDIA drivers or
Oracle Client libraries.
