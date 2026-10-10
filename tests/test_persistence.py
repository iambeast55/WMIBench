import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from remoteops.commands.templates import CommandValidationError
from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, ConnectionMode, CredentialProfile, Host, SessionState
from remoteops.remoting.session import FakeSession, PsrpSession, RemoteSessionError
from remoteops.remoting.session_manager import SessionManager


class MemorySession(FakeSession):
    def __init__(self, host, profile):
        super().__init__(host, profile)
        self.threads = []
        self.checks = 0

    def connect(self, credential=None):
        self.threads.append(threading.get_ident())
        super().connect(credential)

    def run(self, command, timeout_seconds=120):
        self.threads.append(threading.get_ident())
        return CommandResult(self.host.id, self.host.name, command, stdout="result", records=[{"Count": 1}], exit_code=0)

    def health_check(self):
        self.threads.append(threading.get_ident())
        self.checks += 1


class PersistenceTests(unittest.TestCase):
    def test_host_operations_share_thread_and_idle_checks_reuse_session(self):
        manager = SessionManager(session_factory=MemorySession)
        self.addCleanup(manager.shutdown)
        host = Host("test", "example", keepalive_seconds=30)
        session = manager.connect(host)
        manager.run_command(host, "sysinfo")
        manager.schedule_keepalives(now=time.monotonic() + 31)
        manager._submit(host.id, lambda: None).result()
        self.assertEqual(session.checks, 1)
        self.assertEqual(len(set(session.threads)), 1)
        self.assertNotEqual(session.threads[0], threading.get_ident())
        self.assertIs(manager.connect(host), session)
        manager.disconnect(host)
        manager.schedule_keepalives(now=time.monotonic() + 100)
        self.assertEqual(session.checks, 1)

    def test_no_keepalive_queued_while_command_is_running(self):
        manager = SessionManager(session_factory=MemorySession)
        self.addCleanup(manager.shutdown)
        host = Host("test", "example", keepalive_seconds=1)
        session = manager.connect(host)
        gate = threading.Event()
        future = manager._submit(host.id, lambda: gate.wait(3))
        try:
            manager.schedule_keepalives(now=time.monotonic() + 100)
        finally:
            gate.set()
        future.result()
        self.assertEqual(session.checks, 0)

    def test_lost_session_requires_explicit_reconnect(self):
        manager = SessionManager(session_factory=MemorySession)
        self.addCleanup(manager.shutdown)
        host = Host("test", "example", keepalive_seconds=1)
        session = manager.connect(host)
        session.health_check = lambda: (_ for _ in ()).throw(RuntimeError("gone"))
        manager.schedule_keepalives(now=time.monotonic() + 2)
        manager._submit(host.id, lambda: None).result()
        self.assertEqual(manager.states()[(host.id, None)], SessionState.ERROR)
        result = manager.run_command(host, "sysinfo")
        self.assertIn("Reconnect", result.stderr)
        self.assertIsNot(manager.connect(host), session)

    def test_rejected_input_and_help_never_create_session(self):
        manager = SessionManager(session_factory=MemorySession)
        self.addCleanup(manager.shutdown)
        host = Host("test", "example")
        with self.assertRaises(CommandValidationError):
            manager.run_command_with_credential(host, "Get-Process")
        result = manager.run_command_with_credential(host, "events -h")
        self.assertIn("--count", result.stdout)
        self.assertEqual(manager.states(), {})

    def test_results_and_structured_data_are_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            repo = Repository(Path(temp) / "history.db")
            manager = SessionManager(repository=repo, session_factory=MemorySession)
            try:
                host = Host("test", "example")
                manager.run_command(host, "sysinfo")
                result = repo.list_command_history(search="result", host_id=host.id)[0]
                self.assertEqual(result.command_line, "sysinfo")
                self.assertEqual(result.records, [{"Count": 1}])
                self.assertIsNotNone(result.completed_at)
                self.assertEqual(repo.list_command_history(search="missing"), [])
            finally:
                manager.shutdown()

    def test_wmi_unavailable_command_rejected_before_connect(self):
        manager = SessionManager(session_factory=MemorySession)
        self.addCleanup(manager.shutdown)
        host = Host("test", "example", connection_mode=ConnectionMode.WMI)
        with self.assertRaises(CommandValidationError):
            manager.run_command(host, "Get-WinEvent")
        self.assertFalse(manager.states())

    def test_psrp_reuses_single_runspace_pool_and_closes_pipelines(self):
        from pypsrp.powershell import PSInvocationState
        pipelines = []

        class Pipeline:
            def __init__(self, pool):
                self.pool = pool
                self.state = PSInvocationState.NOT_STARTED
                self.streams = SimpleNamespace(error=[])
                self.output = [{"Name": "test"}]
                self.closed = False
                pipelines.append(self)

            def add_script(self, script, use_local_scope):
                self.local_scope = use_local_scope

            def begin_invoke(self):
                self.state = PSInvocationState.RUNNING

            def poll_invoke(self, timeout):
                self.state = PSInvocationState.COMPLETED

            def close(self):
                self.closed = True

        with patch("pypsrp.wsman.WSMan") as wsman, patch("pypsrp.powershell.RunspacePool") as pool:
            with patch("pypsrp.powershell.PowerShell", Pipeline):
                session = PsrpSession(Host("test", "example"), CredentialProfile("test", "user"))
                session.connect()
                first = session.run("first")
                session.health_check()
                session.run("second")
                self.assertEqual(first.records, [{"Name": "test"}])
                self.assertEqual(pool.call_count, 1)
                self.assertEqual(pool.call_args.kwargs, {"min_runspaces": 1, "max_runspaces": 1})
                self.assertTrue(all(p.pool is pool.return_value and p.closed and not p.local_scope for p in pipelines))
                session.disconnect()
                pool.return_value.close.assert_called_once()
                wsman.return_value.close.assert_called_once()
