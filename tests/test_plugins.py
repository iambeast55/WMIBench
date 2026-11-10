import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from remoteops.commands.plugin_api import FakeContext, PluginError, execute_plugin
from remoteops.commands.plugins import PluginStore, read_plugin
from remoteops.commands.templates import CommandRegistry, CommandValidationError, command_backends
from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, CommandTemplate, ConnectionMode, Host, SessionState
from remoteops.remoting.batch import BatchRunner, BatchTarget
from remoteops.remoting.session import PersistentSession
from remoteops.remoting.session_manager import SessionManager


class StubSession(PersistentSession):
    def connect(self, credential=None):
        self.state = SessionState.CONNECTED
        self.calls = []

    def run(self, command, timeout_seconds=120):
        self.calls.append((command, threading.get_ident()))
        return CommandResult(self.host.id, self.host.name, command, exit_code=0,
                             records=[{"Vendor": "Fake"}])

    def run_prepared(self, command, timeout_seconds=120):
        self.calls.append((command, threading.get_ident()))
        if getattr(self, "break_transport", False):
            self.state = SessionState.ERROR
            return CommandResult(self.host.id, self.host.name, "", exit_code=1, stderr="Transport lost")
        if command.method_plan is not None:
            return CommandResult(self.host.id, self.host.name, "", exit_code=0, records=[{"ReturnValue": 0}])
        if command.delete_plan is not None:
            return CommandResult(self.host.id, self.host.name, "", exit_code=0, records=[{"Deleted": True}])
        return CommandResult(self.host.id, self.host.name, "", exit_code=0,
                             records=[{"Manufacturer": "Fake", "SMBIOSBIOSVersion": "1.0"}])

    def disconnect(self):
        self.state = SessionState.DISCONNECTED


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.registry = CommandRegistry(self.root / "commands.json")
        self.store = self.registry.plugins
        self.folder = self.store.scaffold("my-bios")

    def enable(self, source=None):
        if source is not None:
            (self.folder / "implementation.py").write_text(source, encoding="utf-8")
        snapshot = self.store.validate(self.folder)
        self.store.enable(snapshot)
        return snapshot

    def manifest(self, **changes):
        path = self.folder / "command.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data), encoding="utf-8")

    def manager(self):
        repository = Repository(self.root / "test.db")
        manager = SessionManager(repository=repository, session_factory=StubSession)
        manager.registry = self.registry
        self.addCleanup(manager.shutdown)
        return manager

    def test_validation_activation_and_help_never_execute_source(self):
        snapshot = self.enable('raise AssertionError("must not execute")\ndef run(ctx, args):\n    return []\n')
        self.assertEqual(self.registry.prepare("my-bios").plugin, snapshot)
        self.assertIn("Python command", self.registry.prepare("my-bios -h").help_text)

    def test_scaffold_offline(self):
        snapshot = self.enable()
        ctx = FakeContext(query_results=[[{"Manufacturer": "Fake", "SMBIOSBIOSVersion": "1"}]])
        self.assertEqual(execute_plugin(snapshot, ctx, {}), [{"Vendor": "Fake", "Version": "1"}])
        self.assertIn("Win32_BIOS", ctx.queries[0].query.wql)
        self.assertEqual(ctx.messages, ["Reading BIOS inventory"])

    def test_snapshot_reload_and_invalid_update_blocks_new_runs(self):
        first = self.enable('def run(ctx, args):\n    return [{"Version": 1}]\n')
        prepared = self.registry.prepare("my-bios")
        second = self.enable('def run(ctx, args):\n    return [{"Version": 2}]\n')
        self.assertEqual(execute_plugin(prepared.plugin, FakeContext(), {}), [{"Version": 1}])
        self.assertEqual(execute_plugin(second, FakeContext(), {}), [{"Version": 2}])
        (self.folder / "implementation.py").write_text("broken (", encoding="utf-8")
        with self.assertRaises(SyntaxError):
            self.store.validate(self.folder)
        with self.assertRaisesRegex(CommandValidationError, "unavailable"):
            self.registry.prepare("my-bios")
        self.assertIsNone(self.store.active(self.folder))
        self.assertEqual(execute_plugin(second, FakeContext(), {}), [{"Version": 2}])
        self.assertNotEqual(first.digest, second.digest)

    def test_restart_automatically_loads_changed_source(self):
        self.enable()
        restored = CommandRegistry(self.registry.path)
        self.assertIsNotNone(restored.prepare("my-bios").plugin)
        (self.folder / "implementation.py").write_text("def run(ctx, args):\n    return []\n", encoding="utf-8")
        restored = CommandRegistry(self.registry.path)
        self.assertEqual(execute_plugin(restored.prepare("my-bios").plugin, FakeContext(), {}), [])
        self.assertFalse(restored.plugins.errors)

    def test_change_during_approval_rejected(self):
        snapshot = self.store.validate(self.folder)
        self.manifest(description="Changed")
        with self.assertRaisesRegex(ValueError, "changed since inspection"):
            self.store.enable(snapshot)
        self.assertEqual(self.store.active(self.folder), snapshot)

    def test_removed_folder_disappears_and_existing_snapshot_stays_valid(self):
        snapshot = self.enable("def run(ctx, args):\n    return []\n")
        import shutil
        shutil.rmtree(self.folder)
        with self.assertRaises(CommandValidationError):
            CommandRegistry(self.registry.path).prepare("my-bios")
        self.assertEqual(execute_plugin(snapshot, FakeContext(), {}), [])

    def test_library_edits_do_not_serialize_or_drop_plugin(self):
        self.enable()
        self.registry.upsert(CommandTemplate("Custom", script="hostname", alias="custom"))
        self.assertIsNotNone(self.registry.prepare("my-bios").plugin)
        content = self.registry.path.read_text(encoding="utf-8")
        self.assertNotIn("my-bios", content)
        with self.assertRaises(CommandValidationError):
            self.registry.export_file(self.root / "export.json", [self.registry.aliases()["my-bios"]])

    def test_alias_conflicts_both_directions(self):
        self.manifest(alias="sysinfo")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.store.validate(self.folder)
        self.manifest(alias="my-bios")
        self.enable()
        with self.assertRaises(ValueError):
            self.registry.upsert(CommandTemplate("Conflict", script="hostname", alias="my-bios"))
        second = self.store.scaffold("second")
        (second / "command.json").write_text((self.folder / "command.json").read_text(), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "already installed"):
            self.store.validate(second)

    def test_invalid_manifests(self):
        original = (self.folder / "command.json").read_text()
        for changes in ({"api_version": True}, {"api_version": 4}, {"entry_point": "../bad.py:run"},
                        {"backends": []}, {"backends": ["ssh"]}, {"modifies_remote": "false"},
                        {"alias": "../bad"}, {"unknown": 1}, {"parameters": []}):
            with self.subTest(changes=changes):
                (self.folder / "command.json").write_text(original, encoding="utf-8")
                self.manifest(**changes)
                with self.assertRaises(ValueError):
                    self.store.validate(self.folder)

    def test_invalid_entrypoints(self):
        for source in ("async def run(ctx, args): pass", "def run(ctx): pass", "x = 1",
                       "@decorator\ndef run(ctx, args): pass"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                self.enable(source)

    def test_typed_arguments_and_backend_preflight(self):
        self.manifest(parameters={"_schema": {"count": {"type": "int", "min": 1, "max": 5, "required": True}}})
        self.enable()
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        self.assertEqual(manager.prepare("my-bios --count 3", host).values, {"count": 3})
        with self.assertRaises(ValueError):
            manager.prepare("my-bios --count 6", host)
        host.connection_mode = ConnectionMode.WINRM
        with self.assertRaises(ValueError):
            manager.prepare("my-bios --count 3", host)

    def test_fresh_module_globals_each_run(self):
        snapshot = self.enable("items = []\ndef run(ctx, args):\n    items.append(1)\n    return [{'Count': len(items)}]\n")
        for _ in range(2):
            self.assertEqual(execute_plugin(snapshot, FakeContext(), {}), [{"Count": 1}])

    def test_invalid_output_and_systemexit_are_command_errors(self):
        for expression in ("None", "[1]", "[{1: 'bad'}]", "[{'bad': float('nan')}]", "[{}] * 10001"):
            with self.subTest(expression=expression):
                snapshot = self.enable(f"def run(ctx, args):\n    return {expression}\n")
                with self.assertRaises(PluginError):
                    execute_plugin(snapshot, FakeContext(), {})
        snapshot = self.enable("def run(ctx, args):\n    raise SystemExit(2)\n")
        with self.assertRaisesRegex(PluginError, "SystemExit"):
            execute_plugin(snapshot, FakeContext(), {})

    def test_cancellation_deadline_and_progress_bounds(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaisesRegex(PluginError, "cancelled"):
            FakeContext(cancel=cancel).check_cancelled()
        with self.assertRaisesRegex(PluginError, "deadline"):
            FakeContext(timeout_seconds=-1).check_cancelled()
        context = FakeContext()
        for _ in range(105):
            context.progress("hello")
        self.assertEqual(len(context.messages), 100)

    def test_filters_are_escaped_and_invalid_queries_never_dispatch(self):
        context = FakeContext(query_results=[[]])
        context.query(class_name="Win32_Service", properties={"Name": "string"},
                      filters=[{"property": "Name", "operator": "eq", "value": "x' OR Name='y"}])
        self.assertIn("x\\' OR Name=\\'y", context.queries[0].query.wql)
        with self.assertRaises(ValueError):
            context.query(class_name="Win32_Service;DROP", properties={"Name": "string"})
        self.assertEqual(len(context.queries), 1)
        with self.assertRaisesRegex(PluginError, "no backend fallback"):
            context.powershell("hostname")

    def test_wmi_worker_ownership_history_and_local_failure_keeps_session(self):
        self.enable()
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        session = manager.connect(host)
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertTrue(result.succeeded, result.stderr)
        self.assertEqual(result.records[0]["Vendor"], "Fake")
        self.assertIsNotNone(result.history_id)
        self.assertIn("Python my-bios @", result.command)
        self.assertNotEqual(session.calls[0][1], threading.get_ident())
        self.enable("def run(ctx, args):\n    raise RuntimeError('example failure')\n")
        failed = manager.run_command_with_credential(host, "my-bios")
        self.assertIn("example failure", failed.stderr)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertIs(manager.connect(host), session)
        self.enable()

    def test_real_transport_failure_is_not_hidden(self):
        self.enable()
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        session = manager.connect(host)
        session.break_transport = True
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertIn("Transport lost", result.stderr)
        self.assertEqual(session.state, SessionState.ERROR)

    def test_winrm_helper(self):
        self.manifest(backends=["winrm"])
        self.enable("def run(ctx, args):\n    return ctx.powershell('hostname')['records']\n")
        self.assertEqual(command_backends(self.registry.aliases()["my-bios"]), ["WinRM"])
        manager = self.manager()
        host = Host("Lab", "example.invalid")
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertTrue(result.succeeded, result.stderr)

    def test_powershell_validation_reports_reason_and_failing_call(self):
        for value, reason in (("None", "received NoneType"),
                              ("'   '", "empty or whitespace")):
            with self.subTest(value=value):
                snapshot = self.enable(
                    f"def run(ctx, args):\n    ctx.powershell({value})\n    return []\n")
                context = FakeContext(backend="winrm")
                with self.assertRaises(PluginError) as caught:
                    execute_plugin(snapshot, context, {})
                diagnostic = str(caught.exception)
                self.assertIn(reason, diagnostic)
                self.assertIn('implementation.py", line 2, in run', diagnostic)
                self.assertNotIn("ctx.powershell(", diagnostic)
                self.assertEqual(context.scripts, [])

    def test_powershell_accepts_reported_script_length(self):
        context = FakeContext(backend="winrm", powershell_results=[{"records": []}])
        script = "#" + "x" * 41736
        self.assertEqual(context.powershell(script), {"records": []})
        self.assertEqual(context.scripts, [script])

    def test_batch_requires_separate_python_approval(self):
        self.enable("def run(ctx, args):\n    return []\n")
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        manager.connect(host)
        command = manager.prepare("my-bios", host)
        self.assertEqual(command.read_only_modes, ())
        with self.assertRaises(ValueError):
            manager.run_prepared_read_only(host, command)
        denied = BatchRunner(manager, [BatchTarget(host, command)]).run(lambda _: None)
        self.assertEqual(denied[0].status, "Skipped")
        approved = BatchRunner(manager, [BatchTarget(host, command, approve_python=True)]).run(lambda _: None)
        self.assertEqual(approved[0].status, "Succeeded")
        self.manifest(modifies_remote=True)
        self.enable()
        command = manager.prepare("my-bios", host)
        with self.assertRaises(ValueError):
            manager.run_prepared_read_only(host, command, approve_python=True)

    def test_install_is_immediately_available_and_never_overwrites(self):
        other = PluginStore(self.root / "other", lambda: set())
        installed = other.install(self.folder)
        self.assertIsNotNone(other.active(installed))
        self.assertTrue((installed / "test_implementation.py").exists())
        with self.assertRaises(FileExistsError):
            other.install(self.folder)

    def test_legacy_trust_file_is_not_required_or_modified(self):
        trust = self.root / "plugin-trust.json"
        trust.write_text("invalid old trust data", encoding="utf-8")
        registry = CommandRegistry(self.registry.path)
        self.assertIsNotNone(registry.prepare("my-bios").plugin)
        self.assertEqual(trust.read_text(), "invalid old trust data")

    def test_copied_folder_and_edits_are_detected_on_next_dispatch(self):
        import shutil
        external = self.root / "incoming"
        other = PluginStore(external, lambda: set())
        folder = other.scaffold("new-plugin")
        installed = self.store.root / folder.name
        shutil.copytree(folder, installed)
        first = self.registry.prepare("new-plugin").plugin
        (installed / "implementation.py").write_text("def run(ctx, args):\n    return [{'New': True}]\n", encoding="utf-8")
        second = self.registry.prepare("new-plugin").plugin
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(execute_plugin(second, FakeContext(), {}), [{"New": True}])

    def test_duplicate_plugins_are_both_unavailable_without_blocking_builtins(self):
        second = self.store.scaffold("second")
        (second / "command.json").write_text((self.folder / "command.json").read_text(), encoding="utf-8")
        self.registry.load()
        self.assertIsNone(self.store.active(self.folder))
        self.assertIsNone(self.store.active(second))
        self.assertIn("Duplicate", self.store.errors["my-bios"])
        self.assertEqual(self.registry.prepare("sysinfo").alias, "sysinfo")

    def test_external_saved_command_conflict_does_not_break_library(self):
        data = json.loads(self.registry.path.read_text())
        data["commands"].append({"name": "Saved", "alias": "my-bios", "script": "hostname"})
        self.registry.path.write_text(json.dumps(data), encoding="utf-8")
        self.registry.load()
        self.assertIsNone(self.store.active(self.folder))
        self.assertIsNone(self.registry.prepare("my-bios").plugin)

    def test_reload_while_running_keeps_frozen_source(self):
        snapshot = self.enable("def run(ctx, args):\n    ctx.progress('started')\n    return [{'Version': 1}]\n")
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        started, release = threading.Event(), threading.Event()

        def progress(_host, _message):
            started.set()
            release.wait(5)

        manager.plugin_progress = progress
        prepared = manager.prepare("my-bios", host)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(manager.run_prepared_with_credential, host, prepared)
            try:
                self.assertTrue(started.wait(5))
                self.enable("def run(ctx, args):\n    return [{'Version': 2}]\n")
            finally:
                release.set()
            result = future.result(5)
        self.assertEqual(result.records, [{"Version": 1}])
        self.assertIn(snapshot.digest, result.command)

    def test_cancel_running_plugin_retains_session(self):
        self.enable("def run(ctx, args):\n    ctx.progress('started')\n    ctx.check_cancelled()\n    return []\n")
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        manager.plugin_progress = lambda host_id, _: manager.cancel_plugin(host_id)
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertIn("cancelled", result.stderr)
        self.assertEqual(manager.session_state(host), SessionState.CONNECTED)

    def test_actual_wmi_adapters_keep_provider_failures_but_retire_broken_transport(self):
        from unittest.mock import Mock
        from remoteops.core.credentials import ResolvedCredential
        from remoteops.core.models import AuthMethod, CredentialProfile
        from remoteops.remoting.dcom_session import DcomWmiSession
        from remoteops.remoting.wmi import WmiSession
        from remoteops.remoting.rpc_probe import RpcProbeError
        from remoteops.remoting.wmi_query import WmiQueryError

        self.enable()
        profile = CredentialProfile("Fixture", "fixture", method=AuthMethod.NTLM)
        credential = ResolvedCredential("fixture", "fixture-secret")
        for adapter in (WmiSession, DcomWmiSession):
            with self.subTest(adapter=adapter.__name__):
                client = Mock()
                client.query.return_value = client.query_spec.return_value = [
                    {"Manufacturer": "Fake", "SMBIOSBIOSVersion": "1.0"}]
                client.query_os.return_value = []
                with patch.object(adapter, "client_factory", return_value=client):
                    manager = SessionManager(session_factory=adapter)
                    manager.registry = self.registry
                    host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
                    try:
                        session = manager.connect_with_credential(host, profile, credential)
                        result = manager.run_command_with_credential(host, "my-bios", profile, credential)
                        self.assertTrue(result.succeeded, result.stderr)
                        self.assertEqual(result.records[0]["Vendor"], "Fake")
                        operation = client.query_spec if adapter is WmiSession else client.query
                        failure = ValueError("provider query rejected") if adapter is WmiSession else WmiQueryError("provider query rejected")
                        operation.side_effect = failure
                        result = manager.run_command_with_credential(host, "my-bios", profile, credential)
                        self.assertFalse(result.succeeded)
                        self.assertEqual(session.state, SessionState.CONNECTED)
                        client.close.assert_not_called()
                        operation.side_effect = RpcProbeError("broken transport")
                        result = manager.run_command_with_credential(host, "my-bios", profile, credential)
                        self.assertFalse(result.succeeded)
                        self.assertEqual(session.state, SessionState.ERROR)
                    finally:
                        manager.shutdown()

    def test_pre_cancelled_snapshot_never_connects(self):
        self.enable()
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        cancel = threading.Event()
        cancel.set()
        with patch.object(manager, "_connect") as connect:
            result = manager.run_prepared_with_credential(host, manager.prepare("my-bios", host), cancel=cancel)
        connect.assert_not_called()
        self.assertIn("cancelled before dispatch", result.stderr)

    def test_plugin_invocation_uses_worker_and_records_method_outcome(self):
        self.manifest(api_version=2, modifies_remote=True)
        self.enable("def run(ctx, args):\n    result = ctx.invoke('Example', 'Change')\n    ctx.require_success(result)\n    return [result]\n")
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        session = manager.connect(host)
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertTrue(result.succeeded, result.stderr)
        self.assertIn("Example.Change returned; ReturnValue=0", result.command)
        self.assertEqual(len(session.calls), 1)
        self.assertNotEqual(session.calls[0][1], threading.get_ident())
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.manifest(modifies_remote=False)
        self.enable()
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertIn("modifies_remote", result.stderr)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.state, SessionState.CONNECTED)

    def test_v1_commands_remain_compatible(self):
        self.manifest(api_version=1)
        self.enable("def run(ctx, args):\n    return [{'Compatible': True}]\n")
        snapshot = self.registry.prepare("my-bios").plugin
        self.assertEqual(execute_plugin(snapshot, FakeContext(), {}), [{"Compatible": True}])

    def test_no_output_method_audit_does_not_invent_return_value(self):
        self.manifest(modifies_remote=True)
        self.enable("def run(ctx, args):\n    return [ctx.invoke('Example', 'Change', allow_no_output=True)]\n")
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        session = manager.connect(host)
        reply = CommandResult(host.id, host.name, "", exit_code=0, records=[{}])
        with patch.object(session, "run_prepared", return_value=reply) as remote:
            result = manager.run_command_with_credential(host, "my-bios")
        self.assertTrue(result.succeeded, result.stderr)
        self.assertEqual(result.records, [{}])
        self.assertIn("completed without output; provider ReturnValue unavailable", result.command)
        self.assertNotIn("ReturnValue=0", result.command)
        self.assertEqual(session.state, SessionState.CONNECTED)
        remote.assert_called_once()
        self.assertTrue(remote.call_args.args[0].method_plan.allow_no_output)

    def test_plugin_deletion_worker_audit_and_mutation_guard(self):
        self.manifest(api_version=3, modifies_remote=True)
        source = 'def run(ctx, args):\n    return [ctx.delete_instance(ctx.object_path("Example", {"Name": "x"}))]\n'
        self.enable(source)
        manager = self.manager()
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        session = manager.connect(host)
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertTrue(result.succeeded, result.stderr)
        self.assertIn('DeleteInstance Example.Name="x" completed; HRESULT=0', result.command)
        self.assertEqual(result.records, [{"Deleted": True, "Path": 'Example.Name="x"'}])
        self.assertNotEqual(session.calls[0][1], threading.get_ident())
        self.manifest(modifies_remote=False)
        self.enable()
        result = manager.run_command_with_credential(host, "my-bios")
        self.assertIn("modifies_remote", result.stderr)
        self.assertEqual(len(session.calls), 1)
        self.assertEqual(session.state, SessionState.CONNECTED)

    def test_admin_plugin_manifests_help_and_parsed_commands(self):
        examples = Path(__file__).resolve().parents[1] / "examples" / "plugins"
        for alias in ("scheduled-task", "registry"):
            folder = self.store.install(examples / alias)
            self.store.enable(self.store.validate(folder))
            self.assertIn("Changes remote system: yes", self.registry.prepare(alias + " -h").help_text)
        prepared = self.registry.prepare('registry set "SOFTWARE\\WMIBenchLab" --name Flag --type DWORD --data 0xffffffff')
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        self.assertEqual(execute_plugin(prepared.plugin, ctx, prepared.values)[0]["ReturnValue"], 0)
        self.assertEqual(ctx.methods[0].inputs["uValue"], (19, 2**32-1))
        prepared = self.registry.prepare('scheduled-task create Lab --executable "C:\\Tools\\test.exe" --account "LOCAL SERVICE"')
        ctx = FakeContext(query_results=[[]], method_results=[{"ReturnValue": 0}], modifies_remote=True)
        self.assertEqual(execute_plugin(prepared.plugin, ctx, prepared.values)[0]["Action"], "create")
        self.assertEqual(ctx.methods[0].inputs["User"], (8, "S-1-5-19"))

    def test_v3_optional_arguments_preserve_absent_versus_empty(self):
        self.manifest(api_version=3, parameters={"_schema": {
            "optional": {"type": "str"}, "choice": {"type": "str", "choices": ["a", "b"]},
            "number": {"type": "int"}, "flag": {"type": "bool"}, "defaulted": {"type": "str", "default": "x"}}})
        self.enable()
        self.assertEqual(self.registry.prepare("my-bios").values, {"defaulted": "x"})
        self.assertEqual(self.registry.prepare("my-bios --optional='' --flag=false").values,
                         {"defaulted": "x", "optional": "", "flag": False})
        for version in (1, 2):
            self.manifest(api_version=version, parameters={"_schema": {"optional": {"type": "str"}}})
            self.enable()
            self.assertEqual(self.registry.prepare("my-bios").values, {"optional": ""})
