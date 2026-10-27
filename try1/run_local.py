"""Run the front/back ID API directly with Python, without Docker.

Place this file in the project root (the folder that contains compose.yaml,
pipeline/, core/ and project_config.py).

First run (creates .venv and installs dependencies):
    python run_local.py --setup

Later runs:
    python run_local.py
    python run_local.py --fast
    python run_local.py --port 8080 --host 0.0.0.0

Any option not listed below is passed straight to the API server
(for example --device, --output, --fast).
"""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
VENV = ROOT / '.venv'
VENV_PYTHON = VENV / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
MIN_PYTHON = (3, 11)  # the server uses sys.exception(), added in 3.11
SKIP_DIRS = {'.venv', 'venv', '.git', '__pycache__', 'node_modules', 'outputs', 'results'}


def fail(message):
    print(f'\nERROR: {message}', file=sys.stderr)
    sys.exit(1)


def check_python():
    if sys.version_info < MIN_PYTHON:
        fail(f'Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; '
             f'this is {sys.version.split()[0]}. Install a newer Python from python.org.')


def in_project_venv():
    return Path(sys.prefix).resolve() == VENV.resolve()


def find_requirements():
    for name in ('requirements.txt', 'requirements.production.txt', 'requirements-cpu.txt',
                 'requirements/base.txt'):
        if (ROOT / name).is_file():
            return ROOT / name
    found = sorted(ROOT.glob('requirements*.txt'))
    return found[0] if found else None


def setup_venv():
    """Create .venv and install the project's dependencies into it."""
    if not VENV_PYTHON.is_file():
        print(f'Creating virtual environment in {VENV} ...', flush=True)
        subprocess.check_call([sys.executable, '-m', 'venv', str(VENV)])
    requirements = find_requirements()
    if requirements is None:
        fail('No requirements.txt found in the project root. Check the Dockerfile for the '
             '"pip install" line and save those packages to requirements.txt.')
    print(f'Installing dependencies from {requirements.name} (this can take a while) ...', flush=True)
    subprocess.check_call([str(VENV_PYTHON), '-m', 'pip', 'install', '--upgrade', 'pip'])
    subprocess.check_call([str(VENV_PYTHON), '-m', 'pip', 'install', '-r', str(requirements)])
    print('Setup complete.\n', flush=True)


def relaunch_in_venv(argv):
    """Re-run this script with the .venv interpreter and pass its exit code through."""
    try:
        return subprocess.call([str(VENV_PYTHON), str(Path(__file__).resolve()), *argv])
    except KeyboardInterrupt:
        return 0


def load_env_file(path):
    """Load KEY=VALUE lines from .env, like docker compose does. Existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip().removeprefix('export ').strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def find_server_script(explicit=None):
    """Find the API server module: the file that defines PipelineServer."""
    if explicit:
        path = (ROOT / explicit).resolve()
        if not path.is_file():
            fail(f'Server script not found: {path}')
        return path
    this_file = Path(__file__).resolve()
    candidates = []
    for path in ROOT.rglob('*.py'):
        if path.resolve() == this_file or SKIP_DIRS & set(path.relative_to(ROOT).parts):
            continue
        try:
            text = path.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if 'class PipelineServer(' in text and "__name__ == '__main__'" in text:
            candidates.append(path)
    if not candidates:
        fail('Could not find the API server file (the one defining PipelineServer). '
             'Pass it explicitly, e.g.: python run_local.py --server api.py')
    # Prefer the server that has its web page beside it, then the shallowest path.
    candidates.sort(key=lambda p: (not (p.parent / 'index.html').is_file(),
                                   len(p.relative_to(ROOT).parts)))
    return candidates[0]


def check_project_files(server_script):
    missing = [name for name in ('index.html', 'assets')
               if not (server_script.parent / name).exists()]
    if not (ROOT / 'project_config.py').is_file():
        missing.append('project_config.py (project root)')
    if missing:
        fail('Missing project files: ' + ', '.join(missing) +
             '\nMake sure run_local.py sits in the project root.')


def check_dependencies():
    missing = []
    for module in ('PIL', 'torch'):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        fail('Missing Python packages: ' + ', '.join(missing) +
             '\nRun: python run_local.py --setup')


def main():
    check_python()
    parser = argparse.ArgumentParser(
        description='Run the ID API locally without Docker. Unknown options go to the server.')
    parser.add_argument('--setup', action='store_true',
                        help='create .venv and install requirements before starting')
    parser.add_argument('--no-venv', action='store_true',
                        help='use the current Python instead of the project .venv')
    parser.add_argument('--server', help='server script path, relative to the project root '
                                         '(found automatically if omitted)')
    parser.add_argument('--host', default='127.0.0.1',
                        help='interface to bind (default 127.0.0.1; use 0.0.0.0 for other machines)')
    parser.add_argument('--port', default='8000')
    args, server_args = parser.parse_known_args()

    if args.setup:
        setup_venv()

    if not args.no_venv and not in_project_venv():
        if VENV_PYTHON.is_file():
            forwarded = [a for a in sys.argv[1:] if a != '--setup']
            sys.exit(relaunch_in_venv(forwarded))
        print('No .venv found; using the current Python. '
              'Run "python run_local.py --setup" to create one.', flush=True)

    os.chdir(ROOT)
    load_env_file(ROOT / '.env')
    server_script = find_server_script(args.server)
    check_project_files(server_script)
    check_dependencies()

    for folder in (ROOT, server_script.parent):
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))

    print(f'Starting {server_script.relative_to(ROOT)} without Docker '
          f'(Python {sys.version.split()[0]}) ...', flush=True)
    print(f'Open http://localhost:{args.port} once you see "Ready."\n', flush=True)
    sys.argv = [str(server_script), '--host', args.host, '--port', args.port, *server_args]
    try:
        runpy.run_path(str(server_script), run_name='__main__')
    except KeyboardInterrupt:
        print('\nServer stopped.')


if __name__ == '__main__':
    main()