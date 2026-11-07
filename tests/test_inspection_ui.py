import os
import tempfile
import time
import unittest
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from remoteops.commands.templates import prepare_command
from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, ConnectionMode, Host, SessionState, utc_now
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.main_window import MainWindow
from remoteops.ui.batch_dialog import BatchDialog
from remoteops.ui.inspection import EventPanel
from test_batch import TrackingSession


class InspectionSession(TrackingSession):
    def run_prepared(self, command, timeout_seconds=120):
        result = super().run_prepared(command, timeout_seconds)
        if result.stderr:
            return result
        if command.alias in ("services", "svc", "restart-service"):
            result.records = [{"Name": "Spooler", "DisplayName": "Print service", "State": "Running", "StartMode": "Auto"}]
            if command.alias == "services":
                result.records.append({"Name": "Other", "DisplayName": "Other service", "State": "Stopped", "StartMode": "Manual"})
        elif command.alias == "events":
            result.records = [
                {"TimeCreated": utc_now().isoformat(), "Id": 123, "ProviderName": "Fixture", "LevelDisplayName": "Error", "Message": "<literal> & message\nFull second line"},
                {"TimeGenerated": (utc_now() - timedelta(days=3)).strftime("%Y%m%d%H%M%S.000000+000"), "EventCode": 7, "SourceName": "OldProvider", "EventType": 3, "Message": "Older event"},
            ]
        return result


class InspectionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "history.db")
        self.host = self.repository.save_host(Host("Fixture", "192.0.2.1"))
        self.manager = SessionManager(repository=self.repository, session_factory=InspectionSession)
        self.window = MainWindow(self.repository, self.manager)
        self.window.resize(1200, 800)
        self.window.show()
        self.tab = self.window.host_tabs_by_id[self.host.id]
        self.app.processEvents()

    def tearDown(self):
        self.window.thread_pool.waitForDone(5000)
        self.app.processEvents()
        self.window.close()
        self.window.thread_pool.waitForDone(5000)
        self.app.processEvents()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def wait(self):
        deadline = time.monotonic() + 5
        while self.window.pending_operations and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.window.pending_operations)

    def selected_service(self):
        panel = self.tab.services
        panel.table.table.selectRow(0)
        self.app.processEvents()
        return panel

    def test_opening_views_does_not_connect_and_refresh_reuses_session(self):
        for index in (2, 3, 2):
            self.tab.view_tabs.setCurrentIndex(index)
        self.assertEqual(self.manager.states(), {})
        self.tab.services.refresh_button.click()
        self.wait()
        session = self.manager.connect(self.host)
        self.assertEqual(session.connections, 1)
        self.assertEqual([call.alias for call in session.calls], ["services"])
        self.assertEqual(self.tab.services.table.proxy.rowCount(), 2)
        self.assertEqual(self.tab.status_text.text(), "Connected")
        self.tab.services.state_filter.setCurrentText("Stopped")
        self.assertEqual(self.tab.services.table.proxy.rowCount(), 1)
        self.selected_service().inspect_button.click()
        self.wait()
        self.assertEqual(session.calls[-1].values["service_name"], "Other")
        self.assertEqual(session.connections, 1)
        self.assertEqual(len(self.repository.list_command_history()), 2)

    def test_restart_cancel_then_success_refreshes_without_replay(self):
        self.tab.services.refresh()
        self.wait()
        panel = self.selected_service()
        session = self.manager.connect(self.host)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            panel.restart_button.click()
        self.assertEqual([call.alias for call in session.calls], ["services"])
        self.assertFalse(panel.restart_button.isEnabled())
        panel.refresh()
        self.wait()
        self.selected_service()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm:
            panel.restart_button.click()
            self.wait()
            confirm.assert_called_once()
        self.assertEqual([call.alias for call in session.calls], ["services", "services", "restart-service", "services"])
        self.assertEqual(session.connections, 1)
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertTrue(panel.fresh)

    def test_command_error_preserves_connection_and_disables_stale_action(self):
        self.tab.services.refresh()
        self.wait()
        panel = self.selected_service()
        session = self.manager.connect(self.host)
        session.failure = "Access denied"
        panel.refresh()
        self.wait()
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertEqual(len(panel.records), 2)
        self.assertIn("Previous results retained", panel.status.text())
        self.assertIn("Access denied", panel.details.toPlainText())
        self.assertFalse(panel.restart_button.isEnabled())
        session.failure = None
        panel.refresh()
        self.wait()
        self.selected_service()
        self.assertTrue(panel.restart_button.isEnabled())

    def test_restart_uses_snapshot_when_library_changes_during_confirmation(self):
        self.tab.services.refresh()
        self.wait()
        panel = self.selected_service()
        original = deepcopy(self.window.command_registry.aliases()["restart-service"])

        def change_library(*_args):
            altered = deepcopy(original)
            altered.script = "Write-Output 'different command'"
            self.window.command_registry.upsert(altered)
            return QMessageBox.StandardButton.Yes

        with patch.object(QMessageBox, "question", side_effect=change_library):
            panel.restart_button.click()
            self.wait()
        session = self.manager.connect(self.host)
        restart = next(call for call in session.calls if call.alias == "restart-service")
        expected = prepare_command(restart.command_line, [original])
        self.assertEqual(restart.script, expected.script)
        self.assertEqual([call.alias for call in session.calls], ["services", "restart-service", "services"])

    def test_wmi_restart_failure_is_not_retried_and_connection_survives(self):
        self.host.connection_mode = ConnectionMode.WMI
        self.repository.save_host(self.host)
        self.window.reload_data()
        self.tab.services.refresh()
        self.wait()
        panel = self.selected_service()
        session = self.manager.connect(self.host)
        session.failure = "Service restart outcome unknown"
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes) as confirm:
            panel.restart_button.click()
            self.wait()
        confirm.assert_called_once()
        self.assertIn("not be force-stopped", confirm.call_args.args[2])
        self.assertEqual([call.alias for call in session.calls], ["services", "restart-service"])
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.assertFalse(panel.restart_button.isEnabled())
        self.assertIn("outcome unknown", panel.details.toPlainText())

    def test_host_changes_invalidate_loaded_rows_and_actions(self):
        self.tab.services.refresh()
        self.wait()
        self.selected_service()
        self.tab.host.address = "192.0.2.2"
        self.tab.refresh()
        self.assertFalse(self.tab.services.restart_button.isEnabled())
        self.assertEqual(self.tab.services.records, [])
        self.assertEqual(self.tab.services.status.text(), "Not loaded")

    def test_modified_command_is_not_executed_by_service_view(self):
        altered = deepcopy(self.window.command_registry.aliases()["services"])
        altered.script = "Stop-Computer"
        self.window.command_registry.upsert(altered)
        with patch.object(self.window, "start_worker") as worker:
            self.tab.services.refresh()
            worker.assert_not_called()
        self.assertIn("differs from the built-in", self.tab.services.details.toPlainText())
        self.assertEqual(self.manager.states(), {})

    def test_event_filters_and_full_message_are_local_only(self):
        panel = self.tab.events
        panel.log.setCurrentText("Application")
        panel.count.setValue(5)
        panel.refresh()
        self.wait()
        session = self.manager.connect(self.host)
        self.assertEqual(session.calls[0].values, {"log": "Application", "count": 5})
        self.assertEqual(panel.table.proxy.rowCount(), 2)
        panel.provider.setText("fixture")
        self.assertEqual(panel.table.proxy.rowCount(), 1)
        panel.table.table.selectRow(0)
        self.assertIn("<literal> & message\nFull second line", panel.details.toPlainText())
        panel.event_id.setText("007")
        self.assertEqual(panel.table.proxy.rowCount(), 0)
        panel.provider.clear()
        self.assertEqual(panel.table.proxy.rowCount(), 1)
        panel.event_id.clear()
        panel.age.setCurrentIndex(1)
        self.assertEqual(panel.table.proxy.rowCount(), 1)
        panel.event_id.setText("bad")
        self.assertEqual(panel.table.proxy.rowCount(), 0)
        self.assertIn("integer", panel.status.text())
        self.assertEqual(len(session.calls), 1)

    def test_timestamp_handles_iso_cim_missing_and_naive_values(self):
        self.assertIsNotNone(EventPanel.timestamp({"TimeCreated": utc_now().isoformat()}))
        self.assertIsNotNone(EventPanel.timestamp({"TimeGenerated": "20261016080000.000000-240"}))
        for value in (None, "bad", "2026-10-16T08:00:00", "20269999880000.000000+000"):
            self.assertIsNone(EventPanel.timestamp({"TimeCreated": value}))

    def test_host_and_batch_accept_typed_arguments_without_form_controls(self):
        with patch.object(self.window, "start_worker") as worker:
            self.tab.command_input.setText("events System 10")
            self.assertFalse(hasattr(self.tab, "arguments_button"))
            self.assertEqual(prepare_command(self.tab.command_input.text()).values["count"], 10)
            worker.assert_not_called()
        batch = BatchDialog(self.window)
        try:
            batch.command.setCurrentText("events System 10")
            self.assertFalse(hasattr(batch, "arguments_button"))
            self.assertEqual(prepare_command(batch.command.currentText()).values["count"], 10)
            self.assertFalse(batch.running)
            self.assertEqual(self.manager.states(), {})
        finally:
            batch.close()
            batch.deleteLater()

    def test_service_and_event_layout_capture(self):
        self.tab.services.refresh()
        self.wait()
        self.tab.events.refresh()
        self.wait()
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        for dark in (False, True):
            self.window.set_dark_mode(dark)
            for width, height in ((1200, 800), (820, 560)):
                self.window.resize(width, height)
                for index, name in ((2, "services"), (3, "events")):
                    self.tab.view_tabs.setCurrentIndex(index)
                    QTest.qWait(30)
                    self.app.processEvents()
                    panel = self.tab.services if index == 2 else self.tab.events
                    self.assertLessEqual(self.window.width(), width)
                    self.assertLessEqual(self.window.height(), height)
                    self.assertTrue(self.window.rect().contains(panel.refresh_button.mapTo(self.window, panel.refresh_button.rect().bottomRight())))
                    self.assertGreaterEqual(panel.table.table.viewport().height(), 60)
                    self.assertTrue(self.tab.command_input.isHidden() or not self.tab.command_input.isVisible())
                    self.assertTrue(panel.rect().contains(panel.table.mapTo(panel, panel.table.rect().bottomRight())))
                    if folder:
                        path = Path(folder)
                        path.mkdir(parents=True, exist_ok=True)
                        self.window.grab().save(str(path / f"{name}-{'dark' if dark else 'light'}-{width}.png"))
