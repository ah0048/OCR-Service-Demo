# Active old Arabic OCR model

Runtime: `reader.py`. Deployed weights and tokenizer/processor files:
`bundle/models/Qari-OCR-merged`.

The application is fixed to the old merged Qwen2-VL/Qari 2B model for routine
inference, independent of a stale `QARI_ENGINE` environment setting. V4 and
experimental models are archived outside the active project. Compatibility
helpers for old diagnostics may still reference the archive explicitly.

The reader loads locally only, caches weights, uses greedy generation, and
returns text with timing/review metadata. CPU defaults: BF16, 4 threads,
128 output-token limit, 200704 maximum input pixels.

Optional environment settings before launch: `QARI_THREADS`,
`QARI_MAX_PIXELS`, `QARI_MAX_TOKENS`. Lower resolution may lose small text.
Model output can contain added diacritics or word/number errors.

Use the pipeline/API to run the deployed OCR configuration.
