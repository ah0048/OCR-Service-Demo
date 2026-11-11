"""Shutdown regression tests with real spawn processes, without DB/model loading."""
import multiprocessing
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_worker as worker


def idle_process(stop, ready):
    request = worker.install_shutdown_handlers()
    ready.set()
    worker.wait_for_stop(stop, request, 30)


class ShutdownTests(unittest.TestCase):
    def test_interrupted_idle_sleep_exits_cleanly_on_shutdown(self):
        stop = threading.Event()
        request = worker.ShutdownRequest()

        def interrupt(_):
            request.handle()
            raise InterruptedError(4, 'Interrupted function call')

        with patch.object(worker.time, 'sleep', side_effect=interrupt) as sleep:
            worker.wait_for_stop(stop, request, 30)
        sleep.assert_called_once()
        self.assertTrue(request.requested)

    def test_unrelated_interruption_retries_without_forcing_shutdown(self):
        stop = threading.Event()
        request = worker.ShutdownRequest()

        with patch.object(worker.time, 'sleep', side_effect=[InterruptedError(4, 'interrupted'), None]) as sleep:
            with patch.object(worker.time, 'monotonic', side_effect=[0, 0, 0, 31]):
                worker.wait_for_stop(stop, request, 30)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(request.requested)
        self.assertFalse(stop.is_set())

    def test_idle_polling_does_not_use_shared_condition_wait(self):
        stop = Mock()
        stop.is_set.side_effect = [False, True]
        with patch.object(worker.time, 'sleep'):
            worker.wait_for_stop(stop, worker.ShutdownRequest(), 30)
        stop.wait.assert_not_called()

    def test_interrupted_process_joins_are_not_reported_as_worker_failures(self):
        process = Mock()
        process.join.side_effect = [InterruptedError(4, 'interrupted'), None]
        worker.join_process(process)
        self.assertEqual(process.join.call_count, 2)
        process.join.reset_mock(side_effect=True)
        process.join.side_effect = InterruptedError(4, 'interrupted')
        worker.join_process(process, timeout=.2)
        process.join.assert_called_once_with(timeout=.2)

    def test_handler_only_sets_local_flag(self):
        request = worker.ShutdownRequest()
        with patch.object(worker.log, 'info') as log:
            request.handle(None, None)
        self.assertTrue(request.requested)
        log.assert_not_called()

    def test_idle_wait_exits_on_local_signal_without_setting_shared_event(self):
        stop = threading.Event()
        request = worker.ShutdownRequest()
        started = time.monotonic()
        timer = threading.Timer(.1, request.handle)
        timer.start()
        try:
            worker.wait_for_stop(stop, request, 30)
        finally:
            timer.join()
        self.assertLess(time.monotonic() - started, 2)
        self.assertFalse(stop.is_set())

    def test_two_spawn_workers_exit_after_supervisor_shutdown_request(self):
        ctx = multiprocessing.get_context('spawn')
        stop = ctx.Event()
        ready = [ctx.Event(), ctx.Event()]
        request = worker.ShutdownRequest()
        processes = [ctx.Process(target=idle_process, args=(stop, event)) for event in ready]
        errors = []

        def interrupt_when_ready():
            if not all(event.wait(15) for event in ready):
                errors.append('Workers did not become ready')
            request.handle()

        interrupter = threading.Thread(target=interrupt_when_ready)
        # A watchdog prevents a regression from hanging the test suite.
        watchdog = threading.Timer(25, lambda: [p.kill() for p in processes if p.is_alive()])
        interrupter.start()
        watchdog.start()
        try:
            worker.supervise_workers(processes, stop, request)
        finally:
            watchdog.cancel()
            interrupter.join(timeout=2)
            for process in processes:
                if process.is_alive():
                    process.kill()
                if process.pid is not None:
                    process.join(timeout=2)
        self.assertEqual(errors, [])
        self.assertTrue(stop.is_set())
        self.assertEqual([p.exitcode for p in processes], [0, 0])


if __name__ == '__main__':
    unittest.main()
