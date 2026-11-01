"""Bounded job dispatch, connection ownership and shutdown without a broker."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
import worker


def delivery(number):
    job = {'jobId': str(UUID(int=number)), 'correlationId': str(UUID(int=100)),
           'imageReference': 'pair'}
    return SimpleNamespace(delivery_tag=number), None, json.dumps(job).encode()


def channel_for(numbers):
    channel = Mock()
    channel.consume.return_value = iter(delivery(n) for n in numbers)
    channel.connection.process_data_events.side_effect = lambda **kwargs: time.sleep(.001)
    return channel


class DispatcherTests(unittest.TestCase):
    def test_concurrency_config_defaults_and_limits(self):
        with patch.dict(os.environ):
            os.environ.pop('OCR_JOB_CONCURRENCY', None)
            self.assertEqual(worker.job_concurrency(), 1)
            for value in ('1', '2', '8'):
                os.environ['OCR_JOB_CONCURRENCY'] = value
                self.assertEqual(worker.job_concurrency(), int(value))
            for value in ('0', '9', '-1', 'bad', '1.5'):
                os.environ['OCR_JOB_CONCURRENCY'] = value
                with self.assertRaisesRegex(ValueError, 'OCR_JOB_CONCURRENCY'):
                    worker.job_concurrency()

    def test_two_jobs_overlap_bound_is_enforced_and_completions_can_finish_out_of_order(self):
        channel = channel_for([])
        main_thread = threading.get_ident()
        io_threads = []
        inference_threads = set()
        unacked = peak = 0
        acked = []
        confirmed = set()
        second_started = threading.Event()
        third_started = threading.Event()

        def deliveries():
            nonlocal unacked, peak
            for number in (1, 2, 3):
                io_threads.append(threading.get_ident())
                unacked += 1
                peak = max(peak, unacked)
                yield delivery(number)

        def infer(job):
            inference_threads.add(threading.get_ident())
            number = UUID(job['jobId']).int
            if number == 1:
                self.assertTrue(second_started.wait(5))
                # First job cannot finish until the dispatcher has acked job 2
                # and accepted job 3. This catches head-of-line blocking.
                self.assertTrue(third_started.wait(5))
            elif number == 2:
                second_started.set()
            else:
                third_started.set()
            return worker.OcrOutput('Success')

        def publish(**kwargs):
            io_threads.append(threading.get_ident())
            if kwargs['routing_key'] == worker.RESULTS_QUEUE:
                confirmed.add(UUID(json.loads(kwargs['body'])['jobId']).int)

        def acknowledge(tag):
            nonlocal unacked
            io_threads.append(threading.get_ident())
            self.assertIn(tag, confirmed)
            unacked -= 1
            acked.append(tag)

        channel.consume.return_value = deliveries()
        channel.basic_publish.side_effect = publish
        channel.basic_ack.side_effect = acknowledge
        with patch.object(worker, 'run_ocr', side_effect=infer), ThreadPoolExecutor(max_workers=2) as executor:
            worker.consume_jobs(channel, executor, 2, lambda: False)
        self.assertEqual(peak, 2)
        self.assertEqual(unacked, 0)
        self.assertEqual(acked[0], 2)
        self.assertEqual(set(acked), {1, 2, 3})
        self.assertEqual(set(io_threads), {main_thread})
        self.assertNotIn(main_thread, inference_threads)
        channel.basic_qos.assert_called_once_with(prefetch_count=2)

    def test_shutdown_drains_accepted_jobs_without_accepting_another(self):
        channel = channel_for([1, 2, 3])
        with ThreadPoolExecutor(max_workers=2) as pool:
            executor = SimpleNamespace(submit=Mock(wraps=pool.submit))
            with patch.object(worker, 'run_ocr', side_effect=lambda job: (time.sleep(.02), worker.OcrOutput('Success'))[1]):
                worker.consume_jobs(channel, executor, 2, lambda: executor.submit.call_count >= 2)
        self.assertEqual(executor.submit.call_count, 2)
        self.assertEqual(channel.basic_ack.call_count, 2)
        self.assertEqual({c.args[0] for c in channel.basic_ack.call_args_list}, {1, 2})

    def test_connection_or_publication_failure_abandons_old_jobs_before_reusing_pool(self):
        for failure in ('connection', 'publication'):
            with self.subTest(failure=failure):
                channel = channel_for([1, 2])
                second_started = threading.Event()
                closed = threading.Event()
                completed = []
                channel.connection.close.side_effect = closed.set

                def infer(job):
                    number = UUID(job['jobId']).int
                    if number == 1:
                        self.assertTrue(second_started.wait(5))
                        if failure == 'connection':
                            self.assertTrue(closed.wait(5))
                    else:
                        second_started.set()
                        self.assertTrue(closed.wait(5))
                    completed.append(number)
                    return worker.OcrOutput('Success')

                def publish(**kwargs):
                    if kwargs['routing_key'] == worker.RESULTS_QUEUE:
                        raise RuntimeError('publisher confirmation failed')

                if failure == 'connection':
                    channel.connection.process_data_events.side_effect = OSError('connection lost')
                else:
                    channel.basic_publish.side_effect = publish
                with ThreadPoolExecutor(max_workers=2) as executor:
                    with patch.object(worker, 'run_ocr', side_effect=infer), self.assertRaises((OSError, RuntimeError)):
                        worker.consume_jobs(channel, executor, 2, lambda: False)
                    self.assertEqual(set(completed), {1, 2})
                    channel.basic_ack.assert_not_called()
                    channel.connection.close.assert_called_once()
                    # A new connection sees only its own deliveries/results.
                    replacement = channel_for([3])
                    with patch.object(worker, 'run_ocr', return_value=worker.OcrOutput('Success')):
                        worker.consume_jobs(replacement, executor, 2, lambda: False)
                    replacement.basic_ack.assert_called_once_with(3)
                    results = [json.loads(c.kwargs['body']) for c in replacement.basic_publish.call_args_list
                               if c.kwargs['routing_key'] == worker.RESULTS_QUEUE]
                    self.assertEqual([r['jobId'] for r in results], [str(UUID(int=3))])


if __name__ == '__main__':
    unittest.main()
