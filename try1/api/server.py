"""Local HTTP API for front/back ID extraction; all model inference stays offline."""
import argparse
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import warnings

from PIL import Image
from pipeline.front_id import DEFAULT_MODEL, ROOT, load_detector, load_qari, run_pipeline
from project_config import API_OUTPUT, BACK_DETECTOR_MODEL, FIELD_CLASSIFIER_MODELS
from pipeline.document import run_document
from core.runtime import resolve_device

MAX_BODY_BYTES = 41 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 25_000_000
FORMATS = {'JPEG': '.jpg', 'PNG': '.png', 'WEBP': '.webp', 'BMP': '.bmp', 'TIFF': '.tiff'}

PAGE_PATH = Path(__file__).with_name('index.html')
PAGE = PAGE_PATH.read_bytes()
ASSET_DIR = PAGE_PATH.with_name('assets')
ASSETS = {
    '/assets/cover.jpg': ('cover.jpg', 'image/jpeg'),
    '/assets/egx-white.png': ('egx-white.png', 'image/png'),
    '/assets/bell.png': ('bell.png', 'image/png'),
}


class RequestError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


def extract_image(body, content_type):
    if content_type.lower().startswith('multipart/form-data'):
        if '\r' in content_type or '\n' in content_type:
            raise RequestError(400, 'Invalid Content-Type')
        message = BytesParser(policy=default).parsebytes(
            ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
        if not message.is_multipart() or message.defects:
            raise RequestError(400, 'Invalid multipart upload')
        images = [part.get_payload(decode=True) for part in message.iter_parts()
                  if part.get_param('name', header='content-disposition') == 'image']
        if len(images) != 1:
            raise RequestError(400, 'Upload exactly one file in the image field')
        body = images[0]
    elif not (content_type.lower().startswith('image/') or
              content_type.lower().split(';')[0] == 'application/octet-stream'):
        raise RequestError(415, 'Use multipart/form-data with an image field, or an image/* body')
    if not body:
        raise RequestError(400, 'Image is empty')
    if len(body) > MAX_IMAGE_BYTES:
        raise RequestError(413, 'Image exceeds 20 MB')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(BytesIO(body)) as image:
                if image.format not in FORMATS:
                    raise RequestError(415, 'Supported formats: JPEG, PNG, WEBP, BMP, TIFF')
                suffix = FORMATS[image.format]
                if image.width * image.height > MAX_PIXELS:
                    raise RequestError(413, 'Image exceeds 25 million pixels')
                if getattr(image, 'n_frames', 1) != 1:
                    raise RequestError(400, 'Upload a single-frame image')
                image.verify()
            with Image.open(BytesIO(body)) as image:
                image.load()
    except RequestError:
        raise
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise RequestError(413, 'Image dimensions are too large')
    except Exception:
        raise RequestError(400, 'Image is corrupt or is not a supported image')
    return body, suffix


def extract_uploads(body, content_type):
    """Validate both images before saving or running either side.

    Legacy single-image uploads remain supported for existing clients.
    A request containing front or back must contain exactly one of each.
    """
    if not content_type.lower().startswith('multipart/form-data'):
        return {'image': extract_image(body, content_type)}
    if '\r' in content_type or '\n' in content_type:
        raise RequestError(400, 'Invalid Content-Type')
    message = BytesParser(policy=default).parsebytes(
        ('Content-Type: ' + content_type + '\r\nMIME-Version: 1.0\r\n\r\n').encode() + body)
    if not message.is_multipart() or message.defects:
        raise RequestError(400, 'Invalid multipart upload')
    parts = {}
    for part in message.iter_parts():
        name = part.get_param('name', header='content-disposition')
        if name not in ('front', 'back', 'image') or name in parts or part.is_multipart():
            raise RequestError(400, 'Use exactly one front and one back file; duplicate or unknown fields are not allowed')
        parts[name] = part.get_payload(decode=True)
    if set(parts) not in ({'front', 'back'}, {'image'}):
        raise RequestError(400, 'Upload both front and back files')
    return {name: extract_image(data, 'application/octet-stream') for name, data in parts.items()}


def qari_ready(device='cpu'):
    """Report the engine this process will actually use, and whether it loads.

    The default-engine check alone is misleading when a server was started with
    QARI_ENGINE=v4, so the cached reader's own engine is reported too.
    """
    info = {'engine': None, 'weights_present': False, 'adapter_present': False,
            'model_dir': None, 'loaded': False}
    try:
        from core.ocr_old.reader import _default_locations, check_model_files
        model_dir, adapter_dir = _default_locations()
        directory, adapter, engine = check_model_files(model_dir, adapter_dir)
        info.update(engine=engine, weights_present=True, model_dir=str(directory),
                    adapter_present=adapter is not None)
        # Do not evict the cached inference reader during health checks.
        info['loaded'] = load_qari(None, device).model is not None
    except Exception as exc:
        info['error'] = f'{type(exc).__name__}: {exc}'
    return info


def warm_up(device='cpu'):
    """Preload the cached detector and the Qari reader before serving requests.

    Loading the detector takes seconds and Qari takes minutes on CPU; doing it
    once here keeps the first real request fast. Failures are nonfatal: requests
    retry lazily and report the same errors they would have before.
    """
    try:
        load_detector(DEFAULT_MODEL)
        load_detector(BACK_DETECTOR_MODEL)
        from pipeline.document import load_field_classifier
        for checkpoint in FIELD_CLASSIFIER_MODELS.values():
            load_field_classifier(Path(checkpoint).resolve())
        print('Front/back detectors and field classifiers preloaded; warming Qari...', flush=True)
        reader = load_qari(None, device)
        reader.warm()
        print(f'Old Qari model loaded on {reader.device}.', flush=True)
    except Exception as exc:
        print(f'Preload failed (requests will load lazily): {exc}', flush=True)
        if os.environ.get('REQUIRE_MODEL_WARMUP', '').lower() in ('1', 'true', 'yes'):
            raise


class PipelineServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, output, runner=run_pipeline, device='cpu', document_runner=run_document):
        self.device = resolve_device(device)
        self.output = Path(output).resolve()
        self.runner = runner
        self.document_runner = document_runner
        self.inference_lock = threading.Lock()
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        if isinstance(sys.exception(), ConnectionResetError):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = 'IDPipelineAPI/2.0'

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def log_message(self, format, *args):
        # Avoid logging filenames, query strings, or ID text.
        pass

    def send_body(self, status, body, content_type='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_body(status, json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/':
            self.send_body(200, PAGE_PATH.read_bytes(), 'text/html; charset=utf-8')
        elif path in ASSETS:
            filename, content_type = ASSETS[path]
            self.send_body(200, (ASSET_DIR / filename).read_bytes(), content_type)
        elif path == '/health':
            self.send_json(200, {'status': 'ok', 'detector_weights_present': DEFAULT_MODEL.is_file(),
                                 'back_detector_weights_present': BACK_DETECTOR_MODEL.is_file(),
                                 'qari': qari_ready(self.server.device),
                                 'device': self.server.device,
                                 'busy': self.server.inference_lock.locked(),
                                 'max_image_mb': 20})
        else:
            self.send_json(404, {'error': 'Not found'})

    def do_POST(self):
        locked = False
        try:
            url = urlsplit(self.path)
            if url.path != '/predict':
                raise RequestError(404, 'Not found')
            query = parse_qs(url.query, keep_blank_values=True)
            if set(query) - {'qari'} or len(query.get('qari', ['true'])) != 1:
                raise RequestError(400, 'Only one qari=true or qari=false parameter is supported')
            qari = query.get('qari', ['true'])[0].lower()
            if qari not in ('true', 'false'):
                raise RequestError(400, 'qari must be true or false')
            if self.headers.get('Transfer-Encoding'):
                raise RequestError(400, 'Chunked uploads are not supported; send Content-Length')
            lengths = self.headers.get_all('Content-Length', [])
            if not lengths:
                raise RequestError(411, 'Content-Length is required')
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise RequestError(400, 'Invalid Content-Length')
            length = int(lengths[0])
            if not 0 < length <= MAX_BODY_BYTES:
                raise RequestError(413 if length else 400, 'Request body must be between 1 byte and 41 MB')
            locked = self.server.inference_lock.acquire(blocking=False)
            if not locked:
                raise RequestError(503, 'Another image is being processed; retry after it finishes')
            body = self.rfile.read(length)
            if len(body) != length:
                raise RequestError(400, 'Incomplete request body')
            uploads = extract_uploads(body, self.headers.get('Content-Type', ''))
            request_id = uuid4().hex
            if set(uploads) == {'front', 'back'}:
                paths = {}
                for side, (data, suffix) in uploads.items():
                    path = self.server.output / 'uploads' / (request_id + '_' + side + suffix)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    paths[side] = path
                result_path, report = self.server.document_runner(
                    paths['front'], paths['back'], output=self.server.output / 'results',
                    qari=qari == 'true', device=self.server.device, qari_device=self.server.device)
                summary = {side: {name: {'status': field['status'], 'text': field.get('text')}
                                  for name, field in result.get('fields', {}).items()}
                           for side, result in report['sides'].items()}
                # One cross-checked national ID at the top level. It is the same
                # value now written into both sides, so a client that only wants the
                # ID does not have to compare the two itself.
                national_id = report.get('national_id') or {}
                payload = {'request_id': request_id, 'status': report['status'],
                           'result_file': str(result_path), 'fields': summary,
                           'national_id': {'status': national_id.get('status'),
                                           'text': national_id.get('text'),
                                           'confirmed_by': national_id.get('confirmed_by'),
                                           'differing_positions': national_id.get(
                                               'differing_positions'),
                                           'front_text': national_id.get('front_text'),
                                           'back_text': national_id.get('back_text')},
                           'result': report}
                self.send_json(200, payload)
                return
            data, suffix = uploads['image']
            upload = self.server.output / 'uploads' / (request_id + suffix)
            upload.parent.mkdir(parents=True, exist_ok=True)
            upload.write_bytes(data)
            result_path, report = self.server.runner(
                upload, output=self.server.output / 'results', qari=qari == 'true', device=self.server.device,
                qari_device=self.server.device)
            statuses = [entry['status'] for entry in report['fields'].values()]
            status = 'complete' if all(s == 'extracted' for s in statuses) else 'partial'
            report_fields = report['fields']
            summary = {field: {'status': report_fields[field]['status'],
                               'text': report_fields[field].get('text')}
                       for field in ('id', 'name', 'address') if field in report_fields}
            self.send_json(200, {'request_id': request_id, 'status': status,
                                 'result_file': str(result_path), 'fields': summary,
                                 'result': report})
        except RequestError as exc:
            self.send_json(exc.status, {'error': exc.message})
        except TimeoutError:
            self.send_json(408, {'error': 'Image upload timed out'})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            # Release before sending the error so an immediate retry isn't busy.
            if locked:
                self.server.inference_lock.release()
                locked = False
            self.send_json(500, {'error': 'Pipeline failed', 'detail': str(exc)})
        finally:
            if locked:
                self.server.inference_lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=os.environ.get('API_HOST', '0.0.0.0'),
                        help='interface to bind (default: API_HOST or 0.0.0.0)')
    parser.add_argument('--device', choices=('cpu', 'cuda', 'mps', 'auto'), default='cpu',
                        help='inference device; mps is available only in native macOS, not Docker')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--output', type=Path, default=API_OUTPUT)
    parser.add_argument('--fast', action='store_true',
                        help='speed up Qari: use half-resolution crops and more CPU threads')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('port must be between 1 and 65535')
    if args.fast:
        os.environ.setdefault('QARI_MAX_PIXELS', str(128 * 28 * 28))  # half of the default 200704
        os.environ.setdefault('QARI_THREADS', str(os.cpu_count() or 4))
    try:
        device = resolve_device(args.device)
    except RuntimeError as exc:
        parser.exit(1, f'{exc}\n')
    with PipelineServer((args.host, args.port), args.output, device=device) as server:
        display_host = '127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host
        print(f'Front/back ID API: http://{display_host}:{args.port}', flush=True)
        print('POST /predict accepts front and back image uploads. Ctrl+C stops the server.', flush=True)
        print(f"Qari settings: max_pixels={os.environ.get('QARI_MAX_PIXELS', '200704 (default)')}, "
              f"threads={os.environ.get('QARI_THREADS', '4 (default)')}. "
              f"Tune with QARI_MAX_PIXELS/QARI_THREADS env vars.", flush=True)
        print('Warming up models before accepting requests...', flush=True)
        warm_up(server.device)
        print('Ready.', flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()

