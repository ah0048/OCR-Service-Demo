"""Exercise a running real pipeline using one front/back pair, without printing ID data.

Usage: python backend/scripts/verify-integration.py --front try1/images/CARD_1.jpg --back try1/images/CARD_2.jpg
"""
import argparse
import hashlib
import json
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--api', default='http://localhost:5125')
parser.add_argument('--front', required=True, type=Path)
parser.add_argument('--back', required=True, type=Path)
args = parser.parse_args()
opener = build_opener(ProxyHandler({}))

def request(path, body=None, content_type=None):
    headers = {'Content-Type': content_type} if content_type else {}
    return opener.open(Request(args.api + path, data=body, headers=headers), timeout=20)

def upload(path, fields, files):
    boundary = uuid.uuid4().hex
    chunks = []
    for key, value in fields:
        chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                   value.encode(), b'\r\n']
    for side, filename, content in files:
        chunks += [f'--{boundary}\r\nContent-Disposition: form-data; name="{side}"; filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'.encode(),
                   content, b'\r\n']
    chunks += [f'--{boundary}--\r\n'.encode()]
    with request(path, b''.join(chunks), 'multipart/form-data; boundary=' + boundary) as response:
        assert response.status == 202
        return json.load(response)

front, back = args.front.read_bytes(), args.back.read_bytes()
t0 = time.monotonic()
submitted = upload('/api/jobs/upload', [('clientReference', 'REAL-INTEGRATION')],
                   [('front', 'front.jpg', front), ('back', 'back.jpg', back)])
print('Single upload accepted in', round(time.monotonic() - t0, 3), 'seconds', flush=True)
job_id = submitted['jobId']
# Invalid requests are rejected before jobs are persisted.
try:
    upload('/api/jobs/upload', [('clientReference', 'INVALID')], [('front', 'front.jpg', front)])
    raise AssertionError('Missing back accepted')
except HTTPError as error:
    assert error.code == 400
# Signature-valid but corrupt files verify that poison images terminate with an
# Unreadable result rather than looping forever. They do not run model inference.
corrupt = b'\x89PNG\r\n\x1a\n' + bytes(32)
batch = upload('/api/jobs/batch/upload', [('batchName', 'CORRUPT-INTEGRATION'),
               ('clientReference', 'CORRUPT-1'), ('clientReference', 'CORRUPT-2')],
               [('front', '1_1.png', corrupt), ('back', '1_2.png', corrupt),
                ('front', '2_1.png', corrupt), ('back', '2_2.png', corrupt)])
with request('/api/jobs?batchId=' + batch['batchId']) as response:
    batch_jobs = json.load(response)['items']
ids = [job_id] + [j['jobId'] for j in batch_jobs]
deadline = time.monotonic() + 600
pending = set(ids)
completed = {}
while pending and time.monotonic() < deadline:
    for id in list(pending):
        with request('/api/jobs/' + id) as response:
            job = json.load(response)
        if job['status'] == 'Completed':
            completed[id] = job
            pending.remove(id)
            print('Completed', id, job['outcome'], 'fields:', sorted((job['ocr'] or {}).get('fields') or {}), flush=True)
    if pending:
        print('Waiting for', len(pending), 'job(s); elapsed', round(time.monotonic() - t0), 'seconds', flush=True)
        time.sleep(10)
assert not pending, 'Jobs did not complete'
real = completed[job_id]
assert real['ocr']['modelVersion'].startswith('egyptian-id-')
assert 0 <= real['ocr']['confidence'] <= 1
assert real['ocr']['details']['confidenceKind'] == 'heuristic'
assert real['ocr']['details'] and real['ocr']['fields']
assert real['outcome'] != 'TechnicalError'
assert real['ocrWorkerId'] and real['businessWorkerId']
for id in ids[1:]:
    assert completed[id]['outcome'] == 'OcrFailed'
    assert completed[id]['ocr']['details']['errorCode'] == 'INVALID_IMAGE_PAIR'
for side, data in [('front', front), ('back', back)]:
    with request('/api/jobs/' + job_id + '/images/' + side) as response:
        assert response.headers['Cache-Control'] == 'no-store'
        assert hashlib.sha256(response.read()).digest() == hashlib.sha256(data).digest()
with request('/api/results/stream?backlog=200') as response:
    found = False
    for _ in range(800):
        line = response.readline()
        if line.startswith(b'data: '):
            result = json.loads(line[6:])
            if result['jobId'] == job_id:
                assert result['ocr']['details'] == real['ocr']['details']
                found = True
                break
    assert found, 'Result not delivered through SSE'
with request('/api/dashboard/snapshot') as response:
    snapshot = json.load(response)
    summary = next(b for b in snapshot['batches'] if b['id'] == batch['batchId'])
    assert summary['completed'] == 2 and summary['ocrFailed'] == 2
print('PASS: real extraction, business persistence, batch processing, invalid input, image previews and SSE.', flush=True)
