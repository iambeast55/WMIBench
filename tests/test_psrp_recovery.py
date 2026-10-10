import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pypsrp.powershell import PSInvocationState
from remoteops.core.models import Host, SessionState
from remoteops.remoting.session import PsrpSession


class PsrpRecoveryTests(unittest.TestCase):
    def session(self):
        session = PsrpSession(Host("fixture", "example"), None)
        session._pool = Mock()
        session.state = SessionState.CONNECTED
        return session

    def pipeline(self, state=PSInvocationState.COMPLETED):
        pipeline = Mock()
        pipeline.state = state
        pipeline.output = []
        pipeline.streams = SimpleNamespace(error=[])
        return pipeline

    def test_confirmed_timeout_keeps_pool_and_does_not_replay(self):
        session = self.session()
        pool = session._pool
        pipeline = self.pipeline(PSInvocationState.RUNNING)
        pipeline.stop.side_effect = lambda: setattr(pipeline, "state", PSInvocationState.STOPPED)
        with patch("pypsrp.powershell.PowerShell", return_value=pipeline) as factory:
            result = session.run("action", timeout_seconds=-1)
            self.assertEqual(result.exit_code, 1)
            self.assertIn("side effects", result.stderr)
            pipeline.begin_invoke.assert_called_once()
            pipeline.stop.assert_called_once()
            pipeline.close.assert_called_once()
            factory.assert_called_once_with(pool)
        self.assertIs(session._pool, pool)
        self.assertEqual(session.state, SessionState.CONNECTED)
        with patch("pypsrp.powershell.PowerShell", return_value=self.pipeline()):
            self.assertEqual(session.run("next command").exit_code, 0)

    def test_unconfirmed_stop_or_transport_failure_still_invalidates(self):
        for failure in ("stop-state", "stop-error", "poll-error", "cleanup-error"):
            with self.subTest(failure=failure):
                session = self.session()
                pipeline = self.pipeline(PSInvocationState.RUNNING)
                timeout = -1
                if failure == "stop-error":
                    pipeline.stop.side_effect = RuntimeError("lost reply")
                elif failure == "poll-error":
                    timeout = 10
                    pipeline.poll_invoke.side_effect = RuntimeError("lost transport")
                elif failure == "cleanup-error":
                    timeout = 10
                    pipeline.state = PSInvocationState.COMPLETED
                    pipeline.close.side_effect = RuntimeError("cleanup failed")
                with patch("pypsrp.powershell.PowerShell", return_value=pipeline):
                    result = session.run("action", timeout)
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(session.state, SessionState.ERROR)
                pipeline.begin_invoke.assert_called_once()

    def test_local_output_error_and_pipeline_error_do_not_break_pool(self):
        session = self.session()
        pipeline = self.pipeline()
        with patch("pypsrp.powershell.PowerShell", return_value=pipeline), \
                patch("remoteops.remoting.session.output_records", side_effect=TypeError("local formatter")):
            self.assertEqual(session.run("query").exit_code, 1)
        self.assertEqual(session.state, SessionState.CONNECTED)
        pipeline = self.pipeline(PSInvocationState.FAILED)
        pipeline.streams.error = ["Access denied"]
        with patch("pypsrp.powershell.PowerShell", return_value=pipeline):
            self.assertEqual(session.run("query").exit_code, 1)
            session.health_check()
        self.assertEqual(session.state, SessionState.CONNECTED)
