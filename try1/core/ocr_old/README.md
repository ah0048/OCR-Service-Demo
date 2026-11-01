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

### Compare batching on local test cards

From the repository root on the Mac, using local front/back images:

```bash
backend/ocr-worker/.venv/bin/python backend/ocr-worker/benchmark_batch.py \
  --device mps --batch-size 2 --repeats 3 \
  --pair "/path/to/card1-front.jpg" "/path/to/card1-back.jpg" \
  --pair "/path/to/card2-front.jpg" "/path/to/card2-back.jpg" \
  --output batch-comparison.json
```

Use `--device cuda` on the GPU server. Use representative cards whose IDs pass
the front/back agreement gate, so Qari actually runs. Stop competing OCR worker
processes and wait for them to finish before benchmarking; this tool does not
stop them or change the running service. It reads local files directly and does
not publish RabbitMQ messages, download job images, or write to PostgreSQL.

Each batch-size variant starts in a fresh subprocess, loads one model set,
warms up on the first card, then times the requested cards and repetitions.
The variants run sequentially. The summary reports elapsed time, throughput,
process peak RSS, CUDA peak allocated tensor memory when applicable, batch
fallbacks, actual batched crop results, and field/status/outcome/confidence
differences against batch size 1. Extracted text is omitted from the summary;
temporary comparison fingerprints and pipeline outputs are removed on normal
exit. Existing summary files are not overwritten.

Process peak RSS includes startup/warmup and does not capture total GPU or Mac
unified-memory consumption. No output differences is a consistency check,
not proof of accuracy; compare with labeled cards too. If `batchedCrops` is zero
or `batchFallbacks` is nonzero, inspect that before claiming a batching benefit.
The baseline always runs first; repeat the experiment to check load/temperature
effects. Model loading is excluded, but measured inference includes validation,
temporary-file work, detection, classifiers, and Qari. This is not an end-to-end
RabbitMQ/HTTP latency test.

Use the pipeline/API to run the deployed OCR configuration.
