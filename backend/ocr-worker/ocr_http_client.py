"""Synchronous HTTP transport; the coordinator polls several requests per loop."""
from urllib.parse import urlsplit
from uuid import UUID

import httpx


class ApiUnavailable(Exception):
    """Temporary transport/capacity failure; do not mark the Oracle job failed."""


class ApiProtocolError(Exception):
    """Authentication, conflicting IDs or an invalid response requires attention."""


class ApiMissing(Exception):
    def __init__(self, instance_id):
        self.instance_id = instance_id


class ApiInvalidImage(Exception):
    def __init__(self, code):
        self.code = code


def uuid_text(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ApiProtocolError('INVALID_API_IDENTIFIER') from None


class OcrClient:
    def __init__(self, url, token, timeout=30, transport=None):
        parts = urlsplit(url)
        if (parts.scheme not in ('http', 'https') or not parts.hostname or
                parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError('API URL must be HTTP(S), without credentials, query or fragment')
        if not token:
            raise ValueError('Set OCR_API_TOKEN')
        self.client = httpx.Client(base_url=url.rstrip('/') + '/',
                                   headers={'Authorization': 'Bearer ' + token},
                                   timeout=timeout, transport=transport, trust_env=False,
                                   follow_redirects=False)

    def close(self):
        self.client.close()

    def _send(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise ApiUnavailable('API_TRANSPORT_ERROR') from None
        if response.status_code in (408, 429, 500, 502, 503, 504):
            raise ApiUnavailable('API_BUSY_OR_UNAVAILABLE')
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError()
        except ValueError:
            raise ApiProtocolError('INVALID_API_JSON') from None
        if response.status_code == 404:
            raise ApiMissing(uuid_text(data.get('serviceInstanceId')))
        if response.status_code == 409 and data.get('errorCode') == 'REQUEST_UPLOADING':
            raise ApiUnavailable('UPLOAD_IN_PROGRESS')
        if response.status_code in (413, 422):
            codes = {'IMAGE_TOO_LARGE', 'EMPTY_IMAGE', 'INVALID_IMAGE'}
            if data.get('errorCode') in codes:
                raise ApiInvalidImage(data['errorCode'])
        if response.status_code not in (200, 202):
            raise ApiProtocolError('API_HTTP_' + str(response.status_code))
        return data

    def ready(self):
        data = self._send('GET', 'health/ready')
        if data.get('ready') is not True:
            raise ApiUnavailable('API_NOT_READY')
        uuid_text(data.get('serviceInstanceId'))
        return data

    def _request(self, method, path, request_id, job_id, **kwargs):
        data = self._send(method, path, **kwargs)
        if (uuid_text(data.get('requestId')) != request_id or
                type(data.get('jobId')) is not int or data['jobId'] != job_id or
                data.get('status') not in ('UPLOADING', 'QUEUED', 'RUNNING',
                                           'COMPLETED', 'FAILED', 'ACKNOWLEDGED')):
            raise ApiProtocolError('API_REQUEST_MISMATCH')
        uuid_text(data.get('serviceInstanceId'))
        return data

    def get(self, request_id, job_id):
        return self._request('GET', 'ocr/requests/' + request_id, request_id, job_id)

    def submit(self, request_id, job_id, front, back):
        with front.open('rb') as f, back.open('rb') as b:
            return self._request('POST', 'ocr', request_id, job_id,
                                 data={'request_id': request_id, 'job_id': str(job_id)},
                                 files={'front': (front.name, f, 'application/octet-stream'),
                                        'back': (back.name, b, 'application/octet-stream')})

    def acknowledge(self, request_id, job_id):
        data = self._request('POST', 'ocr/requests/' + request_id + '/ack', request_id, job_id)
        if data['status'] != 'ACKNOWLEDGED':
            raise ApiProtocolError('INVALID_ACK_RESPONSE')
        return data
