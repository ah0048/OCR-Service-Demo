# Offline production assets

The Docker image contains all model assets and never downloads them at runtime.
Before building on a new machine, copy these folders from the approved offline
model transfer and preserve their relative paths:

- core/detector/weights/
- core/detector/id-back detect/runs_id_back/yolo26_id_back_v2/weights/
- core/digits/files/arabic_digit_ai/models/ (only arabic_digit_cnn.pt)
- core/ocr_old/bundle/ (old Qari merged weights, tokenizer and processor)
- core/fieldmatch/models/ (gender, religion and marital classifiers)

The production startup validator checks these files before loading the models.
Do not run `git lfs pull`; these private assets are distributed separately.
