import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QLineEdit, QMessageBox, QTabWidget, QTextEdit
from remoteops.commands.templates import CommandRegistry
from remoteops.ui.main_window import CommandLibraryDialog, MainWindow
from remoteops.core.models import Host, ConnectionMode, SessionState
from remoteops.ui.plugin_dialog import PluginDialog
from remoteops.ui.theme import stylesheet


class PluginUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        for name in ("segoeui.ttf", "consola.ttf"):
            font = Path("C:/Windows/Fonts") / name
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CommandRegistry(Path(self.temp.name) / "commands.json")
        self.folder = self.registry.plugins.scaffold("my-bios")
        self.dialog = PluginDialog(self.registry)
        self.addCleanup(self.dialog.close)
        self.dialog.select_folder("my-bios")

    def test_preview_and_validate_do_not_execute(self):
        source = "raise RuntimeError('not executed')\ndef run(ctx, args):\n    return []\n"
        (self.folder / "implementation.py").write_text(source, encoding="utf-8")
        self.dialog.refresh()
        self.assertIn("not executed", self.dialog.details.toPlainText())
        self.assertIsNotNone(self.registry.plugins.active(self.folder))

    def test_added_plugin_is_ready_without_activation_controls(self):
        self.assertEqual(self.dialog.table.item(0, 2).text(), "Ready")
        self.assertIsNotNone(self.registry.plugins.active(self.folder))
        for name in ("Validate", "Enable / reload reviewed version", "Disable"):
            self.assertNotIn(name, self.dialog.buttons)

    def test_use_command_inserts_without_dispatch_or_argument_dialog(self):
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        command_input = QLineEdit()
        tabs = QTabWidget()
        self.addCleanup(command_input.close)
        self.addCleanup(tabs.close)
        tab = SimpleNamespace(command_input=command_input, view_tabs=tabs, busy=False)
        manager = Mock()
        manager.host_busy.return_value = False
        self.dialog.window = SimpleNamespace(
            current_host=lambda: host, host_tabs_by_id={host.id: tab},
            session_manager=manager, start_worker=Mock())
        with patch.object(QMessageBox, "question") as question:
            self.dialog.run_selected()
            question.assert_not_called()
        self.assertEqual(command_input.text(), "my-bios")
        self.dialog.window.start_worker.assert_not_called()
        self.assertEqual(self.dialog.result(), self.dialog.DialogCode.Accepted)

    def test_trusted_mutating_plugin_does_not_repeat_trust_prompt(self):
        import json
        manifest_path = self.folder / "command.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["modifies_remote"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.registry.plugins.enable(self.registry.plugins.validate(self.folder))
        prepared = self.registry.prepare("my-bios")
        self.assertIn("Changes remote system: yes (declared)", self.registry.prepare("my-bios -h").help_text)
        manager = Mock()
        manager.prepare.return_value = prepared
        window = SimpleNamespace(session_manager=manager, start_worker=Mock(), profile_for_host=Mock(),
                                 state_for_host=lambda host: SessionState.CONNECTED, plugin_cancels={},
                                 set_host_state=Mock(), set_host_busy=Mock())
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        terminal = QTextEdit()
        self.addCleanup(terminal.close)
        with patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No) as question:
            self.assertTrue(MainWindow.run_host_command(window, host, "my-bios", terminal))
        question.assert_not_called()
        window.start_worker.assert_called_once()
        manager.run_prepared_with_credential.assert_not_called()

    def test_automatic_reload_and_library_listing(self):
        self.assertEqual(self.dialog.table.item(0, 2).text(), "Ready")
        library = CommandLibraryDialog(self.registry, backend="WMI/DCOM")
        self.addCleanup(library.close)
        aliases = [library.table.item(row, 0).text() for row in range(library.table.rowCount())]
        self.assertIn("my-bios", aliases)
        row = aliases.index("my-bios")
        self.assertIn("Python:", library.table.item(row, 4).text())
        library.table.selectRow(row)
        library.choose_selected()
        self.assertEqual(library.chosen_alias, "my-bios")
        (self.folder / "implementation.py").write_text("broken (", encoding="utf-8")
        self.dialog.refresh()
        self.assertIn("Invalid:", self.dialog.table.item(0, 2).text())
        self.assertIsNone(self.registry.plugins.active(self.folder))

    def test_local_test_process(self):
        with patch.object(QMessageBox, "question") as question:
            self.dialog.test()
            question.assert_not_called()
        deadline = time.monotonic() + 15
        while self.dialog.process is not None and time.monotonic() < deadline:
            QTest.qWait(20)
        if self.dialog.process is not None:
            self.dialog.stop_tests()
            self.dialog.process.waitForFinished(5000)
        self.assertIsNone(self.dialog.process)
        self.assertIn("Exit code: 0", self.dialog.details.toPlainText())

    def test_layout_light_dark_and_small_window(self):
        self.registry.plugins.enable(self.registry.plugins.validate(self.folder))
        self.dialog.refresh()
        for dark in (False, True):
            for width in (820, 1100):
                self.dialog.dark_mode = dark
                self.dialog.setStyleSheet(stylesheet(dark))
                self.dialog.resize(width, 620)
                self.dialog.show()
                QTest.qWait(20)
                buttons = list(self.dialog.buttons.values()) + [self.dialog.run_button]
                for left, right in zip(buttons, buttons[1:]):
                    self.assertFalse(left.geometry().intersects(right.geometry()))
                self.assertGreater(self.dialog.details.height(), 100)
                capture = os.environ.get("REMOTEOPS_CAPTURE_DIR")
                if capture:
                    folder = Path(capture)
                    folder.mkdir(parents=True, exist_ok=True)
                    self.dialog.grab().save(str(folder / f"plugins-{'dark' if dark else 'light'}-{width}.png"))
