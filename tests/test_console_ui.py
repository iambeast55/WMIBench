import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CommandResult, CredentialProfile, Host, SessionState
from remoteops.remoting.session import FakeSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.main_window import MainWindow, WorkerOutcome, append_terminal


class ConsoleUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "test.sqlite3")
        self.host = self.repository.save_host(Host(name="server-01", address="server-01.example.test"))
        self.manager = SessionManager(repository=self.repository, session_factory=FakeSession)
        self.window = MainWindow(self.repository, self.manager)
        self.window.resize(1200, 780)
        self.window.show()
        self.app.processEvents()
        self.tab = self.window.host_tabs_by_id[self.host.id]

    def tearDown(self):
        self.window.thread_pool.waitForDone(5000)
        self.app.processEvents()
        self.window.close()
        self.window.thread_pool.waitForDone(5000)
        self.app.processEvents()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for_workers(self):
        deadline = time.monotonic() + 5
        while self.window.pending_operations and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.window.pending_operations)

    def test_connection_feedback_survives_inventory_refresh(self):
        gate = threading.Event()

        class DelayedSession(FakeSession):
            def connect(self, credential=None):
                gate.wait(3)
                super().connect(credential)

        self.manager.session_factory = DelayedSession
        try:
            self.tab.connect_button.click()
            self.assertEqual(self.tab.status_text.text(), "Connecting")
            self.assertTrue(self.tab.progress.isVisible())
            self.assertFalse(self.tab.connect_button.isEnabled())
            self.assertFalse(self.tab.auth_combo.isEnabled())
            self.repository.save_host(Host(name="another-server", address="another.example.test"))
            self.window.reload_data()
            self.assertIs(self.window.host_tabs_by_id[self.host.id], self.tab)
        finally:
            gate.set()
        self.wait_for_workers()
        self.assertEqual(self.tab.status_text.text(), "Connected")
        self.assertFalse(self.tab.progress.isVisible())
        self.assertFalse(self.tab.connect_button.isEnabled())
        self.assertIn("[connected]", self.tab.terminal.toPlainText())
        self.tab.disconnect_button.click()
        self.assertEqual(self.tab.status_text.text(), "Disconnecting")
        self.wait_for_workers()
        self.assertEqual(self.tab.status_text.text(), "Disconnected")
        self.assertTrue(self.tab.connect_button.isEnabled())
        self.assertFalse(self.tab.disconnect_button.isEnabled())

    def test_command_error_is_not_repeated_by_session_poll(self):
        detail = "Create outcome unconfirmed; lost reply"
        result = CommandResult(self.host.id, self.host.name, "create", stderr=detail, exit_code=1)
        with patch.object(self.manager, "states", return_value={(self.host.id, None): SessionState.ERROR}), \
             patch.object(self.manager, "errors", return_value={self.host.id: detail}), \
             patch.object(self.manager, "schedule_keepalives"):
            self.window.append_terminal_result(result, self.tab.terminal)
            self.window.check_sessions()
            self.window.check_sessions()
            text = self.tab.terminal.toPlainText()
            self.assertEqual(text.count("Outcome unknown."), 1)
            self.assertIn(detail, text)
            # A new underlying failure is still surfaced by the poll.
            with patch.object(self.manager, "errors", return_value={self.host.id: "RPC server unavailable"}):
                self.window.check_sessions()
            self.assertIn("unreachable", self.tab.terminal.toPlainText())

    def test_failure_retains_full_error_and_reenables_controls(self):
        class FailedSession(FakeSession):
            def connect(self, credential=None):
                raise RuntimeError("Detailed authentication failure " + "x" * 600)

        self.manager.session_factory = FailedSession
        self.tab.connect_button.click()
        self.wait_for_workers()
        self.assertEqual(self.tab.status_text.text(), "Error")
        self.assertTrue(self.tab.connect_button.isEnabled())
        self.assertFalse(self.tab.progress.isVisible())
        self.assertIn("Authentication failed", self.tab.terminal.toPlainText())
        self.assertIn("Traceback", self.tab.terminal.toPlainText())
        self.assertIn("x" * 600, self.tab.terminal.toPlainText())
        self.assertFalse(hasattr(self.tab, "error_details_button"))

    def test_profile_selection_persists_without_losing_output(self):
        profile = self.repository.save_credential_profile(CredentialProfile(name="Ops", username="ops"))
        append_terminal(self.tab.terminal, "Existing output")
        self.window.reload_data()
        self.tab.auth_combo.setCurrentIndex(self.tab.auth_combo.findData(profile.id))
        self.assertEqual(self.repository.get_host(self.host.id).auth_profile_id, profile.id)
        self.assertIn("Existing output", self.tab.terminal.toPlainText())
        self.window.host_search.setText("does-not-exist")
        self.assertTrue(self.window.host_tree.host_items[self.host.id].isHidden())
        self.window.host_search.setText("EXAMPLE")
        self.assertFalse(self.window.host_tree.host_items[self.host.id].isHidden())

    def test_connected_command_does_not_prompt_or_echo_expansion(self):
        profile = self.repository.save_credential_profile(
            CredentialProfile(name="Ops", username="ops", method=AuthMethod.NTLM)
        )
        self.host.auth_profile_id = profile.id
        self.repository.save_host(self.host)
        self.window.reload_data()
        self.manager.connect_with_credential(self.host, profile)
        self.window.refresh_statuses()
        result = CommandResult(self.host.id, self.host.name, "expanded script", stdout="output", exit_code=0)
        with patch.object(self.window, "resolve_credential_for_profile") as prompt:
            with patch.object(self.manager, "run_prepared_with_credential", return_value=result):
                self.tab.command_input.setText("sysinfo")
                self.tab.run_button.click()
                self.wait_for_workers()
            prompt.assert_not_called()
        output = self.tab.terminal.toPlainText()
        self.assertIn("wmibench> sysinfo", output)
        self.assertNotIn("Get-CimInstance", output)
        self.tab.command_input.setText("draft")
        QTest.keyClick(self.tab.command_input, Qt.Key.Key_Up)
        self.assertEqual(self.tab.command_input.text(), "sysinfo")
        QTest.keyClick(self.tab.command_input, Qt.Key.Key_Down)
        self.assertEqual(self.tab.command_input.text(), "draft")

    def test_command_error_does_not_mark_healthy_session_disconnected(self):
        self.manager.connect(self.host)
        result = CommandResult(self.host.id, self.host.name, "bad", stderr="Unknown command", exit_code=1)
        self.window.finish_run(self.host.id, self.tab.terminal, WorkerOutcome(value=result))
        self.assertEqual(self.window.state_for_host(self.host), SessionState.CONNECTED)
        self.assertEqual(self.tab.activity_label.text(), "Command failed")

    def test_output_preserves_table_spacing_and_literal_markup(self):
        output = "Name      Count\n----      -----\n<server>      2\nerror: &literal;"
        self.tab.terminal.clear()
        append_terminal(self.tab.terminal, output)
        self.assertEqual(self.tab.terminal.toPlainText(), output)
        self.tab.wrap_button.click()
        self.assertEqual(self.tab.terminal.toPlainText(), output)

    def test_worker_error_uses_actual_session_state(self):
        self.manager.connect(self.host)
        self.window.set_host_state(self.host.id, SessionState.RUNNING)
        self.window.finish_run(self.host.id, self.tab.terminal, WorkerOutcome(error="Local history save failed"))
        self.assertEqual(self.tab.status_text.text(), "Connected")
        self.assertIn("Session connected", self.tab.terminal.toPlainText())
        self.assertIn("Local history save failed", self.tab.terminal.toPlainText())

    def test_errors_are_copyable_in_console_without_extra_controls(self):
        self.manager.connect(self.host)
        self.window.refresh_statuses()
        self.assertEqual(self.tab.status_text.text(), "Connected")
        detail = "Traceback: fixture\nAccess denied 0x80041003\n" + "x" * 600
        self.window.present_error(self.host.id, self.tab.terminal, detail)
        self.tab.terminal.selectAll()
        self.tab.terminal.copy()
        self.assertIn(detail, self.app.clipboard().text())
        for name in ("error_details_button", "browse_commands_button", "arguments_button"):
            self.assertFalse(hasattr(self.tab, name))
        self.assertEqual([self.tab.view_tabs.tabText(i) for i in range(self.tab.view_tabs.count())],
                         ["Terminal", "History", "Services", "Events", "Tools", "Explorer"])
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            self.window.grab().save(str(target / "connected-command-error.png"))

    def test_help_and_unknown_commands_do_not_dispatch(self):
        with patch.object(self.window, "start_worker") as worker:
            self.window.run_host_command(self.host, "events -h", self.tab.terminal)
            self.window.run_host_command(self.host, "Get-Process", self.tab.terminal)
            worker.assert_not_called()
        self.assertIn("--count VALUE", self.tab.terminal.toPlainText())
        self.assertIn("Unknown command", self.tab.terminal.toPlainText())

    def test_history_search_and_structured_table(self):
        result = CommandResult(self.host.id, self.host.name, "script", stdout="needle", command_line="sysinfo", records=[{"Name": "server", "Count": 2}])
        self.repository.save_command_result(result)
        self.tab.history.search.setText("needle")
        self.tab.history.reload()
        self.assertEqual(len(self.tab.history.results), 1)
        self.assertIn("needle", self.tab.history.viewer.text.toPlainText())
        self.assertEqual(self.tab.history.viewer.table.proxy.rowCount(), 1)
        self.tab.history.viewer.table.search.setText("missing")
        self.assertEqual(self.tab.history.viewer.table.proxy.rowCount(), 0)

    def test_dark_preference_persists(self):
        self.window.set_dark_mode(True)
        self.window.settings.sync()
        from PySide6.QtCore import QSettings
        settings = QSettings(str(self.repository.db_path.parent / "settings.ini"), QSettings.Format.IniFormat)
        self.assertTrue(settings.value("dark_mode", False, type=bool))

    def test_linux_wmi_sysinfo_reaches_table_history_and_disconnect(self):
        from remoteops.core.credentials import ResolvedCredential
        from remoteops.core.models import ConnectionMode
        from remoteops.remoting.dcom_session import DcomWmiSession
        from test_dcom_session import MemoryDcomClient

        profile = self.repository.save_credential_profile(CredentialProfile(
            "Lab NTLM", "synthetic", method=AuthMethod.NTLM
        ))
        self.host.connection_mode = ConnectionMode.WMI
        self.host.auth_profile_id = profile.id
        self.repository.save_host(self.host)
        self.window.reload_data()
        self.manager.session_factory = DcomWmiSession
        with patch.object(DcomWmiSession, "client_factory", MemoryDcomClient), \
                patch("remoteops.remoting.wmi.sys.platform", "linux"):
            session = self.manager.connect_with_credential(
                self.host, profile, ResolvedCredential("synthetic", "synthetic-secret")
            )
            client = session.client
            self.window.refresh_statuses()
            with patch.object(self.window, "resolve_credential_for_profile") as prompt:
                for _ in range(2):
                    self.tab.command_input.setText("sysinfo")
                    self.tab.run_button.click()
                    self.wait_for_workers()
                prompt.assert_not_called()
            self.assertEqual(self.tab.history.viewer.table.proxy.rowCount(), 1)
            self.assertIn("FIXTURE", self.tab.terminal.toPlainText())
            self.assertNotIn("synthetic-secret", self.tab.terminal.toPlainText())
            self.assertEqual(self.tab.status_text.text(), "Connected")
            history = self.repository.list_command_history(host_id=self.host.id)
            self.assertEqual(len(history), 2)
            self.assertEqual(history[0].connection_mode, "wmi_dcom")
            self.assertEqual(history[0].records[0]["CSName"], "FIXTURE")
            self.assertEqual(client.queries, 2)
            self.assertFalse(self.tab.transfer_button.isEnabled())
            self.tab.command_input.setText("disk")
            self.tab.run_button.click()
            self.wait_for_workers()
            self.assertEqual(client.queries, 3)
            self.assertIn("FreeGB", self.tab.terminal.toPlainText())
            from remoteops.remoting.wmi_query import WmiQueryError

            client.failure = WmiQueryError("Query returned an unexpected CIM class 'Unrelated'")
            with patch.object(self.window, "resolve_credential_for_profile") as prompt:
                self.tab.command_input.setText("services")
                self.tab.run_button.click()
                self.wait_for_workers()
                self.assertEqual(self.tab.status_text.text(), "Connected")
                self.assertIn("WMI response parse failed", self.tab.terminal.toPlainText())
                self.assertIn("unexpected CIM class", self.tab.terminal.toPlainText())
                self.assertEqual(client.closes, 0)
                client.failure = None
                self.tab.command_input.setText("sysinfo")
                self.tab.run_button.click()
                self.wait_for_workers()
                prompt.assert_not_called()
            self.assertEqual(self.tab.status_text.text(), "Connected")
            self.assertIs(session.client, client)
            history = self.repository.list_command_history(host_id=self.host.id)
            self.assertEqual([item.exit_code for item in history[:2]], [0, 1])
            self.tab.disconnect_button.click()
            self.wait_for_workers()
            self.assertEqual(self.tab.status_text.text(), "Disconnected")
            self.assertEqual(client.closes, 1)

    def test_wmi_restart_can_be_cancelled_without_dispatch(self):
        from remoteops.core.models import ConnectionMode

        self.host.connection_mode = ConnectionMode.WMI
        with patch("remoteops.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.StandardButton.No) as confirm, \
                patch.object(self.window, "start_worker") as worker, \
                patch.object(self.window, "resolve_credential_for_profile") as credential:
            self.window.run_host_command(self.host, "restart-service Spooler", self.tab.terminal)
            confirm.assert_called_once()
            worker.assert_not_called()
            credential.assert_not_called()
        self.assertIn("restart cancelled", self.tab.terminal.toPlainText())
