import ast
import json
import os
import subprocess
import tempfile
import threading
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from remoteops.commands.plugin_api import FakeContext
from remoteops.core.models import CommandResult, ConnectionMode, Host, SessionState
from remoteops.remoting.explorer import (
    checked_rows, method_snippet, native_inspect, perform, powershell_script, query_snippet, request,
)
from remoteops.remoting.session import FakeSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.remoting.wmi import WmiSession
from remoteops.ui.explorer import ExplorerPanel
from remoteops.ui.main_window import WorkerOutcome


SCHEMA = [
    {"Kind": "Class", "Name": "Win32_Process", "Qualifiers": {}},
    {"Kind": "Property", "Name": "Name", "Type": "string", "Key": False, "Qualifiers": {}},
    {"Kind": "Property", "Name": "ProcessId", "Type": "uint32", "Key": True, "Qualifiers": {}},
    {"Kind": "Method", "Name": "Create", "Static": True, "Qualifiers": {}},
    {"Kind": "Parameter", "Name": "CommandLine", "Type": "string", "Direction": "In", "Method": "Create"},
    {"Kind": "Parameter", "Name": "ReturnValue", "Type": "uint32", "Direction": "Out", "Method": "Create"},
]


class ExplorerBackendTests(unittest.TestCase):
    def test_validation_rejects_query_and_namespace_injection(self):
        for namespace in ("root; whoami", "//other/root", "root/../cimv2", "root/cimv2\n"):
            with self.subTest(namespace=namespace), self.assertRaises(ValueError):
                request("classes", {"namespace": namespace})
        for name in ("Win32_Process;foo", "Win32*", "Name FROM Win32_Process"):
            with self.assertRaises(ValueError):
                request("describe", {"class_name": name})
        for limit in (0, 1001, True, "100"):
            with self.assertRaises(ValueError):
                request("instances", {"class_name": "Win32_Process", "properties": ["Name"], "limit": limit})
        with self.assertRaises(ValueError):
            request("delete", {})

    def test_normalized_namespace_and_bounded_properties(self):
        self.assertEqual(request("classes", {"namespace": "root\\cimv2"}), {"namespace": "root/cimv2"})
        with self.assertRaises(ValueError):
            request("instances", {"class_name": "Win32_Process", "properties": ["Name"] * 65})
        with self.assertRaisesRegex(ValueError, "size limit"):
            checked_rows([{}] * 10001)

    def test_query_and_method_examples_execute_with_fake_context(self):
        scope = {}
        exec(query_snippet("root/cimv2", "Win32_Process", SCHEMA[1:3], 5), scope)
        ctx = FakeContext(query_results=[[{"Name": "cmd.exe", "ProcessId": 7}]])
        self.assertEqual(scope["run"](ctx, {})[0]["ProcessId"], 7)
        self.assertEqual(ctx.queries[0].query.take, 5)
        scope = {}
        exec(method_snippet("root/cimv2", "Win32_Process", SCHEMA[3], SCHEMA), scope)
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        scope["run"](ctx, {"CommandLine": "cmd.exe /c exit 7"})
        self.assertEqual(ctx.methods[0].method, "Create")
        instance = method_snippet("root/cimv2", "Win32_Process", {"Name": "Terminate", "Static": False}, [])
        self.assertIn('args["object_path"]', instance)
        ast.parse(instance)

    def test_unsupported_method_types_are_not_invented(self):
        rows = [{"Method": "Create", "Name": "Startup", "Type": "object", "Direction": "In"}]
        self.assertNotIn("ctx.invoke(", method_snippet("root/cimv2", "Win32_Process", SCHEMA[3], rows))

    def test_native_query_uses_explicit_prefix_without_extra_rows(self):
        client = Mock()
        client.query.return_value = [{"Name": "cmd.exe"}]
        args = request("instances", {"class_name": "Win32_Process", "properties": ["Name"], "limit": 5})
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            self.assertEqual(native_inspect(client, "instances", args, 30), [{"Name": "cmd.exe"}])
        client.query.assert_called_once_with("SELECT Name FROM Win32_Process", 30, 5, take=5)
        client.services.ExecMethod.assert_not_called()

    def test_native_schema_includes_arrays_methods_and_qualifiers(self):
        def prop(name, kind, array=False):
            return SimpleNamespace(Name=name, CIMType=kind, IsArray=array, Qualifiers_=[])
        obj = SimpleNamespace(Path_=SimpleNamespace(Class="Example"), Qualifiers_=[],
                              Properties_=[prop("Names", 8, True)],
                              Methods_=[SimpleNamespace(Name="Test", Qualifiers_=[SimpleNamespace(Name="Static", Value=True)],
                                  InParameters=None, OutParameters=SimpleNamespace(Properties_=[prop("ReturnValue", 19)]))])
        client = Mock()
        client.services.Get.return_value = obj
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            rows = native_inspect(client, "describe", {"class_name": "Example"}, 30)
        self.assertEqual(rows[1]["Type"], "string[]")
        self.assertTrue(rows[2]["Static"])
        self.assertEqual(rows[3]["Direction"], "Out")

    def test_provider_failure_keeps_native_session(self):
        host = Host(name="Test", address="192.0.2.1", connection_mode=ConnectionMode.WMI)
        session = WmiSession(host, None)
        session.state = SessionState.CONNECTED
        session.client = Mock()
        session.namespace_clients["root/cimv2"] = session.client
        with patch("remoteops.remoting.explorer.native_inspect", side_effect=ValueError("Invalid class")):
            with self.assertRaisesRegex(Exception, "Invalid class"):
                perform(session, "describe", {"class_name": "Missing"})
        self.assertEqual(session.state, SessionState.CONNECTED)
        session.client.close.assert_not_called()

    def test_transport_failure_still_marks_native_session_lost(self):
        host = Host(name="Test", address="192.0.2.1", connection_mode=ConnectionMode.WMI)
        session = WmiSession(host, None)
        session.state = SessionState.CONNECTED
        session.client = Mock()
        session.namespace_clients["root/cimv2"] = session.client
        with patch("remoteops.remoting.explorer.native_inspect", side_effect=OSError("RPC unavailable")):
            with self.assertRaises(OSError):
                perform(session, "classes", {})
        self.assertEqual(session.state, SessionState.ERROR)

    def test_winrm_json_failure_and_cancel(self):
        session = Mock()
        session.host.connection_mode = ConnectionMode.WINRM
        session.run.return_value = CommandResult("h", "host", "", records=[{"ExplorerJson": json.dumps(SCHEMA)}], exit_code=0)
        self.assertEqual(perform(session, "describe", {"class_name": "Win32_Process"}), SCHEMA)
        session.run.return_value.records = [{"ExplorerJson": "null"}]
        with self.assertRaises(ValueError):
            perform(session, "describe", {"class_name": "Win32_Process"})
        cancel = threading.Event()
        cancel.set()
        session.run.reset_mock()
        with self.assertRaisesRegex(ValueError, "cancelled"):
            perform(session, "classes", {}, cancel)
        session.run.assert_not_called()

    def test_linux_schema_not_supported_does_not_disconnect_or_fallback(self):
        from remoteops.remoting.dcom_session import DcomWmiSession
        host = Host(name="Test", address="192.0.2.1", connection_mode=ConnectionMode.WMI)
        session = DcomWmiSession(host, None)
        session.state = SessionState.CONNECTED
        with self.assertRaisesRegex(ValueError, "DCOM transport"):
            perform(session, "classes", {})
        self.assertEqual(session.state, SessionState.CONNECTED)

    def test_manager_requires_connection_and_runs_on_host_worker(self):
        host = Host(name="Test", address="192.0.2.1")
        manager = SessionManager(session_factory=FakeSession)
        try:
            with patch("remoteops.remoting.explorer.perform") as explore:
                self.assertFalse(manager.run_tool(host, "explorer-classes", {}).succeeded)
                explore.assert_not_called()
            manager.connect(host)
            caller = threading.get_ident()
            def inspect(*args):
                self.assertNotEqual(threading.get_ident(), caller)
                return [{"Name": "Win32_Process"}]
            with patch("remoteops.remoting.explorer.perform", side_effect=inspect):
                self.assertTrue(manager.run_tool(host, "explorer-classes", {}).succeeded)
        finally:
            manager.shutdown()

    @unittest.skipUnless(os.name == "nt", "PowerShell syntax check")
    def test_powershell_parses_without_running_it(self):
        for action in ("namespaces", "classes", "describe", "instances"):
            args = request(action, {"class_name": "Win32_Process", "properties": ["Name", "ProcessId"]})
            script = powershell_script(action, args)
            import base64
            encoded = base64.b64encode(script.encode("utf-16le")).decode()
            check = ("$s=[Text.Encoding]::Unicode.GetString([Convert]::FromBase64String('" + encoded + "')); "
                     "$t=$null; $e=$null; [void][Management.Automation.Language.Parser]::ParseInput($s,[ref]$t,[ref]$e); "
                     "if($e.Count){$e | Out-String | Write-Output; exit 1}")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", check], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ExplorerUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.panel = ExplorerPanel(lambda *args: self.calls.append(args))
        self.panel.set_connection(True)

    def tearDown(self):
        self.panel.close()
        self.panel.deleteLater()
        self.app.processEvents()

    def finish(self, rows):
        self.calls[-1][2](WorkerOutcome(value=CommandResult("h", "host", "", records=rows, exit_code=0)))

    def test_class_selection_and_query(self):
        self.panel.refresh()
        self.assertEqual(self.calls[-1][0], "explorer-classes")
        self.finish([{"Name": "Win32_Process"}])
        self.panel.classes.setCurrentRow(0)
        self.assertEqual(self.calls[-1][0], "explorer-describe")
        self.finish(SCHEMA)
        self.assertEqual(self.panel.methods.count(), 1)
        self.assertEqual(self.panel.parameters.model.rowCount(), 2)
        self.panel.limit.setValue(5)
        self.panel.query()
        self.assertEqual(self.calls[-1][1]["limit"], 5)
        self.finish([{"Name": "cmd.exe"}])
        self.assertEqual(self.panel.instances.model.rowCount(), 1)

    def test_stale_reply_cannot_repopulate_disconnected_host(self):
        self.panel.refresh()
        self.panel.set_connection(False)
        self.finish([{"Name": "Old"}])
        self.assertEqual(self.panel.classes.count(), 0)
        self.assertFalse(self.panel.query_button.isEnabled())

    def test_failure_inline_and_no_implicit_dispatch(self):
        self.assertEqual(self.calls, [])
        self.panel.refresh()
        self.calls[-1][2](WorkerOutcome(error="Access denied"))
        self.assertIn("Terminal", self.panel.status.text())
        self.assertEqual(self.panel.status.toolTip(), "Access denied")
        self.panel.set_busy(True)
        self.panel.refresh()
        self.assertEqual(len(self.calls), 1)

    def test_namespace_change_clears_old_schema_and_snippets(self):
        self.panel.describe("Win32_Process")
        self.finish(SCHEMA)
        self.panel.copy_code(True)
        self.assertIn("ctx.invoke", QApplication.clipboard().text())
        self.panel.namespace.setText("root/default")
        self.panel.open_namespace()
        self.assertEqual(self.panel.schema, [])
        self.assertEqual(self.panel.code.toPlainText(), "")
        self.assertEqual(self.calls[-1][1]["namespace"], "root/default")


if __name__ == "__main__":
    unittest.main()
