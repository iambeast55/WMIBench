import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from remoteops.commands.templates import BUILTIN_TEMPLATES, CommandValidationError, prepare_command
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, ConnectionMode, CredentialProfile, Host, SessionState
from remoteops.remoting.dcom_session import DcomWmiSession
from remoteops.remoting.rpc_probe import RpcProbeError
from remoteops.remoting.session import RemoteSessionError
from remoteops.remoting.session_manager import SessionManager
from remoteops.remoting.wmi import WmiSession, wmi_session_factory
from remoteops.remoting.wmi_query import OS_QUERY, WmiQueryError


class MemoryDcomClient:
    def __init__(self):
        self.threads = []
        self.queries = 0
        self.pings = 0
        self.closes = 0
        self.next_ping = None
        self.failure = None

    def connect(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.arguments = args
        self.next_ping = time.monotonic() + 60

    def query(self, spec, timeout):
        self.query_os(timeout)
        if spec.class_name == "Win32_LogicalDisk":
            return [{"DeviceID": "C:", "VolumeName": "System", "FreeSpace": 2**30, "Size": 2**31}]
        return []

    def invoke(self, path, method, inputs, timeout):
        self.threads.append(threading.get_ident())
        return {"ReturnValue": 2, "sNames": None}

    def query_os(self, timeout):
        self.threads.append(threading.get_ident())
        self.queries += 1
        if self.failure:
            raise self.failure if isinstance(self.failure, Exception) else RpcProbeError(self.failure)
        self.next_ping = time.monotonic() + 60
        return [{"CSName": "FIXTURE", "Caption": "Windows", "TotalVisibleMemorySize": 2**40}]

    def maintain(self):
        self.threads.append(threading.get_ident())
        self.pings += 1
        if self.failure:
            raise RpcProbeError(self.failure)
        self.next_ping = time.monotonic() + 60

    def close(self):
        self.threads.append(threading.get_ident())
        self.closes += 1
        self.next_ping = None


class DcomSessionTests(unittest.TestCase):
    def setUp(self):
        self.host = Host("test", "192.0.2.10", connection_mode=ConnectionMode.WMI)
        self.profile = CredentialProfile("ops", "synthetic", method=AuthMethod.NTLM)
        self.credential = ResolvedCredential("synthetic", "synthetic-secret")
        patcher = patch.object(DcomWmiSession, "client_factory", MemoryDcomClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_platform_selects_intended_backend(self):
        with patch("remoteops.remoting.wmi.sys.platform", "linux"):
            self.assertIsInstance(wmi_session_factory(self.host, self.profile), DcomWmiSession)
        with patch("remoteops.remoting.wmi.sys.platform", "win32"):
            self.assertIsInstance(wmi_session_factory(self.host, self.profile), WmiSession)

    def test_ntlm_explicit_credentials_required_without_fallback(self):
        for method, credential in ((AuthMethod.KERBEROS, self.credential),
                                   (AuthMethod.BASIC, self.credential), (AuthMethod.NTLM, None)):
            self.profile.method = method
            session = DcomWmiSession(self.host, self.profile)
            with self.assertRaises(RemoteSessionError):
                session.connect(credential)
            self.assertEqual(session.state, SessionState.ERROR)
            self.assertIsNone(session.client)

    def test_sysinfo_records_and_wql_are_preserved(self):
        session = DcomWmiSession(self.host, self.profile)
        session.connect(self.credential)
        self.addCleanup(session.disconnect)
        result = session.run_prepared(prepare_command("sysinfo"))
        self.assertEqual(result.command, OS_QUERY)
        self.assertEqual(result.connection_mode, "wmi_dcom")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.records[0]["TotalVisibleMemorySize"], 2**40)
        self.assertIn("FIXTURE", result.stdout)
        self.assertEqual(session.state, SessionState.CONNECTED)
        disk = session.run_prepared(prepare_command("disk"))
        self.assertEqual(disk.records[0]["FreeGB"], 1)
        self.assertEqual(session.client.queries, 2)

    def test_failure_closes_and_sets_error_state(self):
        session = DcomWmiSession(self.host, self.profile)
        session.connect(self.credential)
        client = session.client
        client.failure = "Lost association. Reconnect explicitly."
        result = session.run_prepared(prepare_command("sysinfo"))
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.records, [])
        self.assertIn("Reconnect", result.stderr)
        self.assertEqual(session.state, SessionState.ERROR)
        self.assertIsNone(session.client)
        self.assertEqual(client.closes, 1)

    def test_disconnected_session_does_not_run(self):
        session = DcomWmiSession(self.host, self.profile)
        with self.assertRaises(RemoteSessionError):
            session.run_prepared(prepare_command("sysinfo"))
        with self.assertRaises(RemoteSessionError):
            session.run("arbitrary")

    def manager(self, **kwargs):
        manager = SessionManager(session_factory=DcomWmiSession, **kwargs)
        self.addCleanup(manager.shutdown)
        return manager

    def connect(self, manager):
        return manager.connect_with_credential(self.host, self.profile, self.credential)

    def drain(self, manager):
        manager._submit(self.host.id, lambda: None, activity=False).result()

    def test_all_builtins_preflight_and_help_without_dispatch(self):
        manager = self.manager()
        with patch("remoteops.remoting.wmi.sys.platform", "linux"), \
                patch.object(manager, "_resolve_credential") as credentials:
            for template in BUILTIN_TEMPLATES:
                with self.subTest(alias=template.alias):
                    self.assertTrue(manager.prepare(template.alias, self.host).native_wmi)
            with self.assertRaises(CommandValidationError):
                manager.run_command(self.host, "Get-Process")
            help_result = manager.run_command_with_credential(self.host, "disk -h")
            self.assertNotIn("Linux experimental DCOM", help_result.stdout)
            credentials.assert_not_called()
            self.assertEqual(manager.states(), {})

    def test_namespaces_reuse_clients_and_close_all(self):
        session = DcomWmiSession(self.host, self.profile)
        session.connect(self.credential)
        for alias in ("ports", "firewall", "tasks", "pending-reboot"):
            result = session.run_prepared(prepare_command(alias))
            self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(len(session.namespace_clients), 4)
        clients = list(session.namespace_clients.values())
        session.maintain()
        self.assertTrue(all(client.pings == 1 for client in clients))
        session.disconnect()
        self.assertTrue(all(client.closes == 1 for client in clients))
        self.assertIsNone(session.identity)
        self.assertEqual(session.namespace_clients, {})

    def test_namespace_failure_preserves_primary_client(self):
        session = DcomWmiSession(self.host, self.profile)
        session.connect(self.credential)
        root = session.client
        with patch.object(MemoryDcomClient, "connect", side_effect=RpcProbeError("denied")):
            result = session.run_prepared(prepare_command("ports"))
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(root.closes, 0)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertIsNotNone(session.identity)
        self.assertEqual(session.namespace_clients, {"root/cimv2": root})
        self.assertEqual(session.run_prepared(prepare_command("sysinfo")).exit_code, 0)
        session.disconnect()

    def test_command_errors_keep_session_and_history_usable(self):
        manager = self.manager()
        session = self.connect(manager)
        client = session.client
        for failure in (WmiQueryError("class mismatch"), WmiQueryError("query limit"),
                        WmiQueryError("access denied"), TimeoutError("command deadline")):
            with self.subTest(failure=str(failure)):
                client.failure = failure
                result = manager.run_command_with_credential(self.host, "services", self.profile)
                self.assertEqual(result.exit_code, 1)
                self.assertEqual(result.records, [])
                self.assertEqual(session.state, SessionState.CONNECTED)
                self.assertEqual(client.closes, 0)
                client.failure = None
                self.assertEqual(manager.run_command_with_credential(self.host, "sysinfo", self.profile).exit_code, 0)
                self.assertIs(session.client, client)

    def test_local_result_error_keeps_primary_connection(self):
        session = DcomWmiSession(self.host, self.profile)
        session.connect(self.credential)
        self.addCleanup(session.disconnect)
        client = session.client
        with patch("remoteops.remoting.dcom_session.records_text", side_effect=TypeError("local formatter")):
            self.assertEqual(session.run_prepared(prepare_command("sysinfo")).exit_code, 1)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertEqual(client.closes, 0)
        self.assertEqual(session.run_prepared(prepare_command("sysinfo")).exit_code, 0)
        self.assertIs(session.client, client)

    def test_auxiliary_operation_and_ping_failures_leave_primary_usable(self):
        for operation in ("query", "ping", "health"):
            with self.subTest(operation=operation):
                session = DcomWmiSession(self.host, self.profile)
                session.connect(self.credential)
                self.addCleanup(session.disconnect)
                root = session.client
                auxiliary = session._namespace_client("root/standardcimv2", 10)
                auxiliary.failure = "lost auxiliary association"
                if operation == "query":
                    result = session.run_prepared(prepare_command("ports"))
                    self.assertEqual(result.exit_code, 1)
                    self.assertIn("primary session retained", result.stderr)
                elif operation == "ping":
                    session.maintain()
                else:
                    session.health_check()
                self.assertEqual(session.state, SessionState.CONNECTED)
                self.assertEqual(auxiliary.closes, 1)
                self.assertEqual(root.closes, 0)
                self.assertEqual(session.namespace_clients, {"root/cimv2": root})
                self.assertEqual(session.run_prepared(prepare_command("sysinfo")).exit_code, 0)
                self.assertIs(session.client, root)
                self.assertEqual(session.run_prepared(prepare_command("ports")).exit_code, 0)
                self.assertIsNot(session.namespace_clients["root/standardcimv2"], auxiliary)

    def test_query_health_error_does_not_mark_session_lost(self):
        manager = self.manager()
        session = self.connect(manager)
        session.client.failure = WmiQueryError("query rejected")
        manager.schedule_keepalives(now=time.monotonic() + 1000)
        self.drain(manager)
        self.assertEqual(session.state, SessionState.CONNECTED)
        session.client.failure = None
        self.assertEqual(manager.run_command_with_credential(self.host, "sysinfo", self.profile).exit_code, 0)

    def test_maintenance_continues_with_optional_health_disabled(self):
        self.host.keepalive_seconds = 0
        manager = self.manager()
        session = self.connect(manager)
        client = session.client
        activity = manager._activity[self.host.id]
        manager.schedule_keepalives(now=client.next_ping + 1)
        self.drain(manager)
        self.assertEqual((client.pings, client.queries), (1, 0))
        self.assertEqual(manager._activity[self.host.id], activity)
        self.assertEqual(len(set(client.threads)), 1)
        self.assertNotEqual(client.threads[0], threading.get_ident())
        manager.disconnect(self.host)
        manager.schedule_keepalives(now=time.monotonic() + 1000)
        self.assertEqual(client.pings, 1)
        self.assertEqual(client.closes, 1)

    def test_protocol_pings_do_not_postpone_longer_health_interval(self):
        self.host.keepalive_seconds = 300
        manager = self.manager()
        session = self.connect(manager)
        client = session.client
        activity = manager._activity[self.host.id]
        manager.schedule_keepalives(now=activity + 61)
        self.drain(manager)
        manager.schedule_keepalives(now=activity + 301)
        self.drain(manager)
        self.assertEqual((client.pings, client.queries), (1, 1))

    def test_no_maintenance_while_worker_is_busy(self):
        self.host.keepalive_seconds = 0
        manager = self.manager()
        session = self.connect(manager)
        gate = threading.Event()
        future = manager._submit(self.host.id, lambda: gate.wait(2))
        try:
            manager.schedule_keepalives(now=session.client.next_ping + 1)
            self.assertEqual(session.client.pings, 0)
        finally:
            gate.set()
            future.result()

    def test_failed_maintenance_requires_explicit_reconnect(self):
        self.host.keepalive_seconds = 0
        manager = self.manager()
        session = self.connect(manager)
        session.client.failure = "ping set lost"
        manager.schedule_keepalives(now=session.client.next_ping + 1)
        self.drain(manager)
        self.assertEqual(session.state, SessionState.ERROR)
        result = manager.run_command_with_credential(self.host, "sysinfo", self.profile)
        self.assertIn("Reconnect explicitly", result.stderr)
        replacement = self.connect(manager)
        self.assertIsNot(replacement, session)
        self.assertEqual(replacement.state, SessionState.CONNECTED)

    def test_real_adapter_flows_through_history_with_reused_client(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = Repository(Path(folder) / "history.db")
            manager = SessionManager(repository=repository, session_factory=DcomWmiSession)
            try:
                session = self.connect(manager)
                client = session.client
                for _ in range(2):
                    result = manager.run_command_with_credential(self.host, "sysinfo", self.profile)
                    self.assertEqual(result.exit_code, 0)
                history = repository.list_command_history(host_id=self.host.id)
                self.assertEqual(len(history), 2)
                self.assertTrue(all(item.connection_mode == "wmi_dcom" for item in history))
                self.assertTrue(all(item.command == OS_QUERY for item in history))
                self.assertEqual(history[0].records[0]["CSName"], "FIXTURE")
                self.assertIs(session.client, client)
                self.assertEqual(client.queries, 2)
                self.assertEqual(len(set(client.threads)), 1)
            finally:
                manager.shutdown()

    def test_shutdown_closes_remaining_hosts_after_one_cleanup_error(self):
        manager = self.manager()
        first = self.connect(manager)
        second_host = Host("second", "192.0.2.11", connection_mode=ConnectionMode.WMI)
        second = manager.connect_with_credential(second_host, self.profile, self.credential)
        second_client = second.client
        with patch.object(first.client, "close", side_effect=RpcProbeError("uncertain release")):
            with self.assertRaisesRegex(RemoteSessionError, "uncertain release"):
                manager.shutdown()
        self.assertEqual(second_client.closes, 1)
        self.assertEqual(manager.states(), {})
