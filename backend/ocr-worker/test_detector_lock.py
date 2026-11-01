"""Exercise real pipeline prediction scheduling without ML packages or weights."""
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

image = SimpleNamespace(size=(100, 50))
pil = SimpleNamespace(Image=MagicMock(), ImageOps=Mock())
pil.ImageOps.exif_transpose.return_value.convert.return_value = image
sys.modules['PIL'] = pil
sys.path.insert(0, str(Path('try1').resolve()))
from pipeline import front_id
"""


class DetectorLockTests(unittest.TestCase):
    def check_script(self, script):
        result = subprocess.run(
            [sys.executable, '-c', textwrap.dedent(BOOTSTRAP) + textwrap.dedent(script)],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_same_model_serializes_but_separate_cpu_cuda_models_can_overlap(self):
        self.check_script("""
            for device, same_model, expected_peak in (
                    ('cpu', True, 1), ('cuda', True, 1),
                    ('mps', True, 1), ('mps', False, 1),
                    ('cpu', False, 2), ('cuda', False, 2)):
                active = peak = 0
                counter = threading.Lock()
                overlap = threading.Barrier(2)
                def predict(**kwargs):
                    global active, peak
                    with counter:
                        active += 1
                        peak = max(peak, active)
                    if expected_peak == 2:
                        overlap.wait(timeout=5)
                    time.sleep(.03)
                    with counter:
                        active -= 1
                    return [SimpleNamespace(boxes=[], save=Mock())]
                factory = Mock(side_effect=lambda path: SimpleNamespace(
                    names={0:'id', 1:'name', 2:'address'}, predict=predict))
                front_id.load_detector.cache_clear()
                with tempfile.TemporaryDirectory() as directory:
                    source = Path(directory) / 'card.jpg'; source.touch()
                    first = Path(directory) / 'front.pt'; first.touch()
                    second = first if same_model else Path(directory) / 'back.pt'
                    second.touch()
                    def run(path):
                        return front_id.run_pipeline(source, output=directory, model_path=path,
                                                     device=device)
                    with patch.dict(sys.modules, {'ultralytics': SimpleNamespace(YOLO=factory)}), \\
                         patch.object(front_id, 'load_extractor'), \\
                         patch.object(front_id, 'crop_fields', return_value={}):
                        with ThreadPoolExecutor(max_workers=2) as pool:
                            results = list(pool.map(run, [first, str(second)]))
                    assert peak == expected_peak, (device, same_model, peak)
                    assert factory.call_count == (1 if same_model else 2)
                    assert len({str(path) for path, report in results}) == 2
        """)

    def test_prediction_failure_releases_model_and_mps_locks(self):
        self.check_script("""
            model = SimpleNamespace(names={0:'id', 1:'name', 2:'address'}, predict=Mock(
                side_effect=[RuntimeError('prediction failed'), [SimpleNamespace(boxes=[], save=Mock())]]))
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory) / 'card.jpg'; source.touch()
                weights = Path(directory) / 'front.pt'; weights.touch()
                with patch.object(front_id, 'load_detector', return_value=model), \\
                     patch.object(front_id, 'load_extractor'), \\
                     patch.object(front_id, 'crop_fields', return_value={}):
                    try:
                        front_id.run_pipeline(source, output=directory, model_path=weights, device='mps')
                    except RuntimeError:
                        pass
                    else:
                        raise AssertionError('Prediction error must propagate')
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        future = pool.submit(front_id.run_pipeline, source, output=directory,
                                             model_path=weights, device='mps')
                        path, report = future.result(timeout=5)
                    assert path.is_file()
        """)


if __name__ == '__main__':
    unittest.main()
