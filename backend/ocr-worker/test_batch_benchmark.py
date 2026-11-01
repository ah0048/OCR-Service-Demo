"""Benchmark reporting/orchestration checks without inference dependencies."""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_batch as benchmark


def output(text='private sample', outcome='Success'):
    return SimpleNamespace(outcome=outcome, confidence=.9, fields={'fullName': text},
                           details={'sides': {'front': {'fields': {
                               'name': {'status': 'extracted', 'ocr': {'batch_size': 2}}}}}})


class BenchmarkTests(unittest.TestCase):
    def test_comparison_flags_added_missing_changed_fields_and_statuses(self):
        first = benchmark.fingerprint(output())
        other = benchmark.fingerprint(output('different', 'LowConfidence'))
        other['fields']['address'] = 'new'
        other['statuses']['front.name'] = 'needs_review'
        changes = benchmark.compare(first, other)
        self.assertEqual(changes['changedFields'], ['address', 'fullName'])
        self.assertEqual(changes['changedStatuses'], ['front.name'])
        self.assertTrue(changes['outcomeChanged'])
        self.assertFalse(changes['confidenceChanged'])

    def test_summary_reports_fallbacks_and_keeps_values_and_fingerprints_private(self):
        baseline = dict(benchmark.fingerprint(output()), pair=1, repeat=1, seconds=10)
        candidate = dict(benchmark.fingerprint(output('changed private sample')), pair=1, repeat=1, seconds=5)
        variants = [dict(batchSize=size, device='mps', samples=[sample], peakProcessRssMiB=4000,
                         batchFallbacks=fallbacks)
                    for size, sample, fallbacks in [(1, baseline, 0), (2, candidate, 1)]]
        summary = benchmark.summarize(variants)
        self.assertEqual(summary['variants'][0]['cardsPerMinute'], 6)
        self.assertEqual(summary['variants'][1]['cardsPerMinute'], 12)
        self.assertEqual(summary['variants'][1]['batchFallbacks'], 1)
        self.assertEqual(summary['variants'][1]['differencesFromBaseline'][0]['changedFields'], ['fullName'])
        serialized = json.dumps(summary)
        self.assertNotIn('private sample', serialized)
        self.assertNotIn(baseline['fields']['fullName'], serialized)

    def test_variant_initializes_once_and_excludes_one_warmup_from_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'result.json'
            inference = SimpleNamespace(initialize=Mock(), DEVICE='cpu', validate_image=Mock(),
                                        map_report=Mock(return_value=output()))
            document = SimpleNamespace(run_document=Mock(return_value=('unused', {})))
            args = argparse.Namespace(variant=2, device='cpu', pair=[['f1', 'b1'], ['f2', 'b2']],
                                      repeats=2, variant_output=str(target))
            with patch.dict(os.environ), \
                 patch.dict(sys.modules, {'inference': inference, 'torch': Mock(), 'pipeline.document': document}), \
                 patch.object(benchmark, 'peak_rss_mib', return_value=123):
                benchmark.run_variant(args)
                self.assertEqual(os.environ['QARI_BATCH_SIZE'], '2')
            report = json.loads(target.read_text())
            inference.initialize.assert_called_once_with()
            self.assertEqual(document.run_document.call_count, 5)
            self.assertEqual(len(report['samples']), 4)
            self.assertEqual([(s['pair'], s['repeat']) for s in report['samples']],
                             [(1, 1), (2, 1), (1, 2), (2, 2)])
            self.assertEqual(report['peakProcessRssMiB'], 123)
            self.assertNotIn('private sample', target.read_text())

    def test_peak_rss_units_are_bytes_on_macos_and_kib_on_linux(self):
        resource = SimpleNamespace(RUSAGE_SELF=0, getrusage=Mock(return_value=SimpleNamespace(ru_maxrss=1048576)))
        with patch.dict(sys.modules, {'resource': resource}):
            with patch.object(sys, 'platform', 'darwin'):
                self.assertEqual(benchmark.peak_rss_mib(), 1)
            with patch.object(sys, 'platform', 'linux'):
                self.assertEqual(benchmark.peak_rss_mib(), 1024)


if __name__ == '__main__':
    unittest.main()
