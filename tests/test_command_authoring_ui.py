import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QMessageBox
from PySide6.QtGui import QFontDatabase
from remoteops.commands.templates import CommandRegistry, prepare_command
from remoteops.ui.main_window import CommandLibraryDialog, CommandTemplateDialog
from remoteops.ui.theme import stylesheet
from test_wmi_definitions import EXAMPLES, bios_template


class CommandAuthoringUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if os.environ.get("REMOTEOPS_CAPTURE_DIR") and font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CommandRegistry(Path(self.temp.name) / "commands.json")

    def dialog(self, template=None):
        dialog = CommandTemplateDialog(template)
        self.addCleanup(dialog.close)
        return dialog

    def test_bios_preset_saves_and_previews_without_remote_calls(self):
        dialog = self.dialog()
        dialog.name.setText("BIOS inventory")
        dialog.alias.setText("bios")
        dialog.wmi_enabled.setChecked(True)
        dialog.apply_wmi_preset(1)
        with patch("remoteops.remoting.dcom_client.DcomClient.connect") as connect:
            dialog.validate_preview()
            dialog.accept()
            self.registry.upsert(dialog.template())
            connect.assert_not_called()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.validation_status.text(), "Definition valid")
        self.assertIn("FROM Win32_BIOS", dialog.preview.toPlainText())
        prepared = self.registry.prepare("bios")
        self.assertIsNotNone(prepared.wmi_plan)
        self.assertFalse(prepared.script)

    def test_invalid_editor_input_leaves_original_definition_untouched(self):
        template = bios_template()
        self.registry.upsert(template)
        original = self.registry.path.read_bytes()
        dialog = self.dialog(template)
        dialog.wmi_class.setText("Win32_BIOS; DROP")
        dialog.validate_preview()
        self.assertEqual(dialog.validation_status.text(), "Invalid definition")
        self.assertIn("Invalid WMI identifier", dialog.preview.toPlainText())
        with patch("remoteops.ui.main_window.inline_notice") as warning:
            dialog.accept()
            warning.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(template.wmi["class"], "Win32_BIOS")
        self.assertEqual(self.registry.path.read_bytes(), original)

    def test_service_preset_keeps_typed_parameters_and_rejects_invalid_json(self):
        dialog = self.dialog()
        dialog.name.setText("Service status")
        dialog.alias.setText("my-service")
        dialog.wmi_enabled.setChecked(True)
        dialog.apply_wmi_preset(2)
        prepared = prepare_command("my-service Schedule", [dialog.template()])
        self.assertEqual(prepared.wmi_plan.query.where, "Name='Schedule'")
        dialog.wmi_filters.setPlainText("not JSON")
        with patch("remoteops.ui.main_window.inline_notice") as warning:
            dialog.accept()
            warning.assert_called_once()

    def test_library_import_export_duplicate_and_conflict_are_local(self):
        library = CommandLibraryDialog(self.registry)
        self.addCleanup(library.close)
        with patch("remoteops.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(EXAMPLES / "bios.json"), "")):
            library.import_commands()
        row = next(index for index in range(library.table.rowCount()) if library.table.item(index, 0).text() == "bios")
        self.assertEqual(library.table.item(row, 2).text(), "WMI/DCOM")
        library.table.selectRow(row)
        export = Path(self.temp.name) / "bios-export.json"
        with patch("remoteops.ui.main_window.QFileDialog.getSaveFileName", return_value=(str(export), "")):
            library.export_selected()
        self.assertEqual(len(json.loads(export.read_text())["commands"]), 1)
        with patch.object(CommandTemplateDialog, "exec", return_value=QDialog.DialogCode.Accepted):
            library.duplicate_selected()
        self.assertIsNotNone(self.registry.prepare("bios-copy").wmi_plan)
        before = self.registry.path.read_bytes()
        with patch("remoteops.ui.main_window.QFileDialog.getOpenFileName", return_value=(str(export), "")), \
                patch("remoteops.ui.main_window.inline_notice") as warning:
            library.import_commands()
            warning.assert_called_once()
        self.assertEqual(self.registry.path.read_bytes(), before)

    def test_editor_layout_and_optional_visual_capture(self):
        dialog = self.dialog(bios_template())
        dialog.validate_preview()
        dialog.setStyleSheet(stylesheet(False))
        dialog.show()
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), 760)
        buttons = dialog.findChild(QDialogButtonBox)
        for widget in (dialog.wmi_properties, dialog.wmi_filters, dialog.preview, buttons):
            top = widget.mapTo(dialog, widget.rect().topLeft())
            bottom = widget.mapTo(dialog, widget.rect().bottomRight())
            self.assertTrue(dialog.rect().contains(top))
            self.assertTrue(dialog.rect().contains(bottom))
        dialog.resize(700, 650)
        self.app.processEvents()
        self.assertLessEqual(dialog.height(), 650)
        self.assertTrue(dialog.rect().contains(buttons.mapTo(dialog, buttons.rect().bottomRight())))
        dialog.resize(820, 760)
        self.app.processEvents()
        folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
        if folder:
            target = Path(folder)
            target.mkdir(parents=True, exist_ok=True)
            dialog.grab().save(str(target / "wmi-command-editor-light.png"))
        dialog.setStyleSheet(stylesheet(True))
        self.app.processEvents()
        page = dialog.wmi_scroll.widget()
        pixel = page.grab().toImage().pixelColor(page.width() - 5, 5)
        self.assertLess(pixel.lightness(), 80)
        if folder:
            dialog.grab().save(str(target / "wmi-command-editor-dark.png"))
