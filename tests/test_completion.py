import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from remoteops.commands.completion import complete
from remoteops.commands.templates import BUILTIN_TEMPLATES, prepare_command
from remoteops.core.models import CommandTemplate
from remoteops.ui.command_input import CommandInput


CUSTOM = CommandTemplate(name="Example", alias="example", script="Write-Output 'ok'", parameters={
    "_positional": ["action"], "_schema": {
        "action": {"choices": ["create", "list"], "default": "list"},
        "account": {"choices": ["SYSTEM", "LOCAL SERVICE", "O'Brien"], "default": "SYSTEM"},
        "enabled": {"type": "bool", "default": False},
    }})


class CompletionTests(unittest.TestCase):
    def suggestions(self, text, cursor=None):
        return complete(text, len(text) if cursor is None else cursor, [*BUILTIN_TEMPLATES, CUSTOM])

    def test_alias_flags_and_used_flags(self):
        self.assertEqual([x.text for x in self.suggestions("sys")], ["sysinfo"])
        self.assertEqual([x.text for x in self.suggestions("events --co")], ["--count"])
        self.assertNotIn("--count", [x.text for x in self.suggestions("events --count 5 --")])
        self.assertEqual(self.suggestions("missing --"), [])

    def test_choices_boolean_positional_and_equals_forms(self):
        for text, expected in (("example cr", "create"), ("example --enabled f", "false"),
                               ("example --account=SY", "--account=SYSTEM"), ("example account=SY", "account=SYSTEM")):
            self.assertIn(expected, [x.text for x in self.suggestions(text)])
        self.assertEqual(self.suggestions("events --count "), [])

    def test_quotes_and_windows_paths_are_preserved(self):
        text = 'example --account "LOCAL S'
        suggestion = self.suggestions(text)[0]
        fixed = text[:suggestion.start] + suggestion.text
        self.assertEqual(prepare_command(fixed, [CUSTOM]).values["account"], "LOCAL SERVICE")
        text = "example --account O"
        suggestion = self.suggestions(text)[0]
        self.assertEqual(prepare_command(text[:suggestion.start] + suggestion.text, [CUSTOM]).values["account"], "O'Brien")
        text = r'events --log "C:\Windows\System32" --co'
        item = self.suggestions(text)[0]
        self.assertEqual(text[:item.start], 'events --log "C:\\Windows\\System32" ')

    def test_middle_token_replacement_keeps_suffix(self):
        text = "events --co --log System"
        item = self.suggestions(text, len("events --co"))[0]
        self.assertEqual(text[:item.start] + item.text + text[item.end:], "events --count --log System")


class CompletionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.editor = CommandInput()
        self.editor.template_provider = lambda: [*BUILTIN_TEMPLATES, CUSTOM]
        self.editor.show()
        self.editor.activateWindow()
        self.editor.setFocus()
        self.app.processEvents()

    def tearDown(self):
        self.editor.timer.stop()
        self.editor.completion.popup().hide()
        self.editor.close()
        self.editor.deleteLater()
        self.app.processEvents()

    def test_enter_accepts_without_submitting(self):
        submitted = []
        self.editor.returnPressed.connect(lambda: submitted.append(self.editor.text()))
        self.editor.setText("sys")
        self.editor.show_completions()
        self.assertTrue(self.editor.completion.popup().isVisible())
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(self.editor.text(), "sysinfo ")
        self.assertEqual(submitted, [])
        QTest.keyClick(self.editor, Qt.Key.Key_Return)
        self.assertEqual(submitted, ["sysinfo "])

    def test_tab_and_history_draft(self):
        self.editor.setText("events --co")
        self.editor.show_completions()
        QTest.keyClick(self.editor, Qt.Key.Key_Tab)
        self.assertEqual(self.editor.text(), "events --count ")
        self.editor.remember("sysinfo")
        self.editor.setText("draft")
        QTest.keyClick(self.editor, Qt.Key.Key_Up)
        self.assertEqual(self.editor.text(), "sysinfo")
        QTest.keyClick(self.editor, Qt.Key.Key_Down)
        self.assertEqual(self.editor.text(), "draft")

    def test_stale_completion_and_library_refresh(self):
        self.editor.setText("sys")
        self.editor.show_completions()
        self.editor.setText("events")
        self.editor.accept_completion("sysinfo")
        self.assertEqual(self.editor.text(), "events")
        self.editor.template_provider = lambda: [CUSTOM]
        self.editor.setText("sys")
        self.editor.show_completions()
        self.assertFalse(self.editor.completion.popup().isVisible())


if __name__ == "__main__":
    unittest.main()
