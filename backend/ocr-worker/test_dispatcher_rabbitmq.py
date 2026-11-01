"""Opt-in real-broker check using exclusive test queues, never service queues."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pika
import worker


@unittest.skipUnless(os.getenv('OCR_RUN_BROKER_TESTS') == '1', 'Set OCR_RUN_BROKER_TESTS=1 for isolated RabbitMQ test')
class RabbitDispatcherTests(unittest.TestCase):
    def test_real_prefetch_confirmed_results_and_manual_acknowledgments(self):
        main_thread = threading.get_ident()
        prefix = 'ocr-dispatch-test-' + uuid4().hex
        queues = [prefix + suffix for suffix in ('.jobs', '.results', '.events')]
        job_ids = [str(uuid4()) for _ in range(3)]
        io_threads = []
        connection = worker.connect()
        try:
            channel = connection.channel()
            channel.confirm_delivery()
            for queue in queues:
                channel.queue_declare(queue=queue, exclusive=True, auto_delete=True)
            for job_id in job_ids:
                channel.basic_publish(exchange='', routing_key=queues[0], mandatory=True,
                                      body=json.dumps({'jobId': job_id, 'correlationId': str(uuid4()),
                                                       'imageReference': 'test-pair'}),
                                      properties=pika.BasicProperties(content_type='application/json'))
            original_publish = channel.basic_publish
            original_ack = channel.basic_ack

            def publish(*args, **kwargs):
                io_threads.append(threading.get_ident())
                return original_publish(*args, **kwargs)

            def acknowledge(*args, **kwargs):
                io_threads.append(threading.get_ident())
                return original_ack(*args, **kwargs)

            def infer(job):
                self.assertNotEqual(threading.get_ident(), main_thread)
                time.sleep(.02)
                return worker.OcrOutput('Success', fields={'test': 'fixture'})

            deadline = time.monotonic() + 10
            with patch.object(worker, 'JOBS_QUEUE', queues[0]), \
                 patch.object(worker, 'RESULTS_QUEUE', queues[1]), \
                 patch.object(worker, 'EVENTS_QUEUE', queues[2]), \
                 patch.object(worker, 'run_ocr', side_effect=infer), \
                 patch.object(channel, 'basic_publish', side_effect=publish), \
                 patch.object(channel, 'basic_ack', side_effect=acknowledge) as ack, \
                 ThreadPoolExecutor(max_workers=2) as executor:
                worker.consume_jobs(channel, executor, 2,
                                    lambda: ack.call_count == 3 or time.monotonic() >= deadline)
                self.assertEqual(ack.call_count, 3)
            results = []
            for _ in range(3):
                method, properties, body = channel.basic_get(queues[1], auto_ack=True)
                self.assertIsNotNone(method)
                results.append(json.loads(body))
            self.assertEqual({r['jobId'] for r in results}, set(job_ids))
            self.assertTrue(all(r['ocrOutcome'] == 'Success' for r in results))
            self.assertEqual(channel.queue_declare(queue=queues[0], passive=True).method.message_count, 0)
            self.assertEqual(set(io_threads), {main_thread})
            channel.cancel()
        finally:
            if connection.is_open:
                connection.close()  # Removes all exclusive test queues.


if __name__ == '__main__':
    unittest.main()
