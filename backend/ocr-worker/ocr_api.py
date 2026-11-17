"""Async OCR HTTP service: one process, shared models and persistent job threads."""
import argparse
from contextlib import asynccontextmanager, ExitStack
from datetime import datetime
import hmac
import logging
import os
from pathlib import Path
from uuid import UUID

from oracle_config import load_environment

load_environment()

from fastapi import Depends, FastAPI, File, Form, Header, UploadFile
from fastapi.responses import JSONResponse
from inference import MAX_IMAGE_BYTES, MODEL_ROOT
from oracle_logging import capture_console
from ocr_log_retention import add_log_arguments, log_arguments
from ocr_requests import RequestError, RequestStore
from ocr_retention import ArtifactRetention

log = logging.getLogger('ocr-api')


def create_app(*, token, output, concurrency=3, capacity=6, retain_artifacts=True,
               initialize_models=None, infer=None, validate=None, pools_factory=None,
               artifact_retention_hours=168, cleanup_interval=60, min_free_mb=1024):
    if not token:
        raise ValueError('Set OCR_API_TOKEN to a nonempty service credential')
    if not 1 <= concurrency <= 8 or not concurrency <= capacity <= 64:
        raise ValueError('Concurrency must be 1..8; capacity must be concurrency..64')
    retention = ArtifactRetention(output, artifact_retention_hours, cleanup_interval, min_free_mb)
    import inference
    initialize_models = initialize_models or inference.initialize
    infer = infer or inference.run_ocr_paths
    validate = validate or inference.validate_image
    pools_factory = pools_factory or inference.document_thread_pools

    @asynccontextmanager
    async def lifespan(app):
        with ExitStack() as resources:
            log.info('Loading offline OCR models')
            initialize_models()
            resources.enter_context(pools_factory(concurrency))
            store = RequestStore(output, concurrency, capacity, infer, validate,
                                 MAX_IMAGE_BYTES, retain_artifacts, retention=retention)
            resources.callback(store.close)  # Drain jobs before closing stage pools.
            retention.start()
            resources.callback(retention.close)
            app.state.store = store
            log.info('OCR API ready; instance=%s jobConcurrency=%s capacity=%s output=%s',
                     store.instance_id, concurrency, capacity, store.root)
            log.info('Storage policy; acknowledgedRetentionHours=%s cleanupIntervalSeconds=%s minimumFreeMB=%s',
                     artifact_retention_hours, cleanup_interval, min_free_mb)
            try:
                yield
            finally:
                log.info('OCR API stopping')

    app = FastAPI(title='OCR inference service', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(authorization: str | None = Header(default=None)):
        supplied = (authorization or '').encode('utf-8')
        if not hmac.compare_digest(supplied, ('Bearer ' + token).encode('utf-8')):
            raise RequestError(401, 'UNAUTHORIZED')

    @app.exception_handler(RequestError)
    async def request_error(request, exc):
        log.warning('Request rejected code=%s httpStatus=%s', exc.code, exc.status)
        headers = {'Retry-After': '2'} if exc.status == 503 or exc.code == 'REQUEST_UPLOADING' else {}
        if exc.status == 401:
            headers['WWW-Authenticate'] = 'Bearer'
        return JSONResponse({'errorCode': exc.code,
                             'serviceInstanceId': app.state.store.instance_id},
                            status_code=exc.status, headers=headers)

    @app.get('/health/live')
    def live():
        return {'alive': True}

    @app.get('/health/ready', dependencies=[Depends(authorize)])
    def ready():
        data = app.state.store.health()
        return JSONResponse(data, status_code=200 if data['ready'] else 503)

    @app.post('/ocr', dependencies=[Depends(authorize)])
    def submit(request_id: UUID = Form(), job_id: int = Form(gt=0),
               front: UploadFile = File(), back: UploadFile = File()):
        data = app.state.store.submit(str(request_id), job_id, front.file, back.file)
        return JSONResponse(data, status_code=202, headers={
            'Location': '/ocr/requests/' + data['requestId'], 'Retry-After': '1'})

    @app.get('/ocr/requests/{request_id}', dependencies=[Depends(authorize)])
    def status(request_id: UUID):
        return app.state.store.get(str(request_id))

    @app.post('/ocr/requests/{request_id}/ack', dependencies=[Depends(authorize)])
    def acknowledge(request_id: UUID):
        return app.state.store.acknowledge(str(request_id))

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--job-concurrency', type=int, default=3)
    parser.add_argument('--capacity', type=int, default=6)
    parser.add_argument('--output', type=Path, default=MODEL_ROOT / 'outputs/ocr_api')
    parser.add_argument('--delete-acknowledged-artifacts', action='store_true')
    parser.add_argument('--artifact-retention-hours', type=float, default=168,
                        help='Delete acknowledged artifacts after this age; 0 disables age cleanup')
    parser.add_argument('--cleanup-interval', type=float, default=60)
    parser.add_argument('--min-free-mb', type=int, default=1024)
    add_log_arguments(parser)
    args = parser.parse_args()
    token = os.getenv('OCR_API_TOKEN', '')
    if not token:
        parser.error('Set OCR_API_TOKEN in the environment or project-root .env')
    if not 1 <= args.job_concurrency <= 8 or not args.job_concurrency <= args.capacity <= 64:
        parser.error('Concurrency must be 1..8; capacity must be concurrency..64')
    if not 1 <= args.port <= 65535:
        parser.error('Port must be 1..65535')
    try:
        logging_options = log_arguments(args)
    except ValueError as exc:
        parser.error(str(exc))
    pid = os.getpid()
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    with capture_console('api', pid, run_id, prefix='ocr_api', **logging_options) as logfile:
        log.info('API launcher started; launcher PID=%s; log=%s', pid, logfile)
        try:
            app = create_app(token=token, output=args.output,
                             concurrency=args.job_concurrency, capacity=args.capacity,
                             retain_artifacts=not args.delete_acknowledged_artifacts,
                             artifact_retention_hours=args.artifact_retention_hours,
                             cleanup_interval=args.cleanup_interval, min_free_mb=args.min_free_mb)
            import uvicorn
            # Explicitly one process: additional API workers would duplicate models.
            # Keep our console/file handlers and avoid logging request URLs/bodies.
            uvicorn.run(app, host=args.host, port=args.port, workers=1,
                        log_config=None, access_log=False)
        finally:
            log.info('API launcher stopped; launcher PID=%s', pid)


if __name__ == '__main__':
    main()
