"""Verify classifier text acceptance at 80%, without model checkpoints."""
import inspect
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'try1'))
import torch
from core.fieldmatch import classifier
from pipeline import document


class ClassifierThresholdTests(unittest.TestCase):
    def test_pipeline_and_reader_defaults_are_eighty_percent(self):
        for function, parameter in [(classifier.PrintedFieldReader.predict, 'threshold'),
                                    (document.apply_back_classifiers, 'threshold'),
                                    (document.run_document, 'classifier_threshold')]:
            self.assertEqual(inspect.signature(function).parameters[parameter].default, .8)

    def test_example_candidates_now_produce_text(self):
        for score in [.887433, .822398, .984909, .8, .799]:
            reader = classifier.PrintedFieldReader.__new__(classifier.PrintedFieldReader)
            reader.field = 'test'
            reader.classes = ['accepted-label', 'other-label']
            reader.model = lambda _, p=score: torch.tensor([[p, 1-p]]).log()
            with patch.object(classifier, 'encode_image', return_value=torch.zeros(1)):
                result = reader.predict('unused')
            self.assertEqual(result['text'], 'accepted-label' if score >= .8 else None)
            self.assertEqual(result['status'], 'extracted' if score >= .8 else 'needs_review')


if __name__ == '__main__':
    unittest.main()
