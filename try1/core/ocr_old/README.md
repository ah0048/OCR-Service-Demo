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

Experimental: `QARI_BATCH_SIZE=2` processes up to two crops from one document
in one generation call, sharing the loaded merged model. The default `1` keeps
individual generation calls. Restart the worker after changing the setting;
an already-running process retains its configuration. This does not change the
number of worker processes or RabbitMQ prefetch, and does not batch across jobs.
Larger batches need more working memory and must be benchmarked for speed and
extracted-text accuracy on the target hardware before enabling routinely.
Batch failures log a warning and retry those crops individually; one invalid
crop does not discard the other crops. V4 diagnostic calls remain sequential.
Successful batched results include `batch_size`; their `seconds` value is the
whole chunk's elapsed time, repeated per crop, so do not sum it across crops.

Use the pipeline/API to run the deployed OCR configuration.
