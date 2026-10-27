# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false \
    REQUIRE_MODEL_WARMUP=1 \
    QARI_THREADS=4 \
    QARI_MAX_PIXELS=200704

WORKDIR /app

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY requirements.production.txt ./
RUN python -m pip install --no-cache-dir --requirement requirements.production.txt
RUN python -m pip uninstall --yes opencv-python \
    && python -m pip install --no-cache-dir --force-reinstall --no-deps \
       opencv-python-headless==4.14.0.94 \
    && python -c "import cv2; print('Headless OpenCV', cv2.__version__)"

COPY --chown=app:app api ./api
COPY --chown=app:app core ./core
COPY --chown=app:app pipeline ./pipeline
COPY --chown=app:app project_config.py docker-entrypoint.py ./
RUN mkdir -p /app/outputs/api && chown -R app:app /app/outputs

USER app
EXPOSE 8000
VOLUME ["/app/outputs"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
  CMD python -c "import json,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4); raise SystemExit(0 if json.load(r).get('status') == 'ok' else 1)"

ENTRYPOINT ["/bin/sh", "-c", "python docker-entrypoint.py && exec python -m api.server --host 0.0.0.0 --port 8000 --device ${DEVICE:-cpu}"]
