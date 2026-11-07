import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QFontDatabase, QIcon
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QInputDialog, QMessageBox, QStackedWidget

from remoteops.core.credentials import CredentialStore
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CommandResult, CredentialProfile, Host, SessionState, utc_now
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.main_window import AuthProfilesDialog, CredentialDialog, MainWindow, append_terminal
from test_inspection_ui import InspectionSession


class WorkspaceFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = Repository(Path(self.temp.name) / "test.sqlite3")
        self.host = self.repository.save_host(Host("lab-server-01", "192.0.2.1"))
        self.other = self.repository.save_host(Host("lab-server-02", "192.0.2.2"))
        self.manager = SessionManager(repository=self.repository, session_factory=InspectionSession)
        self.window = MainWindow(self.repository, self.manager)
        self.window.resize(1200, 800)
        self.window.show()
        self.window.select_host(self.host.id)
        self.tab = self.window.host_tabs_by_id[self.host.id]
        QTest.qWait(30)

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

    def credential(self):
        profile = CredentialProfile("Lab login", "LAB\\user", method=AuthMethod.NTLM, prompt_for_secret=False)
        self.manager.credential_store.save_profile(profile, "fixture-secret")
        self.host.auth_profile_id = profile.id
        self.repository.save_host(self.host)
        self.window.reload_data()
        return profile

    def test_delete_host_disconnects_and_keeps_other_workspace_history_and_credentials(self):
        profile = self.credential()
        self.repository.save_command_result(CommandResult(self.host.id, self.host.name, "sysinfo"))
        self.manager.connect(self.host)
        other_tab = self.window.host_tabs_by_id[self.other.id]
        append_terminal(other_tab.terminal, "keep this output")
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            self.window.delete_host(self.host)
            self.wait()
        self.assertIsNone(self.repository.get_host(self.host.id))
        self.assertEqual(self.window.current_host_id(), self.other.id)
        self.assertIs(self.window.host_tabs_by_id[self.other.id], other_tab)
        self.assertIn("keep this output", other_tab.terminal.toPlainText())
        self.assertIsNotNone(self.repository.get_credential_profile(profile.id))
        self.assertEqual(len(self.repository.list_command_history()), 1)

    def test_credential_form_has_five_modes_and_domain_settings(self):
        dialog = CredentialDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.method.count(), 5)
        dialog.method.setCurrentIndex(dialog.method.findData(AuthMethod.KERBEROS_AES))
        self.assertTrue(dialog.domain_controller.isEnabled())
        dialog.name.setText("key")
        dialog.username.setText("user@example.com")
        dialog.domain.setText("example.com")
        dialog.domain_controller.setText("192.0.2.1")
        dialog.password.setText("short")
        dialog.accept()
        self.assertIn("32 or 64", dialog.validation.text())
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        dialog.password.setText("a" * 64)
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        profile = self.repository.get_credential_profile(dialog.profile().id)
        self.assertEqual(profile.method, AuthMethod.KERBEROS_AES)
        self.assertEqual(profile.domain_controller, "192.0.2.1")

    def test_saved_credentials_resolve_without_typing_after_reopen(self):
        profile = self.credential()
        self.manager.credential_store = CredentialStore(Repository(self.repository.db_path))
        self.manager.credential_store._keyring = None
        with patch.object(QInputDialog, "getText") as prompt:
            self.tab.connect_button.click()
            self.wait()
            self.tab.disconnect_button.click()
            self.wait()
            self.tab.connect_button.click()
            self.wait()
            prompt.assert_not_called()
        self.assertEqual(self.manager.credential_store.resolve(profile).secret, "fixture-secret")
        self.assertEqual(self.tab.status_text.text(), "Connected")
        self.assertNotIn("fixture-secret", self.tab.terminal.toPlainText())

    def test_tools_dispatch_completion_and_recoverable_error_keep_connection(self):
        self.assertEqual(self.window.windowTitle(), "WMIBench")
        self.tab.connect_button.click()
        self.wait()
        self.tab.view_tabs.setCurrentIndex(4)
        panel = self.tab.tools_panel
        panel.mode.setCurrentIndex(2)
        result = CommandResult(self.host.id, self.host.name, "tools processes", exit_code=0,
                               records=[{"Name": "example.exe", "ProcessId": 42, "CreationDate": "date"}])
        with patch.object(self.manager, "run_tool", return_value=result) as run:
            panel.refresh()
            self.assertTrue(self.tab.busy)
            self.wait()
            run.assert_called_once()
        self.assertEqual(len(panel.records), 1)
        self.assertFalse(self.tab.busy)
        result = CommandResult(self.host.id, self.host.name, "tools processes", exit_code=1, stderr="Access denied")
        with patch.object(self.manager, "run_tool", return_value=result):
            panel.refresh()
            self.wait()
        self.assertEqual(self.tab.status_text.text(), "Connected")
        self.assertIn("permission", panel.status.text())
        self.assertTrue(panel.refresh_button.isEnabled())
        capture = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        if capture:
            folder = Path(capture)
            folder.mkdir(parents=True, exist_ok=True)
            self.window.grab().save(str(folder / "tools-full-window.png"))

    def test_credential_edit_retains_id_password_and_assignments(self):
        profile = self.credential()
        dialog = CredentialDialog(self.window, profile=profile)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.password.text(), "")
        dialog.name.setText("Renamed login")
        dialog.accept()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        saved = self.repository.get_credential_profile(profile.id)
        self.assertEqual(saved.name, "Renamed login")
        self.assertEqual(self.repository.get_host(self.host.id).auth_profile_id, profile.id)
        self.assertEqual(self.manager.credential_store.resolve(saved).secret, "fixture-secret")
        dialog = CredentialDialog(self.window, profile=saved)
        self.addCleanup(dialog.deleteLater)
        dialog.save_password.setChecked(False)
        dialog.accept()
        self.assertIsNone(self.repository.get_encrypted_secret(profile.id))
        self.assertTrue(self.repository.get_credential_profile(profile.id).prompt_for_secret)

    def test_invalid_and_cancelled_credentials_do_not_save(self):
        dialog = CredentialDialog(self.window)
        self.addCleanup(dialog.deleteLater)
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.name.setText("Draft")
        dialog.username.setText("user")
        dialog.accept()
        self.assertIn("Enter a password", dialog.validation.text())
        self.assertEqual(self.repository.list_credential_profiles(), [])
        dialog.password.setText("cancelled-secret")
        dialog.reject()
        self.assertEqual(self.repository.list_credential_profiles(), [])

    def test_credential_manager_edit_delete_controls_and_live_guard(self):
        profile = self.credential()
        dialog = AuthProfilesDialog(self.window.profiles, self.window)
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.table.item(0, 3).text(), "Saved in database")
        with patch.object(self.window, "edit_profile") as edit:
            dialog.edit_button.click()
            edit.assert_called_once()
        self.manager.connect(self.host)
        with patch("remoteops.ui.main_window.inline_notice") as message, patch.object(QMessageBox, "question") as confirm:
            self.window.delete_profile(profile)
            self.window.edit_profile(profile)
            self.assertEqual(message.call_count, 2)
            confirm.assert_not_called()
        self.manager.disconnect(self.host)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            self.window.delete_profile(profile)
        self.assertIsNotNone(self.repository.get_credential_profile(profile.id))
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes):
            dialog.delete_button.click()
        self.assertEqual(dialog.table.rowCount(), 0)
        self.assertFalse(dialog.edit_button.isEnabled())
        self.assertIsNone(self.repository.get_host(self.host.id).auth_profile_id)

    def test_context_menu_targets_clicked_host_and_obeys_busy_state(self):
        self.assertIsInstance(self.window.host_tabs, QStackedWidget)
        with patch.object(self.window, "host_context_menu") as factory:
            position = self.window.host_tree.visualItemRect(self.window.host_tree.host_items[self.other.id]).center()
            self.window.show_host_menu(position)
            factory.assert_called_once()
            factory.return_value.exec.assert_called_once()
        self.assertEqual(self.window.current_host_id(), self.other.id)
        menu = self.window.host_context_menu(self.other)
        self.addCleanup(menu.deleteLater)
        connect, disconnect = menu.actions()[:2]
        self.assertTrue(connect.isEnabled())
        self.assertFalse(disconnect.isEnabled())
        connect.trigger()
        self.wait()
        self.assertEqual(self.manager.session_state(self.other), SessionState.CONNECTED)
        self.assertEqual(self.manager.session_state(self.host), SessionState.DISCONNECTED)
        self.window.host_tabs_by_id[self.other.id].set_busy(True)
        busy_menu = self.window.host_context_menu(self.other)
        self.addCleanup(busy_menu.deleteLater)
        self.assertFalse(busy_menu.actions()[0].isEnabled())
        self.assertFalse(busy_menu.actions()[1].isEnabled())
        self.assertFalse(next(action for action in busy_menu.actions() if action.text() == "Delete host").isEnabled())
        self.window.host_tabs_by_id[self.other.id].set_busy(False)
        connected_menu = self.window.host_context_menu(self.other)
        self.addCleanup(connected_menu.deleteLater)
        self.assertFalse(connected_menu.actions()[0].isEnabled())
        connected_menu.actions()[1].trigger()
        self.wait()
        self.assertEqual(self.manager.session_state(self.other), SessionState.DISCONNECTED)

    def test_switching_hosts_preserves_terminal_and_selected_identity(self):
        append_terminal(self.tab.terminal, "first host output")
        self.window.select_host(self.other.id)
        self.assertEqual(self.window.current_host_id(), self.other.id)
        self.window.select_host(self.host.id)
        self.assertIn("first host output", self.tab.terminal.toPlainText())
        self.assertFalse(hasattr(self.tab, "host_title"))
        self.assertEqual(self.window.host_tree.selected_host_id(), self.host.id)

    def test_icons_have_visible_theme_contrast(self):
        for dark in (False, True):
            self.window.set_dark_mode(dark)
            self.app.processEvents()
            image = self.tab.wrap_button.icon().pixmap(24, 24, QIcon.Mode.Normal).toImage()
            colors = [image.pixelColor(x, y) for x in range(image.width()) for y in range(image.height())
                      if image.pixelColor(x, y).alpha() > 220]
            self.assertGreater(len(colors), 20)
            brightness = sum(c.lightnessF() for c in colors) / len(colors)
            self.assertGreater(brightness, 0.8) if dark else self.assertLess(brightness, 0.3)

    def test_expanded_views_and_readable_history_layout(self):
        for index in range(8):
            self.repository.save_command_result(CommandResult(self.host.id, self.host.name, "fixture",
                command_line=f"events --log Application --count {index + 1}",
                stdout="Provider       ID    Message\nSample         42    A readable event message",
                exit_code=0, completed_at=utc_now()))
        self.tab.services.refresh()
        self.wait()
        self.tab.events.refresh()
        self.wait()
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        for dark in (False, True):
            self.window.set_dark_mode(dark)
            for width, height in ((1200, 800), (820, 560)):
                self.window.resize(width, height)
                for index, name in ((0, "terminal"), (1, "history"), (2, "services"), (3, "events")):
                    self.tab.view_tabs.setCurrentIndex(index)
                    QTest.qWait(30)
                    self.assertEqual((self.window.width(), self.window.height()), (width, height))
                    self.assertLess(self.tab.output_stack.mapTo(self.window, self.tab.output_stack.rect().topLeft()).y(), 230)
                    if index == 0:
                        self.assertGreaterEqual(self.tab.terminal.height(), 250)
                    if index == 1:
                        history = self.tab.history
                        self.assertTrue(history.list.isColumnHidden(1))
                        self.assertGreaterEqual(history.list.columnWidth(2), 160)
                        self.assertLess(history.list.geometry().bottom(), history.viewer.geometry().top())
                        self.assertTrue(history.rect().contains(history.viewer.mapTo(history, history.viewer.rect().bottomRight())))
                    if folder:
                        path = Path(folder)
                        path.mkdir(parents=True, exist_ok=True)
                        self.window.grab().save(str(path / f"workspace-{name}-{'dark' if dark else 'light'}-{width}.png"))
        self.window.resize(1200, 800)
        self.tab.view_tabs.setCurrentIndex(3)
        self.tab.events.table.table.selectRow(0)
        self.tab.events.details_button.click()
        QTest.qWait(30)
        self.assertTrue(self.tab.events.details_dialog.isVisible())
        self.assertGreaterEqual(self.tab.events.details.height(), 400)
        if folder:
            self.tab.events.details_dialog.grab().save(str(Path(folder) / "event-details.png"))
        self.window.select_host(self.other.id)
        self.assertFalse(self.tab.events.details_dialog.isVisible())

    def test_credential_dialog_capture(self):
        profile = self.credential()
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        for dark in (False, True):
            self.window.set_dark_mode(dark)
            for dialog, name in ((AuthProfilesDialog(self.window.profiles, self.window), "credentials"),
                                 (CredentialDialog(self.window, profile=profile), "edit-credential")):
                dialog.show()
                QTest.qWait(30)
                if folder:
                    path = Path(folder)
                    path.mkdir(parents=True, exist_ok=True)
                    dialog.grab().save(str(path / f"{name}-{'dark' if dark else 'light'}.png"))
                dialog.close()
                dialog.deleteLater()
