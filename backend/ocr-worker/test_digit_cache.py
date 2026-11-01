"""Check digit-reader reuse without requiring installed ML packages or weights."""
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

# Isolated subprocess: dependency stubs never affect other worker tests.
for name in ('PIL', 'cv2', 'numpy', 'torch'):
    sys.modules[name] = ModuleType(name)
sys.modules['PIL'].Image = SimpleNamespace()
sys.modules['PIL'].ImageOps = SimpleNamespace()
sys.path.insert(0, str(Path('try1').resolve()))
from pipeline.front_id import load_extractor
from project_config import DIGIT_EXTRACTOR
"""


class DigitCacheTests(unittest.TestCase):
    def check_script(self, script):
        result = subprocess.run(
            [sys.executable, '-c', textwrap.dedent(BOOTSTRAP) + textwrap.dedent(script)],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_document_sides_and_date_reader_share_one_model_load(self):
        self.check_script("""
            from core.digits import date_reader
            from core.digits.files.digit_extractor import extract_digits as digits

            readers = [load_extractor(DIGIT_EXTRACTOR),
                       load_extractor(str(DIGIT_EXTRACTOR)),
                       load_extractor(Path('try1') / DIGIT_EXTRACTOR.relative_to(Path('try1').resolve()))]
            assert all(reader is digits.extract_id_details for reader in readers)
            assert date_reader.digits is digits
            strict = digits._strict_module()

            # Exercise the real cached model loader using a fake CNN/checkpoint.
            model = Mock()
            model.eval.return_value = model
            architecture = SimpleNamespace(ArabicDigitCNN=Mock(return_value=model))
            spec = SimpleNamespace(loader=SimpleNamespace(exec_module=lambda module: None))
            checkpoint = Mock(return_value={'weights': 'fixture'})
            with patch.object(strict.importlib.util, 'spec_from_file_location', return_value=spec), \\
                 patch.object(strict.importlib.util, 'module_from_spec', return_value=architecture), \\
                 patch.object(sys.modules['torch'], 'load', checkpoint, create=True):
                models = [reader.__globals__['_strict_module']()._model() for reader in readers]
                models.append(date_reader.digits._strict_module()._model())
            assert all(value is model for value in models)
            assert checkpoint.call_count == 1, checkpoint.call_count
            assert architecture.ArabicDigitCNN.call_count == 1
        """)

    def test_custom_extractor_reused_and_missing_path_still_rejected(self):
        self.check_script("""
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'custom.py'
                path.write_text('state = object()\\ndef extract_digits(image): return state\\n')
                first = load_extractor(path)
                second = load_extractor(str(path))
                assert first is second
                assert first('front') is second('back')
                try:
                    load_extractor(Path(directory) / 'missing.py')
                except FileNotFoundError:
                    pass
                else:
                    raise AssertionError('Missing extractor must be rejected')
        """)


if __name__ == '__main__':
    unittest.main()
