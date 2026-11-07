import os
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QCheckBox, QComboBox, QDialog, QLineEdit, QSpinBox
from PySide6.QtGui import QFontDatabase
from remoteops.commands.templates import BUILTIN_TEMPLATES, command_line_from_values, prepare_command
from remoteops.commands.plugins import read_plugin
from remoteops.core.models import CommandTemplate
from remoteops.ui.argument_dialog import ArgumentDialog
from remoteops.ui.theme import stylesheet
from test_wmi_definitions import bios_template


class ArgumentFormsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        font = Path("C:/Windows/Fonts/segoeui.ttf")
        if font.exists():
            QFontDatabase.addApplicationFont(str(font))

    def dialog(self, templates=BUILTIN_TEMPLATES, initial="events", **kwargs):
        dialog = ArgumentDialog(templates, initial, **kwargs)
        self.addCleanup(dialog.close)
        return dialog

    def test_quote_roundtrip_is_literal_including_help_and_equals(self):
        template = CommandTemplate("Text", "Write-Output '$text'", alias="literal", parameters={"text": ""})
        for value in ("", "two words", "a'b\"c", r"DOMAIN\user", "-h", "--help", "a=b", "$(Stop-Computer); & x", "snowman \u2603"):
            with self.subTest(value=value):
                line = command_line_from_values(template, {"text": value})
                command = prepare_command(line, [template])
                self.assertFalse(command.help_text)
                self.assertEqual(command.values["text"], value)
                self.assertEqual(command.script, "Write-Output '" + value.replace("'", "''") + "'")
        for value in ("a\nb", "a\rb", "a\0b"):
            with self.assertRaises(ValueError):
                command_line_from_values(template, {"text": value})

    def test_builtin_fields_defaults_existing_values_and_no_remote_calls(self):
        with patch("remoteops.remoting.dcom_client.DcomClient.connect") as connect:
            dialog = self.dialog(initial="events 'Application' 5")
            self.assertIsInstance(dialog.fields["count"], QSpinBox)
            self.assertEqual(dialog.fields["count"].value(), 5)
            self.assertEqual(dialog.fields["log"].text(), "Application")
            dialog.fields["count"].setValue(25)
            dialog.accept()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
            prepared = prepare_command(dialog.command_line)
            self.assertEqual(prepared.values, {"log": "Application", "count": 25})
            connect.assert_not_called()

    def test_typed_custom_fields_preserve_int64_and_validate_required(self):
        template = CommandTemplate("Typed", "Get-Date", alias="typed", parameters={"_schema": {
            "enabled": {"type": "bool", "default": True},
            "mode": {"type": "str", "choices": ["a", "b"], "default": "b"},
            "big": {"type": "int", "default": 2**63 - 1},
            "ratio": {"type": "float", "default": 0.25, "min": 0, "max": 1},
            "name": {"type": "str", "required": True},
        }})
        dialog = self.dialog([template], "typed")
        self.assertIsInstance(dialog.fields["enabled"], QCheckBox)
        self.assertIsInstance(dialog.fields["mode"], QComboBox)
        self.assertIsInstance(dialog.fields["big"], QLineEdit)
        self.assertEqual(dialog.fields["big"].text(), str(2**63 - 1))
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.fields["name"].setText("admin's \"host\"")
        dialog.fields["enabled"].setChecked(False)
        self.assertTrue(dialog.apply_button.isEnabled())
        values = prepare_command(dialog.command_line, [template]).values
        self.assertEqual(values["big"], 2**63 - 1)
        self.assertFalse(values["enabled"])
        dialog.fields["ratio"].setText("nan")
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.accept()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.fields["ratio"].setText("1.5")
        self.assertFalse(dialog.apply_button.isEnabled())

    def test_backend_and_batch_filters_exclude_mutating_custom_scripts(self):
        query = bios_template()
        custom = CommandTemplate("Unsafe", "Stop-Computer", alias="unsafe")
        dialog = self.dialog([*BUILTIN_TEMPLATES, query, custom], "sysinfo", read_only=True)
        self.assertEqual(dialog.command.findData("restart-service"), -1)
        self.assertEqual(dialog.command.findData("unsafe"), -1)
        self.assertGreaterEqual(dialog.command.findData("bios"), 0)
        winrm = self.dialog([query], "bios", mode="winrm")
        self.assertFalse(winrm.apply_button.isEnabled())
        self.assertEqual(winrm.command.count(), 0)

    def test_cancel_and_invalid_initial_input_do_not_change_caller(self):
        dialog = self.dialog(initial="events --count nope")
        self.assertIn("could not be loaded", dialog.notice.text())
        dialog.reject()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)

    def test_choices_without_default_require_selection(self):
        template = CommandTemplate("Choice", "Get-Date", alias="choice",
                                   parameters={"_schema": {"mode": {"type": "int", "choices": [1, 2], "required": True}}})
        dialog = self.dialog([template], "choice")
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.fields["mode"].setCurrentIndex(1)
        self.assertTrue(dialog.apply_button.isEnabled())
        self.assertEqual(prepare_command(dialog.command_line, [template]).values["mode"], 2)

    def test_layout_and_capture(self):
        dialog = self.dialog()
        for dark in (False, True):
            dialog.setStyleSheet(stylesheet(dark))
            dialog.resize(620, 480)
            dialog.show()
            self.app.processEvents()
            for widget in (dialog.command, dialog.preview, dialog.apply_button):
                self.assertTrue(dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight())))
            folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
            if folder:
                path = Path(folder)
                path.mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(path / f"argument-form-{'dark' if dark else 'light'}.png"))

    def test_v3_optional_fields_omit_or_preserve_empty_and_restore(self):
        folder = Path(__file__).resolve().parents[1] / "examples/plugins/registry"
        template = read_plugin(folder).template()
        dialog = self.dialog([template], "registry keys")
        self.assertTrue(dialog.apply_button.isEnabled())
        self.assertNotIn("type", dialog.values())
        self.assertFalse(dialog.fields["type"].isEnabled())
        dialog.optional_fields["type"].setChecked(True)
        self.assertFalse(dialog.apply_button.isEnabled())
        dialog.fields["type"].setCurrentIndex(0)
        self.assertTrue(dialog.apply_button.isEnabled())
        dialog.optional_fields["data"].setChecked(True)
        self.assertEqual(dialog.values()["data"], "")
        restored = self.dialog([template], dialog.command_line)
        self.assertTrue(restored.optional_fields["data"].isChecked())
        self.assertEqual(restored.values()["data"], "")
        restored.optional_fields["data"].setChecked(False)
        self.assertNotIn("data", prepare_command(restored.command_line, [template]).values)

    def test_task_form_compact_layout_and_capture(self):
        folder = Path(__file__).resolve().parents[1] / "examples/plugins/scheduled-task"
        template = read_plugin(folder).template()
        dialog = self.dialog([template], "scheduled-task list")
        self.assertTrue(dialog.apply_button.isEnabled())
        for dark in (False, True):
            dialog.setStyleSheet(stylesheet(dark))
            dialog.resize(620, 480)
            dialog.show()
            self.app.processEvents()
            self.assertEqual(dialog.scroll.horizontalScrollBar().maximum(), 0)
            for widget in (dialog.command, dialog.preview, dialog.apply_button):
                self.assertTrue(dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight())))
            capture = os.environ.get("REMOTEOPS_CAPTURE_DIR")
            if capture:
                output = Path(capture)
                output.mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(output / f"task-arguments-{'dark' if dark else 'light'}.png"))
