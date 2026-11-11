"""Check real dotenv parsing and precedence without database credentials."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from oracle_config import ENV_FILE, load_environment


class OracleConfigTests(unittest.TestCase):
    def test_default_path_is_project_root_not_current_directory(self):
        self.assertEqual(ENV_FILE, Path(__file__).resolve().parents[2] / '.env')

    def test_file_loads_literals_and_existing_environment_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / '.env'
            path.write_text("ORACLE_DSN=example:1521/service\n"
                            "ORACLE_USER=file_user\n"
                            "ORACLE_PASSWORD='literal${VALUE}#password'\n"
                            "ORACLE_CLIENT_LIB_DIR='C:\\oracle\\client'\n"
                            "OCR_DEVICE=cpu\n", encoding='utf-8-sig')
            os.environ['ORACLE_USER'] = 'environment_user'
            load_environment(path)
            self.assertEqual(os.environ['ORACLE_DSN'], 'example:1521/service')
            self.assertEqual(os.environ['ORACLE_USER'], 'environment_user')
            self.assertEqual(os.environ['ORACLE_PASSWORD'], 'literal${VALUE}#password')
            self.assertEqual(os.environ['ORACLE_CLIENT_LIB_DIR'], 'C:\\oracle\\client')
            self.assertEqual(os.environ['OCR_DEVICE'], 'cpu')

    def test_missing_file_is_optional(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(load_environment(Path(directory) / 'missing.env'))

    def test_command_line_overrides_environment_and_password_need_not_be_prompted(self):
        import oracle_worker
        with patch.dict(os.environ, {'ORACLE_DSN': 'env:1521/service',
                                     'ORACLE_PASSWORD': 'test-only'}, clear=True), \
             patch.object(sys, 'argv', ['oracle_worker.py', '--dsn', 'cli:1521/service']), \
             patch.object(oracle_worker.getpass, 'getpass') as prompt:
            args = oracle_worker.parse_args()
        self.assertEqual(args.dsn, 'cli:1521/service')
        self.assertEqual(args.password, 'test-only')
        prompt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
