"""Load the Oracle worker's project-root .env before importing inference."""
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parents[2] / '.env'


def load_environment(path=ENV_FILE):
    # Disable interpolation so a literal ${...} in a password is preserved.
    # Existing process variables take precedence over file values.
    return load_dotenv(path, override=False, interpolate=False, encoding='utf-8-sig')
