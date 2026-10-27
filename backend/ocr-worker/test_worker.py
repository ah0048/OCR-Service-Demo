"""Regression tests for the queue/model boundary; no model weights or broker required."""
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference
import worker

ID = '29001010123456'
JOB = {'jobId': 'b6975489-81b6-40bb-adb7-aeec55f0c0aa',
       'correlationId': '71d724bc-f648-46dd-b568-6bab7158b7dc', 'imageReference': 'pair'}

def entry(text, **extra):
    return dict(text=text, status='extracted', needs_review=False, **extra)

class InferenceTests(unittest.TestCase):
    def report(self):
        return {'status': 'complete', 'review_required': False,
                'national_id': {'status': 'agreed', 'text': ID},
                'sides': {'front': {'fields': {'id': entry(ID), 'name': entry('اسم عربي'),
                                             'address': entry('عنوان')}},
                          'back': {'fields': {'id': entry(ID), 'expiredate': entry('01/02/2030', iso_date='2030-02-01'),
                                              'gender': entry('ذكر'), 'job': entry('مهندس'),
                                              'hasband': entry('اسم'), 'religion': entry('مسلم'),
                                              'marital': entry('متزوج')}}}}

    def test_maps_all_fields_and_dates_with_traceable_confidence(self):
        out = inference.map_report(self.report())
        self.assertEqual(out.fields['fullName'], 'اسم عربي')
        self.assertEqual(out.fields['nationalId'], ID)
        self.assertEqual(out.fields['birthDate'], '1990-01-01')
        self.assertEqual(out.fields['expiryDate'], '2030-02-01')
        self.assertTrue({'address', 'gender', 'occupation', 'spouseName', 'religion', 'maritalStatus',
                         'front.id', 'back.id'}.issubset(out.fields))
        self.assertEqual(out.confidence, 0.0)
        self.assertEqual(out.details['confidenceKind'], 'heuristic')

    def test_preserves_field_review_even_when_report_says_complete(self):
        report = self.report()
        report['sides']['back']['fields']['expiredate']['needs_review'] = True
        self.assertEqual(inference.map_report(report).outcome, 'LowConfidence')

    def test_conflicting_ids_keep_evidence_and_do_not_derive_birth_date(self):
        report = self.report()
        report.update(status='id_mismatch', national_id={'status': 'conflict', 'text': None})
        out = inference.map_report(report)
        self.assertEqual(out.outcome, 'Rejected')
        self.assertNotIn('nationalId', out.fields)
        self.assertNotIn('birthDate', out.fields)
        self.assertEqual(out.details['national_id']['status'], 'conflict')

    def test_score_combines_selected_detection_and_recognition(self):
        report = self.report()
        for side in report['sides'].values():
            for field in side['fields'].values():
                field.update(selected_crop='selected', candidates=[{'crop': 'selected', 'confidence': 0.9}],
                             ocr={'min_confidence': 0.8})
        out = inference.map_report(report)
        self.assertAlmostEqual(out.confidence, 0.8)
        self.assertFalse(out.details['confidenceCalculation']['calibrated'])

    def test_missing_required_fields_reduce_score_and_optional_spouse_does_not(self):
        report = self.report()
        for side in report['sides'].values():
            for field in side['fields'].values():
                field.update(ocr={'min_confidence': 0.9})
        report['sides']['back']['fields'].pop('hasband')
        self.assertEqual(inference.map_report(report).confidence, 0.9)
        report['sides']['front']['fields'].pop('address')
        self.assertEqual(inference.map_report(report).confidence, 0.8)

    def test_selected_crop_fallback_does_not_use_another_detections_score(self):
        report = self.report()
        field = report['sides']['front']['fields']['name']
        field.update(selected_crop='fallback', candidates=[{'crop': 'first', 'confidence': 0.99},
                                                          {'crop': 'fallback', 'confidence': 0.4}])
        out = inference.map_report(report)
        self.assertEqual(out.details['confidenceCalculation']['fields']['front.name']['score'], 0.4)

    def test_conflict_gates_even_high_field_scores_to_zero(self):
        report = self.report()
        for side in report['sides'].values():
            for field in side['fields'].values():
                field.update(ocr={'min_confidence': 0.99})
        report['national_id'] = {'status': 'conflict', 'text': None}
        out = inference.map_report(report)
        self.assertEqual(out.outcome, 'Rejected')
        self.assertEqual(out.confidence, 0)

    def test_detection_proxy_is_explicit_and_invalid_scores_are_ignored(self):
        report = self.report()
        field = report['sides']['front']['fields']['name']
        field.update(candidates=[{'crop': 'a', 'confidence': 0.75}], ocr={'confidence': float('nan')})
        item = inference.map_report(report).details['confidenceCalculation']['fields']['front.name']
        self.assertEqual(item['score'], 0.75)
        self.assertEqual(item['source'], 'detection_proxy')
        self.assertIsNone(item['recognitionScore'])

    def test_empty_detection_is_unreadable(self):
        self.assertEqual(inference.map_report({'sides': {}}).outcome, 'Unreadable')

    def test_shared_store_rejects_escape_and_missing_pair(self):
        with tempfile.TemporaryDirectory() as root, patch.object(inference, 'IMAGE_ROOT', Path(root)):
            for value in ['../outside', root, '', 'missing']:
                with self.assertRaises(ValueError):
                    inference.image_pair(value)

    def test_metadata_omits_machine_local_crop_paths(self):
        self.assertEqual(inference.public_report({'text': 'x', 'crop': 'secret', 'ocr': {'score': .9, 'model': 'private'}}),
                         {'text': 'x', 'ocr': {'score': .9}})


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.channel = Mock()
        self.method = SimpleNamespace(delivery_tag=1)

    def invoke(self, run, payload=None):
        with patch.object(worker, 'run_ocr', run), ThreadPoolExecutor(max_workers=1) as executor:
            worker.process(self.channel, self.method, None, json.dumps(JOB if payload is None else payload).encode(), executor)

    def result(self):
        return json.loads(self.channel.basic_publish.call_args.kwargs['body'])

    def test_heartbeats_continue_and_result_is_published_before_ack(self):
        def slow(_):
            time.sleep(.04)
            return inference.OcrOutput('LowConfidence', fields={'fullName': 'اسم'})
        self.invoke(slow)
        self.assertGreater(self.channel.connection.process_data_events.call_count, 0)
        calls = [c[0] for c in self.channel.mock_calls]
        self.assertLess(max(i for i,c in enumerate(calls) if c == 'basic_publish'), calls.index('basic_ack'))
        self.assertEqual(self.result()['extractedFields']['fullName'], 'اسم')

    def test_broker_publish_failure_never_acks_job(self):
        def publish(**kwargs):
            if kwargs['routing_key'] == worker.RESULTS_QUEUE:
                raise RuntimeError('broker failed')
        self.channel.basic_publish.side_effect = publish
        with self.assertRaises(RuntimeError):
            self.invoke(lambda _: inference.OcrOutput('Unreadable'))
        self.channel.basic_ack.assert_not_called()

    def test_inference_exception_publishes_terminal_technical_error(self):
        self.invoke(Mock(side_effect=RuntimeError('model failed')))
        self.assertEqual(self.result()['ocrOutcome'], 'TechnicalError')
        self.channel.basic_ack.assert_called_once()

    def test_malformed_payload_goes_to_dlq(self):
        self.invoke(Mock(), {'jobId': 'invalid'})
        self.channel.basic_nack.assert_called_once_with(1, requeue=False)
        self.channel.basic_ack.assert_not_called()

if __name__ == '__main__':
    unittest.main()
