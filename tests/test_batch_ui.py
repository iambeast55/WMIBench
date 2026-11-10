import csv
import os
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontDatabase
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, ConnectionMode, CredentialProfile, Host, SessionState
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.batch_dialog import BatchDialog
from remoteops.ui.main_window import MainWindow
from remoteops.ui.theme import stylesheet
from test_batch import TrackingSession
from test_wmi_definitions import bios_template


class BatchUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if os.environ.get("REMOTEOPS_CAPTURE_DIR") and font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "history.db")
        self.hosts = [self.repository.save_host(Host(f"server-{index}", f"192.0.2.{index + 1}")) for index in range(3)]
        self.hosts[1].connection_mode = ConnectionMode.WMI
        self.repository.save_host(self.hosts[1])
        self.manager = SessionManager(repository=self.repository, session_factory=TrackingSession)
        self.window = MainWindow(self.repository, self.manager)
        self.window.resize(1200, 780)
        self.dialog = BatchDialog(self.window)
        self.window.batch_dialog = self.dialog
        self.dialog.show()
        self.app.processEvents()

    def tearDown(self):
        if self.dialog.runner:
            self.dialog.runner.cancel()
        self.window.thread_pool.waitForDone(6000)
        self.app.processEvents()
        self.dialog.close()
        self.window.batch_dialog = None
        self.window.close()
        self.window.thread_pool.waitForDone(6000)
        self.app.processEvents()
        self.dialog.deleteLater()
        self.window.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def select_all(self):
        self.dialog.check_visible(True)

    def wait(self):
        deadline = time.monotonic() + 6
        while self.dialog.running and time.monotonic() < deadline:
            QTest.qWait(10)
        self.assertFalse(self.dialog.running)

    def run_approved(self):
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.start_batch()
        self.wait()

    def test_connected_mixed_batch_results_history_combined_and_export(self):
        sessions = [self.manager.connect(host) for host in self.hosts]
        sessions[1].failure = "Access denied"
        self.select_all()
        with patch.object(self.window, "resolve_credential_for_profile") as credentials:
            self.run_approved()
            credentials.assert_not_called()
        self.assertEqual([self.dialog.updates[host.id].status for host in self.hosts], ["Succeeded", "Failed", "Succeeded"])
        self.assertTrue(all(session.connections == 1 for session in sessions))
        self.assertEqual(len(self.repository.list_command_history()), 3)
        self.assertTrue(self.dialog.export_button.isEnabled())
        self.dialog.pages.setCurrentIndex(2)
        self.assertEqual(self.dialog.combined.proxy.rowCount(), 3)
        self.dialog.combined.search.setText("server-1")
        self.assertEqual(self.dialog.combined.proxy.rowCount(), 1)
        path = Path(self.temp.name) / "batch.csv"
        with patch("remoteops.ui.batch_dialog.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            self.dialog.export_all()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 3)
        self.assertIn("Access denied", rows[1]["Error"])
        self.dialog.pages.setCurrentIndex(1)
        row = next(row for row in range(self.dialog.summary.rowCount()) if self.dialog.summary.item(row, 0).text() == "server-1")
        self.dialog.summary.selectRow(row)
        self.assertIn("Access denied", self.dialog.viewer.text.toPlainText())
        self.assertIn("access denied", self.dialog.summary.item(row, 4).text().lower())
        self.dialog.summary.sortItems(1, Qt.SortOrder.AscendingOrder)
        self.dialog.summary_search.setText("server-1")
        visible = [self.dialog.summary.item(row, 0).text() for row in range(self.dialog.summary.rowCount())
                   if not self.dialog.summary.isRowHidden(row)]
        self.assertEqual(visible, ["server-1"])
        self.dialog.summary_search.clear()
        self.dialog.refresh_preflight()
        self.assertIn("2 succeeded", self.dialog.status.text())
        self.assertTrue(all(session.state is SessionState.CONNECTED for session in sessions))
        self.assertIn("wmibench [batch]> sysinfo", self.window.host_tabs_by_id[self.hosts[0].id].terminal.toPlainText())
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            for dark in (False, True):
                self.dialog.setStyleSheet(stylesheet(dark))
                for page in (1, 2):
                    self.dialog.pages.setCurrentIndex(page)
                    for width, height in ((1100, 740), (820, 560)):
                        self.dialog.resize(width, height)
                        self.app.processEvents()
                        self.assertLessEqual(self.dialog.width(), width)
                        self.assertLessEqual(self.dialog.height(), height)
                        self.assertTrue(self.dialog.rect().contains(self.dialog.run_button.mapTo(self.dialog, self.dialog.run_button.rect().bottomRight())))
                        self.dialog.grab().save(str(target / f"batch-results-{'dark' if dark else 'light'}-{page}-{width}.png"))

    def test_display_limit_does_not_limit_full_csv(self):
        for host in self.hosts:
            self.manager.connect(host)
        self.select_all()
        self.run_approved()
        self.dialog.DISPLAY_LIMIT = 1
        self.dialog.pages.setCurrentIndex(2)
        self.assertEqual(self.dialog.combined.proxy.rowCount(), 1)
        self.assertIn("1 of 3", self.dialog.combined_count.text())
        path = Path(self.temp.name) / "all.csv"
        with patch("remoteops.ui.batch_dialog.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            self.dialog.export_all()
        with path.open(encoding="utf-8-sig", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 3)

    def test_new_connections_require_checkbox_and_confirmation(self):
        self.select_all()
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.dialog.connect_new.setChecked(True)
        self.assertTrue(self.dialog.run_button.isEnabled())
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.dialog.start_batch()
        self.assertEqual(self.manager.states(), {})
        self.run_approved()
        self.assertEqual(len(self.manager.states()), 3)
        self.assertEqual(len(self.repository.list_command_history()), 3)

    def test_credential_is_resolved_once_on_ui_thread_and_cancel_skips(self):
        profile = self.repository.save_credential_profile(CredentialProfile("Shared", "fixture", method=AuthMethod.NTLM))
        self.dialog.profiles[profile.id] = profile
        for host in self.dialog.hosts:
            host.auth_profile_id = profile.id
        self.dialog.connect_new.setChecked(True)
        self.select_all()
        thread_ids = []

        def resolve(profile):
            thread_ids.append(threading.get_ident())
            return None

        with patch.object(self.window, "resolve_credential_for_profile", side_effect=resolve) as resolver:
            self.run_approved()
            resolver.assert_called_once()
        self.assertEqual(thread_ids, [threading.get_ident()])
        self.assertEqual(self.manager.states(), {})
        self.assertTrue(all(update.status == "Skipped" for update in self.dialog.updates.values()))
        with patch.object(self.window, "resolve_credential_for_profile", return_value=ResolvedCredential("fixture", "fixture-secret")) as resolver:
            self.run_approved()
            resolver.assert_called_once()
        self.assertEqual(len(self.manager.states()), 3)
        self.assertNotIn("fixture-secret", self.dialog.viewer.text.toPlainText())

    def test_unsupported_mutating_and_busy_hosts_flagged_before_dispatch(self):
        sessions = [self.manager.connect(host) for host in self.hosts]
        self.select_all()
        self.dialog.command.setCurrentText("restart-service Spooler")
        self.dialog.refresh_preflight()
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.assertTrue(all(not session.calls for session in sessions))
        query = bios_template()
        self.manager.registry.upsert(query)
        self.dialog.templates.append(query)
        self.dialog.command.setCurrentText("bios")
        self.dialog.refresh_preflight()
        self.assertIn("1 ready", self.dialog.status.text())
        self.run_approved()
        self.assertEqual([self.dialog.updates[host.id].status for host in self.hosts], ["Skipped", "Succeeded", "Skipped"])
        self.window.host_tabs_by_id[self.hosts[1].id].set_busy(True)
        self.dialog.refresh_preflight()
        self.assertFalse(self.dialog.run_button.isEnabled())
        self.window.host_tabs_by_id[self.hosts[1].id].set_busy(False)

    def test_cancel_and_close_wait_for_dispatched_host_without_disconnect(self):
        sessions = [self.manager.connect(host) for host in self.hosts]
        gate = sessions[0].gate = threading.Event()
        self.dialog.concurrency.setValue(1)
        self.select_all()
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.dialog.start_batch()
        try:
            deadline = time.monotonic() + 3
            while not sessions[0].calls and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertEqual(len(sessions[0].calls), 1)
            self.dialog.reject()
            self.assertTrue(self.dialog.running)
            self.assertTrue(self.dialog.isVisible())
            self.window.close()
            self.assertFalse(self.window.closing)
        finally:
            gate.set()
        self.wait()
        self.assertEqual([self.dialog.updates[host.id].status for host in self.hosts], ["Succeeded", "Skipped", "Skipped"])
        self.assertTrue(all(session.state is SessionState.CONNECTED for session in sessions))

    def test_definition_change_is_rechecked_before_approval(self):
        self.manager.connect(self.hosts[0])
        changed = deepcopy(self.manager.registry.aliases()["sysinfo"])
        changed.script = "Stop-Computer"
        self.manager.registry.upsert(changed)
        with patch.object(QMessageBox, "question") as approval, patch.object(QMessageBox, "information"):
            self.dialog.start_batch()
        approval.assert_not_called()
        self.assertFalse(self.dialog.running)
        self.assertEqual(self.repository.list_command_history(), [])

    def test_filters_keep_selection_and_layout_capture(self):
        self.dialog.connect_new.setChecked(True)
        self.dialog.host_search.setText("server-1")
        self.dialog.check_visible(True)
        self.assertEqual({host.name for host in self.dialog.selected_hosts()}, {"server-0", "server-1"})
        self.dialog.host_search.clear()
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        for dark in (False, True):
            self.dialog.setStyleSheet(stylesheet(dark))
            for width, height in ((1100, 740), (820, 560)):
                self.dialog.resize(width, height)
                self.app.processEvents()
                self.assertLessEqual(self.dialog.width(), width)
                self.assertLessEqual(self.dialog.height(), height)
                for widget in (self.dialog.command, self.dialog.host_table, self.dialog.run_button, self.dialog.close_button):
                    self.assertTrue(self.dialog.rect().contains(widget.mapTo(self.dialog, widget.rect().bottomRight())))
                if folder:
                    target = Path(folder)
                    target.mkdir(parents=True, exist_ok=True)
                    self.dialog.grab().save(str(target / f"batch-hosts-{'dark' if dark else 'light'}-{width}.png"))
