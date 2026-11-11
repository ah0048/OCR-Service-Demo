"""Package the standalone worker with an allowlist; never include secrets or outputs."""
import argparse
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import tarfile

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_MODULES = (
    'try1/project_config.py', 'try1/requirements.production.txt',
    'try1/core/__init__.py', 'try1/core/runtime.py',
    'try1/core/detector/__init__.py', 'try1/core/digits/__init__.py',
    'try1/core/digits/date_reader.py',
    'try1/core/digits/files/arabic_digit_ai/model.py',
    'try1/core/digits/files/digit_extractor/extract_digits.py',
    'try1/core/digits/files/digit_extractor/strict_id.py',
    'try1/core/fieldmatch/__init__.py', 'try1/core/fieldmatch/classifier.py',
    'try1/core/fieldmatch/options.py',
    'try1/core/ocr_old/__init__.py', 'try1/core/ocr_old/reader.py',
    'try1/pipeline/__init__.py', 'try1/pipeline/arabic_text.py',
    'try1/pipeline/document.py', 'try1/pipeline/front_id.py', 'try1/pipeline/id_agreement.py',
)
CHECKPOINTS = (
    'try1/core/detector/weights/best.pt',
    'try1/core/detector/id-back detect/runs_id_back/yolo26_id_back_v2/weights/best.pt',
    'try1/core/digits/files/arabic_digit_ai/models/arabic_digit_cnn.pt',
    'try1/core/fieldmatch/models/gender/best.pt',
    'try1/core/fieldmatch/models/religion/best.pt',
    'try1/core/fieldmatch/models/marital/best.pt',
)
MODEL_DIRECTORY = 'try1/core/ocr_old/bundle/models/Qari-OCR-merged'
MODEL_REQUIRED = ('config.json', 'model.safetensors', 'tokenizer_config.json',
                  'tokenizer.json', 'preprocessor_config.json')


def digest_file(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def collect_files(root, include_models=True):
    root = root.resolve()
    selected = {root / name for name in RUNTIME_MODULES}
    selected.update(root / name for name in ('README.md', '.env.example', '.gitignore',
                                            'try1/ASSETS.md', 'try1/CUDA_README.md'))
    for directory, patterns in [('backend/ocr-worker', ('*.py', 'requirements.oracle.txt')),
                                ('database/oracle', ('*.sql', '*.md')),
                                ('scripts', ('*.py',))]:
        for pattern in patterns:
            selected.update((root / directory).glob(pattern))
    if include_models:
        selected.update(root / name for name in CHECKPOINTS)
        model = root / MODEL_DIRECTORY
        selected.update(model / name for name in MODEL_REQUIRED)
        selected.update(path for path in model.rglob('*') if path.is_file()
                        and not any(part.startswith('.') or part == '__pycache__'
                                    for part in path.relative_to(model).parts))
    for path in selected:
        if not path.is_file():
            raise FileNotFoundError(f'Required transfer file is missing: {path.relative_to(root)}')
        if not path.resolve().is_relative_to(root):
            raise ValueError(f'Transfer input resolves outside the project: {path}')
        if path.stat().st_size == 0 and path.suffix in ('.pt', '.safetensors'):
            raise ValueError(f'Empty model checkpoint: {path}')
        if path.suffix in ('.pt', '.safetensors'):
            with path.open('rb') as source:
                if source.read(128).startswith(b'version https://git-lfs.github.com/spec/'):
                    raise ValueError(f'Model is a Git LFS pointer, not weights: {path}')
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def create_archive(root, destination, include_models=True):
    root = root.resolve()
    files = collect_files(root, include_models)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest = {'format': 1, 'includesModels': include_models, 'files': {}}
    # Exclusive creation protects a previously built transfer package.
    with destination.open('xb') as output:
        with tarfile.open(fileobj=output, mode='w:gz', compresslevel=1) as archive:
            for path in files:
                name = path.relative_to(root).as_posix()
                size = path.stat().st_size
                print(f'Adding {name} ({size:,} bytes)', flush=True)
                manifest['files'][name] = {'size': size, 'sha256': digest_file(path)}
                archive.add(path, arcname=name, recursive=False)
            body = json.dumps(manifest, indent=2).encode('utf-8')
            info = tarfile.TarInfo('TRANSFER_MANIFEST.json')
            info.size = len(body)
            archive.addfile(info, io.BytesIO(body))
    checksum = destination.with_name(destination.name + '.sha256')
    checksum.write_text(f'{digest_file(destination)}  {destination.name}\n', encoding='ascii')
    print(f'Archive: {destination}\nChecksum: {checksum}', flush=True)
    return manifest


def verify_archive(path):
    actual = {}
    seen = set()
    manifest = None
    # Stream once: do not repeatedly rewind/decompress multi-gigabyte weights.
    with tarfile.open(path, 'r|gz') as archive:
        for member in archive:
            if member.name in seen:
                raise ValueError('Archive has duplicate names')
            seen.add(member.name)
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or not member.isfile():
                raise ValueError(f'Unexpected archive member: {member.name}')
            with archive.extractfile(member) as source:
                if member.name == 'TRANSFER_MANIFEST.json':
                    manifest = json.load(source)
                    continue
                result = hashlib.sha256()
                size = 0
                for chunk in iter(lambda: source.read(1024 * 1024), b''):
                    size += len(chunk)
                    result.update(chunk)
                actual[member.name] = {'size': size, 'sha256': result.hexdigest()}
    if manifest is None or actual != manifest['files']:
        raise ValueError('Archive files/hashes do not match its manifest')
    print(f'Verified {len(manifest["files"])} files: {path}', flush=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path,
                        default=ROOT / '.data/dist' / f'oracle-ocr-{datetime.now():%Y%m%d-%H%M%S}.tar.gz')
    parser.add_argument('--code-only', action='store_true', help='Exclude weights only if target already has them')
    parser.add_argument('--verify', type=Path, help='Verify an existing archive without extracting it')
    args = parser.parse_args()
    if args.verify:
        verify_archive(args.verify)
    else:
        create_archive(ROOT, args.output, include_models=not args.code_only)


if __name__ == '__main__':
    main()
