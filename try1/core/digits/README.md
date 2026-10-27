# ID digit extraction

- `files/digit_extractor/extract_digits.py`: extraction API and preprocessing.
- `files/digit_extractor/strict_id.py`: strict 14-digit CNN recognition.
- `files/arabic_digit_ai/model.py`: runtime CNN architecture.
- `files/arabic_digit_ai/models/arabic_digit_cnn.pt`: deployed weights.

ID and date recognition use the CNN and require no training images or templates.
Legacy template helpers are not part of the production workflow.
