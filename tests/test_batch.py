import csv
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from remoteops.commands.templates import BUILTIN_TEMPLATES, command_read_only_modes, prepare_command
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, ConnectionMode, Host, SessionState, utc_now
from remoteops.remoting.batch import BatchRunner, BatchTarget, BatchUpdate, batch_records, export_batch_csv
from remoteops.remoting.session import PersistentSession
from remoteops.remoting.session_manager import SessionManager
from test_wmi_definitions import bios_template


class TrackingSession(PersistentSession):
    def __init__(self, host, profile):
        super().__init__(host, profile)
        self.connections = 0
        self.calls = []
        self.threads = []
        self.failure = None
        self.gate = None

    def connect(self, credential=None):
        self.connections += 1
        self.threads.append(threading.get_ident())
        self.state = SessionState.CONNECTED

    def run_prepared(self, command, timeout_seconds=120):
        started = utc_now()
        self.threads.append(threading.get_ident())
        self.calls.append(command)
        self.state = SessionState.RUNNING
        if self.gate:
            self.gate.wait(5)
        self.state = SessionState.CONNECTED
        return CommandResult(self.host.id, self.host.name, command.script,
                             records=[] if self.failure else [{"Host": "provider-value", "Value": 42}],
                             stderr=self.failure or "", exit_code=1 if self.failure else 0,
                             started_at=started, completed_at=utc_now())

    def run(self, command, timeout_seconds=120):
        raise AssertionError("Expected a prepared command")

    def disconnect(self):
        self.threads.append(threading.get_ident())
        self.state = SessionState.DISCONNECTED


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = Repository(Path(self.temp.name) / "history.db")
        self.manager = SessionManager(repository=self.repository, session_factory=TrackingSession)
        self.addCleanup(self.manager.shutdown)

    def hosts(self, count=3):
        return [self.repository.save_host(Host(f"host-{index}", f"192.0.2.{index + 1}")) for index in range(count)]

    def test_only_audited_scripts_and_custom_wmi_queries_qualify(self):
        for template in BUILTIN_TEMPLATES:
            modes = command_read_only_modes(template)
            self.assertEqual(modes, () if template.alias == "restart-service" else ("winrm", "wmi_dcom"))
        edited = deepcopy(BUILTIN_TEMPLATES[0])
        edited.script += "\nStop-Computer"
        edited.parameters["_modifies"] = False
        self.assertEqual(command_read_only_modes(edited), ())
        query = bios_template()
        query.script = "Stop-Computer"
        self.assertEqual(command_read_only_modes(query), ("wmi_dcom",))
        self.assertEqual(prepare_command("bios", [query]).read_only_modes, ("wmi_dcom",))

    def test_mixed_backends_reuse_sessions_and_isolate_failures_with_history(self):
        hosts = self.hosts()
        hosts[1].connection_mode = ConnectionMode.WMI
        sessions = [self.manager.connect(host) for host in hosts]
        sessions[1].failure = "Access denied"
        updates = []
        runner = BatchRunner(self.manager, [BatchTarget(host, prepare_command("sysinfo")) for host in hosts], 2)
        results = runner.run(updates.append)
        self.assertEqual([result.status for result in results], ["Succeeded", "Failed", "Succeeded"])
        self.assertEqual(len(self.repository.list_command_history()), 3)
        for host, session in zip(hosts, sessions):
            self.assertEqual(session.connections, 1)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(len(set(session.threads)), 1)
            self.assertIs(self.manager.connect(host), session)
            self.assertEqual(session.state, SessionState.CONNECTED)
            self.assertEqual([u.status for u in updates if u.host_id == host.id], ["Queued", "Running", results[hosts.index(host)].status])
        with self.assertRaisesRegex(RuntimeError, "replayed"):
            runner.run(updates.append)

    def test_disconnected_or_lost_sessions_never_reconnect_without_approval(self):
        first, second, third = self.hosts()
        lost = self.manager.connect(second)
        lost.state = SessionState.ERROR
        results = BatchRunner(self.manager, [
            BatchTarget(first, prepare_command("sysinfo")),
            BatchTarget(second, prepare_command("sysinfo"), allow_connect=True),
            BatchTarget(third, prepare_command("sysinfo"), allow_connect=True),
        ]).run(lambda update: None)
        self.assertEqual([result.status for result in results], ["Failed", "Failed", "Succeeded"])
        self.assertIn("No new connection was authorized", results[0].detail)
        self.assertIn("Reconnect explicitly", results[1].detail)
        self.assertEqual(lost.connections, 1)
        self.assertEqual(lost.calls, [])
        self.assertEqual(self.manager.session_state(first), SessionState.DISCONNECTED)

    def test_connection_lost_while_queued_is_not_reopened(self):
        hosts = self.hosts(2)
        sessions = [self.manager.connect(host) for host in hosts]
        runner = BatchRunner(self.manager, [BatchTarget(host, prepare_command("sysinfo")) for host in hosts], 1)

        def notify(update):
            if update.host_id == hosts[0].id and update.status == "Succeeded":
                self.manager.disconnect(hosts[1])

        results = runner.run(notify)
        self.assertEqual([result.status for result in results], ["Succeeded", "Failed"])
        self.assertEqual(sessions[1].calls, [])
        self.assertEqual(sessions[1].connections, 1)
        self.assertEqual(self.manager.session_state(hosts[1]), SessionState.DISCONNECTED)

    def test_cancel_before_run_drops_credentials_without_connecting(self):
        host = self.hosts(1)[0]
        runner = BatchRunner(self.manager, [BatchTarget(host, prepare_command("sysinfo"),
                             credential=ResolvedCredential("test", "secret"), allow_connect=True)])
        runner.cancel()
        results = runner.run(lambda update: None)
        self.assertEqual(results[0].status, "Skipped")
        self.assertIsNone(runner.targets[0].credential)
        self.assertEqual(self.manager.states(), {})

    def test_unexpected_failure_isolated_without_retry(self):
        hosts = self.hosts(2)
        self.manager.connect(hosts[1])
        original = self.manager.run_prepared_read_only

        def fail_first(host, *args, **kwargs):
            if host.id == hosts[0].id:
                raise OSError("local storage unavailable")
            return original(host, *args, **kwargs)

        with patch.object(self.manager, "run_prepared_read_only", side_effect=fail_first) as execute:
            results = BatchRunner(self.manager, [BatchTarget(host, prepare_command("sysinfo")) for host in hosts]).run(lambda update: None)
        self.assertEqual(execute.call_count, 2)
        self.assertEqual([result.status for result in results], ["Failed", "Succeeded"])
        self.assertIn("local storage unavailable", results[0].detail)

    def test_cancellation_is_bounded_and_queued_hosts_never_dispatch(self):
        hosts = self.hosts(5)
        sessions = [self.manager.connect(host) for host in hosts]
        gate = threading.Event()
        for session in sessions:
            session.gate = gate
        runner = BatchRunner(self.manager, [BatchTarget(host, prepare_command("sysinfo")) for host in hosts], 2)
        updates = []
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runner.run, updates.append)
            try:
                deadline = time.monotonic() + 3
                while sum(len(session.calls) for session in sessions) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(sum(len(session.calls) for session in sessions), 2)
                runner.cancel()
                deadline = time.monotonic() + 3
                while sum(u.status == "Skipped" for u in updates) < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(sum(u.status == "Skipped" for u in updates), 3)
                self.assertFalse(future.done())
            finally:
                gate.set()
            results = future.result(timeout=5)
        self.assertEqual([u.status for u in results], ["Succeeded", "Succeeded", "Skipped", "Skipped", "Skipped"])
        self.assertEqual(sum(len(session.calls) for session in sessions), 2)
        self.assertEqual(len(self.repository.list_command_history()), 2)
        self.assertTrue(all(session.state is SessionState.CONNECTED for session in sessions))

    def test_snapshot_and_credentials_are_not_reloaded_or_retained(self):
        host = self.hosts(1)[0]
        command = self.manager.prepare("sysinfo", host)
        target = BatchTarget(host, command, credential=ResolvedCredential("test", "secret-value"), allow_connect=True)
        runner = BatchRunner(self.manager, [target])
        target.host.address = "192.0.2.200"
        changed = deepcopy(self.manager.registry.aliases()["sysinfo"])
        changed.script = "Stop-Computer"
        self.manager.registry.upsert(changed)
        self.assertNotIn("secret-value", repr(target))
        with patch.object(self.manager, "prepare", side_effect=AssertionError("must not reload")):
            results = runner.run(lambda update: None)
        self.assertEqual(results[0].status, "Succeeded")
        snapshot = runner.targets[0]
        self.assertIsNone(snapshot.credential)
        self.assertEqual(snapshot.host.address, "192.0.2.1")
        session = self.manager._sessions[self.manager._key(snapshot.host, None)]
        self.assertEqual(session.calls[0].script, command.script)

    def test_invalid_read_only_targets_skip_and_manager_also_enforces(self):
        hosts = self.hosts(2)
        mutating = prepare_command("restart-service Spooler")
        results = BatchRunner(self.manager, [BatchTarget(hosts[0], mutating), BatchTarget(hosts[1], None, skip_reason="Unsupported")]).run(lambda update: None)
        self.assertTrue(all(result.status == "Skipped" for result in results))
        self.assertEqual(self.manager.states(), {})
        with self.assertRaises(ValueError):
            self.manager.run_prepared_read_only(hosts[0], mutating, allow_connect=True)
        with self.assertRaises(ValueError):
            BatchRunner(self.manager, [BatchTarget(hosts[0], mutating)] * 2)
        for concurrency in (0, 13, True):
            with self.assertRaises(ValueError):
                BatchRunner(self.manager, [BatchTarget(hosts[0], mutating)], concurrency)

    def test_combined_records_and_csv_preserve_outcomes_and_protect_identity(self):
        hosts = self.hosts(2)
        result = CommandResult(hosts[0].id, hosts[0].name, "query", records=[
            {"Host": "remote-name", "Cell": "=HYPERLINK(x)", "List": [1, 2]},
            {"Value": 3}], completed_at=utc_now())
        updates = {hosts[0].id: BatchUpdate(hosts[0].id, "Succeeded", result),
                   hosts[1].id: BatchUpdate(hosts[1].id, "Skipped", detail="Cancelled before dispatch")}
        records = list(batch_records(hosts, updates))
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["Host"], hosts[0].name)
        self.assertEqual(records[0]["Result.Host"], "remote-name")
        path = Path(self.temp.name) / "batch.csv"
        export_batch_csv(path, hosts, updates)
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["Result.Cell"], "'=HYPERLINK(x)")
        self.assertEqual(rows[-1]["Status"], "Skipped")
        self.assertEqual(rows[-1]["Error"], "Cancelled before dispatch")
