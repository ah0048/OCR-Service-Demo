# Printed-field classifiers

`classifier.py` loads the deployed classifiers used by `pipeline/document.py`.
`options.py` defines the supported field labels.

| Model path | Field |
| --- | --- |
| `models/gender/best.pt` | Gender |
| `models/religion/best.pt` | Religion |
| `models/marital/best.pt` | Marital status |

Paths are configured centrally in `project_config.py` through
`FIELD_CLASSIFIER_DIR` and `FIELD_CLASSIFIER_MODELS`. These runtime assets belong
under `core/fieldmatch/models/`. Input images are supplied by the caller.
The Docker build includes this component and its models when copying `core/`.
