"""Compare local card inference with separate processes per Qari batch size."""
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time


def synchronize(torch, device):
    if device == 'cuda':
        torch.cuda.synchronize()
    elif device == 'mps':
        torch.mps.synchronize()


def peak_rss_mib():
    try:
        import resource
    except ImportError:
        return None  # resource is unavailable on Windows.
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / (1024 ** 2 if sys.platform == 'darwin' else 1024)


def fingerprint(output):
    """Compare fields without saving their extracted values in the report."""
    statuses = {}
    batched_crops = 0
    for side, result in (output.details or {}).get('sides', {}).items():
        for name, field in (result.get('fields') or {}).items():
            statuses[f'{side}.{name}'] = field.get('status')
            batched_crops += int((field.get('ocr') or {}).get('batch_size', 1) > 1)
    return {
        'outcome': output.outcome,
        'confidence': output.confidence,
        'fields': {name: hashlib.sha256(value.encode('utf-8')).hexdigest()
                   for name, value in (output.fields or {}).items()},
        'statuses': statuses,
        'batchedCrops': batched_crops,
    }


def compare(baseline, candidate):
    return {
        'changedFields': sorted(name for name in baseline['fields'].keys() | candidate['fields'].keys()
                                if baseline['fields'].get(name) != candidate['fields'].get(name)),
        'changedStatuses': sorted(name for name in baseline['statuses'].keys() | candidate['statuses'].keys()
                                  if baseline['statuses'].get(name) != candidate['statuses'].get(name)),
        'outcomeChanged': baseline['outcome'] != candidate['outcome'],
        'confidenceChanged': baseline['confidence'] != candidate['confidence'],
    }


class BatchFailures(logging.Handler):
    def __init__(self):
        super().__init__()
        self.count = 0

    def emit(self, record):
        if record.getMessage().startswith('Qari batch failed;'):
            self.count += 1


def run_variant(args):
    # This process owns exactly one set of models; it never imports worker.py.
    os.environ['QARI_BATCH_SIZE'] = str(args.variant)
    os.environ['OCR_DEVICE'] = args.device
    os.environ['YOLO_AUTOINSTALL'] = 'false'
    import inference
    inference.initialize()
    import torch
    from pipeline.document import run_document

    failures = BatchFailures()
    logging.getLogger('core.ocr_old.reader').addHandler(failures)

    def infer(pair):
        front, back = map(Path, pair)
        inference.validate_image(front)
        inference.validate_image(back)
        with tempfile.TemporaryDirectory(prefix='ocr-benchmark-') as directory:
            _, report = run_document(front, back, output=directory,
                                     device=inference.DEVICE, qari_device=inference.DEVICE)
            return fingerprint(inference.map_report(report))

    # One full-card warmup per variant, excluded from timing.
    infer(args.pair[0])
    synchronize(torch, inference.DEVICE)
    failures.count = 0
    if inference.DEVICE == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    samples = []
    for repeat in range(args.repeats):
        for index, pair in enumerate(args.pair):
            synchronize(torch, inference.DEVICE)
            started = time.perf_counter()
            output = infer(pair)
            synchronize(torch, inference.DEVICE)
            samples.append(dict(output, pair=index + 1, repeat=repeat + 1,
                                seconds=time.perf_counter() - started))
    result = {'batchSize': args.variant, 'device': inference.DEVICE, 'samples': samples,
              'peakProcessRssMiB': peak_rss_mib(), 'batchFallbacks': failures.count}
    if inference.DEVICE == 'cuda':
        result['peakCudaAllocatedMiB'] = torch.cuda.max_memory_allocated() / 1024 ** 2
    Path(args.variant_output).write_text(json.dumps(result), encoding='utf-8')


def summarize(variants):
    baseline = variants[0]
    summaries = []
    for variant in variants:
        samples = variant['samples']
        total = sum(sample['seconds'] for sample in samples)
        comparisons = [dict(pair=sample['pair'], repeat=sample['repeat'], **compare(original, sample))
                       for original, sample in zip(baseline['samples'], samples)]
        summaries.append({
            'batchSize': variant['batchSize'], 'device': variant['device'],
            'timedCards': len(samples), 'totalSeconds': round(total, 3),
            'medianCardSeconds': round(statistics.median(s['seconds'] for s in samples), 3),
            'cardsPerMinute': round(len(samples) * 60 / total, 3),
            'peakProcessRssMiB': variant['peakProcessRssMiB'],
            **({'peakCudaAllocatedMiB': variant['peakCudaAllocatedMiB']}
               if 'peakCudaAllocatedMiB' in variant else {}),
            'batchFallbacks': variant['batchFallbacks'],
            'batchedCrops': sum(s['batchedCrops'] for s in samples),
            'outcomeCounts': {outcome: sum(s['outcome'] == outcome for s in samples)
                              for outcome in sorted({s['outcome'] for s in samples})},
            'differencesFromBaseline': [comparison for comparison in comparisons
                                        if comparison['changedFields'] or comparison['changedStatuses']
                                        or comparison['outcomeChanged'] or comparison['confidenceChanged']],
        })
    return {'variants': summaries,
            'notes': ['One untimed warmup card per variant; model loading is excluded from timings.',
                      'Process peak RSS includes startup and warmup; it is not total GPU/unified-memory usage.',
                      'Output equality compares against batch size 1, not against labeled ground truth.',
                      'Batch size 1 runs first; rerun to check for temperature/load effects.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair', nargs=2, action='append', required=True, metavar=('FRONT', 'BACK'))
    parser.add_argument('--device', choices=('cpu', 'cuda', 'mps', 'auto'), default='auto')
    parser.add_argument('--batch-size', type=int, default=2, help='Candidate size; baseline is always 1')
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--output', type=Path, help='New summary JSON file; existing files are not overwritten')
    parser.add_argument('--variant', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--variant-output', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeats < 1 or args.batch_size < 2:
        parser.error('--repeats must be positive and --batch-size must be at least 2')
    if any(not Path(path).is_file() for pair in args.pair for path in pair):
        parser.error('Every front/back image must be an existing local file')
    if args.output and args.output.exists():
        parser.error('--output already exists; choose a new report filename')
    if args.variant is not None:
        run_variant(args)
        return
    variants = []
    with tempfile.TemporaryDirectory(prefix='ocr-benchmark-results-') as directory:
        for size in (1, args.batch_size):
            print(f'Benchmarking Qari batch size {size}...', file=sys.stderr, flush=True)
            target = Path(directory) / f'{size}.json'
            command = [sys.executable, str(Path(__file__).resolve()), '--variant', str(size),
                       '--variant-output', str(target), '--device', args.device,
                       '--repeats', str(args.repeats), '--batch-size', str(args.batch_size)]
            for pair in args.pair:
                command.extend(['--pair', *pair])
            subprocess.run(command, check=True, stdout=sys.stderr)
            variants.append(json.loads(target.read_text(encoding='utf-8')))
    report = json.dumps(summarize(variants), indent=2)
    if args.output:
        with args.output.open('x', encoding='utf-8') as output:
            output.write(report + '\n')
    print(report)


if __name__ == '__main__':
    main()
