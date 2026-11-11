# Offline model assets

The Oracle worker loads models from local files and does not download weights
at startup. The transfer packager includes these assets at their existing paths:

- `core/detector/weights/best.pt`: front detector.
- `core/detector/id-back detect/runs_id_back/yolo26_id_back_v2/weights/best.pt`: back detector.
- `core/digits/files/arabic_digit_ai/models/arabic_digit_cnn.pt`: shared digit CNN.
- `core/fieldmatch/models/gender/best.pt`: gender classifier.
- `core/fieldmatch/models/religion/best.pt`: religion classifier.
- `core/fieldmatch/models/marital/best.pt`: marital classifier.
- `core/ocr_old/bundle/models/Qari-OCR-merged/`: merged Qari weights, tokenizer,
  processor, configuration and accompanying model documentation.

Paths above are relative to `try1/`. Model binaries are ignored by Git.
The packager checks for missing/empty weights and Git LFS pointer placeholders.
Keep the relative paths intact when extracting the archive.
