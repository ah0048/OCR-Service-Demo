"""Exercise the document gate and downstream stages without model inference."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'try1'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pipeline import document
from inference import overall_confidence


class DocumentIdGateTests(unittest.TestCase):
    def run_pipeline(self, status, job_id=None):
        def detect(source, output, **kwargs):
            output.mkdir(parents=True)
            names = ('id', 'name', 'address') if source == 'front' else document.BACK_FIELDS
            fields = {name: {'status': 'pending', 'text': None,
                             'candidates': [{'confidence': .9}]} for name in names}
            fields['id'].update(status='extracted', text='30806191402048')
            return output / 'result.json', {'fields': fields}

        def reconcile(report):
            report['national_id'] = {'status': status, 'text': '30806191402048'}

        def text(side_results, device):
            for _, side in side_results.values():
                for name, entry in side['fields'].items():
                    if name not in ('id', 'expiredate', 'gender', 'marital', 'religion'):
                        entry.update(status='extracted', text='read text')

        def expiry(fields):
            fields['expiredate'].update(status='extracted', text='01/01/2030')

        def classify(fields, models, threshold):
            for name in document.CONSTRAINED_BACK:
                fields[name].update(status='extracted', text='label')
            return {name: {'status': 'extracted'} for name in document.CONSTRAINED_BACK}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(document, 'run_pipeline', side_effect=detect), \
             patch.object(document, 'reconcile_id_sides', side_effect=reconcile), \
             patch.object(document, 'run_text_ocr', side_effect=text) as text_mock, \
             patch.object(document, 'apply_expiry_date', side_effect=expiry) as expiry_mock, \
             patch.object(document, 'apply_back_classifiers', side_effect=classify) as classifier_mock:
            path, report = document.run_document('front', 'back', output=directory, document_id=job_id)
            if job_id is not None:
                self.assertTrue(path.parent.name.startswith(f'document_{job_id}_'))
                self.assertTrue(path.is_file())
                self.assertTrue((path.parent / 'front').is_dir())
                self.assertTrue((path.parent / 'back').is_dir())
        return report, text_mock, expiry_mock, classifier_mock

    def test_oracle_job_id_is_in_document_result_folder(self):
        self.run_pipeline('agreed', job_id=123)

    def test_agreed_and_reconciled_continue_all_extraction_stages(self):
        for status in ('agreed', 'reconciled'):
            with self.subTest(status=status):
                report, text, expiry, classifier = self.run_pipeline(status)
                self.assertTrue(report['pipeline']['continued'])
                self.assertEqual(report['pipeline']['stage'], 'field_processing')
                self.assertEqual(report['national_id']['status'], status)
                self.assertEqual(report['sides']['front']['fields']['name']['text'], 'read text')
                self.assertEqual(report['sides']['back']['fields']['gender']['text'], 'label')
                text.assert_called_once()
                expiry.assert_called_once()
                classifier.assert_called_once()
                self.assertTrue(overall_confidence(report)[1]['idGatePassed'])

    def test_conflict_and_unverified_ids_skip_remaining_extraction(self):
        for status in ('conflict', 'single', 'unreadable', 'invalid_length', None):
            with self.subTest(status=status):
                report, text, expiry, classifier = self.run_pipeline(status)
                self.assertFalse(report['pipeline']['continued'])
                self.assertEqual(report['status'], 'id_mismatch' if status == 'conflict' else 'id_unverified')
                self.assertIsNone(report['sides']['front']['fields']['name']['text'])
                self.assertIn('agreed or reconciled', report['pipeline']['reason'])
                text.assert_not_called()
                expiry.assert_not_called()
                classifier.assert_not_called()
                self.assertFalse(overall_confidence(report)[1]['idGatePassed'])


if __name__ == '__main__':
    unittest.main()
