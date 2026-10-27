# Pipeline

Implementation: `front_id.py`. Run from project root:

```text
python -m pipeline.front_id "C:\path\front.jpg" --qari --device cpu --qari-device cpu
```

Stages:

1. Apply EXIF orientation and detect front-ID fields with YOLO.
2. Save clean, numbered crops for ID, name and address.
3. Read the ID with the existing strict 14-digit CNN extractor.
4. If `--qari` is set, read name/address using the old merged Qari model.
5. Save annotated image and UTF-8 `result.json` to `outputs/pipeline/`.

ID recognition never pads/truncates to force 14 digits. Missing fields, OCR
failures and uncertain readings have explicit statuses. Multiple text crops
are tried in confidence order if the first is unreadable. Review all text.

Options include `--output`, `--model`, `--digit-extractor`, `--conf`, `--imgsz`,
`--padding`, `--device`, `--qari-device`, and `--qari-model-dir`. Default paths
are maintained centrally in `project_config.py`. Use the old model for routine
work; archived V4 files are not selected automatically.

# Arabic OCR text

`arabic_text.py` removes tashkeel from all text OCR results, including name,
address, job and husband name (`hasband`). Both the selected field text and
nested OCR text are cleaned before returning or saving results. Arabic letters
(including hamza), numbers, punctuation and spacing are preserved.

