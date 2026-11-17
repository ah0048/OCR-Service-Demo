"""Bounded Oracle reconnect behavior; never contacts a database."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import oracle_coordinator as coordinator
from oracle_recovery import (OracleRecoveryExhausted, RetryBudget,
                              safe_error_code, transient_database_error)
from oracle_worker import ShutdownRequest


class FakeOracleError(Exception):
    pass


def database_error(code=3113, full_code=None, recoverable=False):
    return FakeOracleError(SimpleNamespace(code=code, full_code=full_code,
                                           isrecoverable=recoverable, message='PRIVATE_DRIVER_MESSAGE'))


DRIVER = SimpleNamespace(Error=FakeOracleError)


def arguments(directory, **overrides):
    values = dict(check_connection=False, db_retries=3, db_retry_seconds=2,
                  db_retry_max_seconds=30, max_outstanding=3,
                  confidence_threshold=.5, poll_seconds=1, retry_seconds=5,
                  drain_seconds=60, pending_dir=Path(directory) / 'pending')
    values.update(overrides)
    return SimpleNamespace(**values)


class RecoveryPolicyTests(unittest.TestCase):
    def test_only_recognised_driver_connection_errors_are_retried(self):
        with patch.dict(sys.modules, {'oracledb': DRIVER}):
            for code in (28, 1012, 1033, 1034, 3113, 3114, 3135, 12170, 12541):
                self.assertTrue(transient_database_error(database_error(code)))
            for code in (1, 904, 942, 1017, 1031, 1400, 1438, 28000, 28001):
                self.assertFalse(transient_database_error(database_error(code)))
            self.assertTrue(transient_database_error(database_error(0, 'DPI-1080')))
            self.assertTrue(transient_database_error(database_error(0, 'DPI-1010')))
            self.assertTrue(transient_database_error(database_error(0, 'DPY-4011')))
            self.assertTrue(transient_database_error(database_error(0, recoverable=True)))
            self.assertFalse(transient_database_error(ValueError('not an Oracle error')))

    def test_error_logs_use_codes_without_driver_message_contents(self):
        self.assertEqual(safe_error_code(database_error(3113)), 'ORA-03113')
        self.assertEqual(safe_error_code(database_error(0, 'DPI-1080')), 'DPI-1080')
        self.assertEqual(safe_error_code(database_error(0, 'PRIVATE_DRIVER_MESSAGE')), 'UNKNOWN_DATABASE_ERROR')

    def test_budget_is_exponential_bounded_and_can_be_reset_after_success(self):
        budget = RetryBudget(4, 2, 5)
        self.assertEqual([budget.next_delay() for _ in range(4)], [2, 4, 5, 5])
        with self.assertRaises(OracleRecoveryExhausted):
            budget.next_delay()
        budget.recovered()
        self.assertEqual(budget.next_delay(), 2)


class RecoveryLoopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.shutdown = ShutdownRequest()
        self.api = Mock()
        self.driver_patch = patch.dict(sys.modules, {'oracledb': DRIVER})
        self.driver_patch.start()
        self.addCleanup(self.driver_patch.stop)

    def test_initial_outage_uses_three_retries_then_stops_without_claiming(self):
        args = arguments(self.temp.name)
        with patch.object(coordinator, 'connect', side_effect=database_error(12541)) as connect, \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop') as wait, \
             patch.object(coordinator, 'Coordinator') as factory, \
             self.assertLogs('oracle-coordinator') as logs:
            with self.assertRaises(OracleRecoveryExhausted):
                coordinator.run_database(args, self.api)
        self.assertEqual(connect.call_count, 4)
        self.assertEqual([call.args[2] for call in wait.call_args_list], [2, 4, 8])
        factory.assert_not_called()
        self.api.ready.assert_not_called()
        self.assertNotIn('PRIVATE_DRIVER_MESSAGE', '\n'.join(logs.output))
        self.assertIn('budget exhausted', '\n'.join(logs.output))

    def test_permanent_authentication_error_is_not_retried(self):
        with patch.object(coordinator, 'connect', side_effect=database_error(1017)) as connect, \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop') as wait:
            with self.assertRaises(FakeOracleError):
                coordinator.run_database(arguments(self.temp.name), self.api)
        connect.assert_called_once()
        wait.assert_not_called()

    def test_readonly_schema_check_reconnects_without_constructing_a_coordinator(self):
        conn = MagicMock(version='11.2.0.4')
        with patch.object(coordinator, 'connect', side_effect=[database_error(), (conn, DRIVER)]) as connect, \
             patch.object(coordinator, 'load_column_storage', return_value={}), \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop'), \
             patch.object(coordinator, 'Coordinator') as factory:
            coordinator.run_database(arguments(self.temp.name, check_connection=True), None)
        self.assertEqual(connect.call_count, 2)
        factory.assert_not_called()
        conn.close.assert_called_once()

    def test_successful_logins_do_not_reset_budget_when_work_keeps_failing(self):
        connections = [MagicMock() for _ in range(3)]
        workers = [Mock(jobs={}, cycle=Mock(side_effect=database_error())) for _ in connections]
        with patch.object(coordinator, 'connect', side_effect=[(c, DRIVER) for c in connections]), \
             patch.object(coordinator, 'load_column_storage', return_value={}), \
             patch.object(coordinator, 'Coordinator', side_effect=workers), \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop') as wait:
            with self.assertRaises(OracleRecoveryExhausted):
                coordinator.run_database(arguments(self.temp.name, db_retries=2), self.api)
        self.assertEqual(wait.call_count, 2)
        for worker, conn in zip(workers, connections):
            worker.restore.assert_called_once()
            conn.close.assert_called_once()

    def test_completed_cycle_resets_budget_for_a_later_independent_outage(self):
        connections = [MagicMock() for _ in range(3)]
        last = Mock(jobs={})
        last.cycle.side_effect = lambda **_: self.shutdown.handle()
        workers = [Mock(jobs={}, cycle=Mock(side_effect=database_error())),
                   Mock(jobs={}, cycle=Mock(side_effect=[None, database_error()])), last]
        with patch.object(coordinator, 'connect', side_effect=[(c, DRIVER) for c in connections]), \
             patch.object(coordinator, 'load_column_storage', return_value={}), \
             patch.object(coordinator, 'Coordinator', side_effect=workers), \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop') as wait:
            coordinator.run_database(arguments(self.temp.name, db_retries=1), self.api)
        self.assertEqual([c.args[2] for c in wait.call_args_list].count(2), 2)
        for worker in workers:
            worker.restore.assert_called_once()

    def test_shutdown_during_backoff_does_not_start_another_connection(self):
        with patch.object(coordinator, 'connect', side_effect=database_error()) as connect, \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop', side_effect=lambda *_: self.shutdown.handle()):
            coordinator.run_database(arguments(self.temp.name), self.api)
        connect.assert_called_once()

    def test_failed_close_does_not_hide_permanent_schema_error(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = database_error(904)
        conn.close.side_effect = database_error(3114)
        with patch.object(coordinator, 'connect', return_value=(conn, DRIVER)), \
             patch.object(coordinator, 'install_shutdown_handlers', return_value=self.shutdown), \
             patch.object(coordinator, 'wait_for_stop') as wait:
            with self.assertRaises(FakeOracleError) as error:
                coordinator.run_database(arguments(self.temp.name), self.api)
        self.assertEqual(error.exception.args[0].code, 904)
        wait.assert_not_called()


if __name__ == '__main__':
    unittest.main()
