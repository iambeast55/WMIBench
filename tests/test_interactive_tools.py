import os
import json
import subprocess
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from pypsrp.powershell import PSInvocationState

from remoteops.commands.plugin_api import FakeContext, PluginError
from remoteops.core.models import CommandResult, ConnectionMode, Host, SessionState
from remoteops.remoting.session import PsrpSession, PersistentSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.remoting.tools import perform, remote_path, run_script
from remoteops.ui.tools_panel import ToolsPanel
from remoteops.ui.notices import inline_notice
from remoteops.ui.theme import stylesheet


class ToolBackendTests(unittest.TestCase):
    def test_wmi_file_queries_are_directory_scoped_and_typed(self):
        ctx = FakeContext(query_results=[[{"Name": "C:\\Windows"}], [{"Name": "C:\\Windows\\Temp"}], [{"Name": "C:\\Windows\\x.txt"}]])
        rows = perform(ctx, "files", {"path": "C:/Windows"})
        self.assertEqual([r["Kind"] for r in rows], ["Directory", "File"])
        for plan in ctx.queries[1:]:
            self.assertIn("Drive", str(plan))
            self.assertIn("Path", str(plan))
        self.assertEqual(len(ctx.queries), 3)

    def test_paths_reject_unc_relative_streams_and_injection(self):
        for path in ("C:temp", "\\\\server\\share", "C:\\x:stream", "C:\\*", "C:\\x\nfoo", ""):
            with self.subTest(path=path), self.assertRaises(ValueError):
                remote_path(path)
        self.assertEqual(remote_path("C:/Windows/../Temp"), "C:\\Temp")

    def test_missing_directory_stops_before_file_enumeration(self):
        ctx = FakeContext(query_results=[[]])
        with self.assertRaisesRegex(ValueError, "Directory not found"):
            perform(ctx, "files", {"path": "C:\\missing"})
        self.assertEqual(len(ctx.queries), 1)

    def test_launch_returns_pid_and_provider_code_without_retry(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "ProcessId": 42}], modifies_remote=True)
        self.assertEqual(perform(ctx, "launch", {"command": "cmd.exe /c whoami"})[0]["ProcessId"], 42)
        self.assertEqual(ctx.methods[0].method, "Create")
        ctx = FakeContext(method_results=[{"ReturnValue": 9}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "provider code 9"):
            perform(ctx, "launch", {"command": "missing.exe"})
        self.assertEqual(len(ctx.methods), 1)

    def test_process_identity_checked_before_terminate(self):
        args = {"ProcessId": 45, "CreationDate": "old"}
        ctx = FakeContext(query_results=[[{**args, "CreationDate": "new"}]], modifies_remote=True)
        with self.assertRaisesRegex(ValueError, "changed"):
            perform(ctx, "terminate", args)
        self.assertFalse(ctx.methods)
        ctx = FakeContext(query_results=[[args]], method_results=[{"ReturnValue": 0}], modifies_remote=True)
        perform(ctx, "terminate", args)
        self.assertEqual(ctx.methods[0].method, "Terminate")

    def test_wmi_session_actions_do_not_treat_logon_ids_as_terminal_ids(self):
        ctx = FakeContext(modifies_remote=True)
        with self.assertRaisesRegex(ValueError, "requires WinRM"):
            perform(ctx, "logoff-session", {"SessionId": 12, "User": "test"})
        self.assertFalse(ctx.methods)

    def test_file_mutations_refuse_directories_and_invalid_destination(self):
        ctx = FakeContext(modifies_remote=True)
        for args in ({"Name": "C:\\Windows", "Kind": "Directory"},
                     {"Name": "C:\\x", "Kind": "File", "new_name": "..\\elsewhere"}):
            with self.assertRaises(ValueError):
                perform(ctx, "rename-file", args)
        self.assertFalse(ctx.methods)

    def test_winrm_literals_and_remote_wts_calls(self):
        ctx = FakeContext(backend="winrm", powershell_results=[{"records": []}, {"records": []}])
        perform(ctx, "files", {"path": "C:\\O'Brien"})
        self.assertIn("'C:\\O''Brien'", ctx.scripts[0])
        self.assertIn("-LiteralPath", ctx.scripts[0])
        perform(ctx, "disconnect-session", {"SessionId": 2, "User": "LAB\\user"})
        self.assertIn("WTSEnumerateSessionsW", ctx.scripts[1])
        self.assertIn("::Change(2, 'LAB\\user', $false)", ctx.scripts[1])

    def test_runner_propagates_native_exit_and_strips_status_record(self):
        session = Mock()
        session._stream_text = ""
        def run(script, timeout):
            import re
            marker = re.search(r"RemoteOpsExit_[a-f0-9]+", script).group()
            return CommandResult("id", "host", script, records=[{"Value": "hello"}, {marker: 9, "PipelineSucceeded": False}], exit_code=0)
        session.run.side_effect = run
        result = run_script(session, "cmd /c exit 9", threading.Event())
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(result.stdout, "hello")
        self.assertEqual(result.records, [{"Value": "hello"}])

    @unittest.skipUnless(os.name == "nt", "Windows PowerShell parser")
    def test_generated_powershell_parses_without_executing_remote_actions(self):
        ctx = FakeContext(backend="winrm", powershell_results=[{"records": []}] * 12)
        for action, args in (("processes", {}), ("users", {}), ("files", {"path": "C:\\O'Brien"}),
                             ("sessions", {}), ("logoff-session", {"SessionId": 2, "User": "D\\U"}),
                             ("rename-file", {"Name": "C:\\O'Brien.txt", "Kind": "File", "new_name": "other.txt"}),
                             ("delete-file", {"Name": "C:\\test.txt", "Kind": "File"})):
            perform(ctx, action, args)
        command = "$scripts=($input|Out-String)|ConvertFrom-Json; foreach($s in $scripts) { $e=$null; $t=$null; $null=[System.Management.Automation.Language.Parser]::ParseInput($s,[ref]$t,[ref]$e); if($e) {$e|Out-String|Write-Output; exit 1} }"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                input=json.dumps(ctx.scripts), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "nt", "Windows native exit-code check")
    def test_runner_wrapper_captures_actual_native_exit_code_locally(self):
        session = Mock()
        session._stream_text = ""
        def run(script, timeout):
            command = "$s=$input|Out-String; & ([scriptblock]::Create($s)) | ConvertTo-Json -Depth 8 -Compress"
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                       input=script, capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            from remoteops.remoting.output import output_records
            return CommandResult("id", "host", script, records=output_records(json.loads(completed.stdout)), exit_code=0)
        session.run.side_effect = run
        result = run_script(session, "'hello'; cmd.exe /c exit 9", threading.Event())
        self.assertEqual(result.exit_code, 9)
        self.assertEqual(result.stdout, "hello")

    def test_pre_cancel_does_not_allocate_or_dispatch_psrp_pipeline(self):
        session = PsrpSession(Host("test", "invalid"), None)
        session._pool = Mock()
        session.state = SessionState.CONNECTED
        cancel = threading.Event()
        cancel.set()
        with patch("pypsrp.powershell.PowerShell") as factory:
            result = session.run("anything", cancel=cancel)
        factory.assert_not_called()
        self.assertFalse(result.succeeded)
        self.assertEqual(session.state, SessionState.CONNECTED)

    def test_cancelled_psrp_pipeline_remains_connected_when_stop_confirmed(self):
        session = PsrpSession(Host("test", "invalid"), None)
        session._pool = Mock()
        session.state = SessionState.CONNECTED
        cancel = threading.Event()
        pipeline = Mock(state=PSInvocationState.RUNNING, streams=SimpleNamespace(error=[]))
        pipeline.begin_invoke.side_effect = cancel.set
        pipeline.stop.side_effect = lambda: setattr(pipeline, "state", PSInvocationState.STOPPED)
        with patch("pypsrp.powershell.PowerShell", return_value=pipeline):
            result = session.run("anything", cancel=cancel)
        self.assertFalse(result.succeeded)
        self.assertIn("cancelled", result.stderr)
        self.assertEqual(session.state, SessionState.CONNECTED)
        pipeline.stop.assert_called_once()
        pipeline.close.assert_called_once()

    def test_manager_requires_existing_connection_and_preserves_command_failure(self):
        class Session(PersistentSession):
            def connect(self, credential=None):
                self.state = SessionState.CONNECTED
                self.thread = threading.get_ident()
            def disconnect(self):
                self.state = SessionState.DISCONNECTED
            def run(self, command, timeout_seconds=120):
                raise AssertionError("Not a PowerShell operation")
            def run_prepared(self, command, timeout_seconds=120):
                assert self.thread == threading.get_ident()
                return CommandResult(self.host.id, self.host.name, "", stderr="Access denied", exit_code=1)
        factory = Mock(side_effect=Session)
        manager = SessionManager(session_factory=factory)
        self.addCleanup(manager.shutdown)
        host = Host("test", "invalid", connection_mode=ConnectionMode.WMI)
        self.assertFalse(manager.run_tool(host, "processes", {}).succeeded)
        factory.assert_not_called()
        session = manager.connect(host)
        self.assertFalse(manager.run_tool(host, "processes", {}).succeeded)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertFalse(manager.run_tool(host, "invalid", {}).succeeded)
        self.assertEqual(session.state, SessionState.CONNECTED)
        factory.assert_called_once()


class ToolUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ("segoeui.ttf", "consola.ttf"):
            font = Path("C:/Windows/Fonts") / name
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.dispatch = Mock()
        self.panel = ToolsPanel(self.dispatch, Mock(), Mock())
        self.panel.set_connection(True, False)
        self.panel.show()
        self.addCleanup(self.panel.close)

    def loaded(self, rows):
        result = CommandResult("id", "host", "", exit_code=0, records=rows)
        self.panel.loaded(SimpleNamespace(error=None, value=result))

    def test_live_pauses_for_busy_hidden_and_disconnect(self):
        p = self.panel
        p.mode.setCurrentIndex(2)
        p.live.setChecked(True)
        p.interval.setValue(3)
        for _ in range(3):
            p.tick()
        self.dispatch.assert_called_once()
        self.dispatch.reset_mock()
        p.set_busy(True)
        for _ in range(10):
            p.tick()
        self.dispatch.assert_not_called()
        p.set_busy(False)
        p.set_connection(False, False)
        self.assertFalse(p.live.isChecked())
        p.tick()
        self.dispatch.assert_not_called()

    def test_wmi_disables_content_transfer_and_terminal_session_actions(self):
        p = self.panel
        p.set_connection(True, True)
        p.mode.setCurrentIndex(1)
        self.loaded([{"Name": "C:\\x", "Kind": "File"}])
        p.table.table.selectRow(0)
        self.assertFalse(p.buttons["download"].isEnabled())
        self.assertTrue(p.buttons["delete-file"].isEnabled())
        p.mode.setCurrentIndex(3)
        self.assertEqual(p.accounts.itemText(0), "WMI logon inventory")
        self.assertFalse(p.buttons["logoff-session"].isEnabled())

    def test_destructive_action_has_inline_confirmation_and_failure_disables_stale_actions(self):
        p = self.panel
        p.mode.setCurrentIndex(2)
        self.loaded([{"Name": "notepad", "ProcessId": 43, "CreationDate": "date"}])
        p.table.table.selectRow(0)
        p.act("terminate")
        self.dispatch.assert_not_called()
        self.assertTrue(p.confirmation.isVisible())
        p.confirm()
        self.assertEqual(self.dispatch.call_args.args[0], "terminate")
        p.loaded(SimpleNamespace(error="Access denied", value=None))
        self.assertFalse(p.fresh)
        self.assertFalse(p.buttons["terminate"].isEnabled())
        self.assertIn("access denied", p.status.text().lower())

    def test_inline_notice_reuses_nonmodal_widget(self):
        dialog = QDialog()
        QVBoxLayout(dialog)
        self.addCleanup(dialog.close)
        inline_notice(dialog, "Invalid", "<bad>")
        notice = dialog._inline_notice
        inline_notice(dialog, "Failed", "Second")
        self.assertIs(dialog._inline_notice, notice)
        self.assertEqual(notice.label.text(), "Failed: Second")

    def test_upload_prefill_tracks_filename_without_overriding_manual_path(self):
        from remoteops.ui.transfer_dialog import TransferDialog
        dialog = TransferDialog("upload")
        self.addCleanup(dialog.close)
        dialog.set_remote_directory("C:\\Temp")
        dialog.local.setText("/home/operator/first.txt")
        self.assertEqual(dialog.remote.text(), "C:\\Temp\\first.txt")
        dialog.local.setText("/home/operator/second.txt")
        self.assertEqual(dialog.remote.text(), "C:\\Temp\\second.txt")
        dialog.remote.setText("C:\\Elsewhere\\custom.txt")
        dialog.local.setText("/home/operator/third.txt")
        self.assertEqual(dialog.remote.text(), "C:\\Elsewhere\\custom.txt")

    def test_layout_and_screenshots(self):
        p = self.panel
        rows = [{"Name": "C:\\Windows\\System32", "Kind": "Directory", "FileSize": 0},
                {"Name": "C:\\Windows\\WindowsUpdate.log", "Kind": "File", "FileSize": 12040}]
        for dark in (False, True):
            p.dark_mode = dark
            p.setStyleSheet(stylesheet(dark))
            for width in (720, 1100):
                p.resize(width, 620)
                for index in range(4):
                    p.mode.setCurrentIndex(index)
                    records = rows if index == 1 else ([{"User": "LAB\\operator", "SessionId": 2, "State": "Active", "Station": "RDP-Tcp#1"}] if index == 3 else
                               [{"Name": "explorer.exe", "ProcessId": 1234, "CreationDate": "20261020120000.000000-240", "WorkingSetSize": 8388608}])
                    self.loaded(records)
                    p.editor.setPlainText("Get-Date\nGet-ComputerInfo | Select-Object CsName, OsName")
                    QTest.qWait(10)
                    buttons = [b for b in p.buttons.values() if b.isVisible()]
                    for left, right in zip(buttons, buttons[1:]):
                        self.assertFalse(left.geometry().intersects(right.geometry()))
                    self.assertLessEqual(p.minimumSizeHint().width(), width)
                    capture = os.environ.get("REMOTEOPS_CAPTURE_DIR")
                    if capture:
                        folder = Path(capture)
                        folder.mkdir(parents=True, exist_ok=True)
                        p.grab().save(str(folder / f"tools-{'dark' if dark else 'light'}-{width}-{index}.png"))


if __name__ == "__main__":
    unittest.main()
