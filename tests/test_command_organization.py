import json
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox
from remoteops.commands.templates import CommandRegistry, command_category
from remoteops.core.models import CommandTemplate
from remoteops.ui.main_window import CommandLibraryDialog, CommandTemplateDialog
from remoteops.ui.theme import stylesheet


class OrganizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.registry = CommandRegistry(Path(self.temp.name) / "commands.json")

    def library(self, **kwargs):
        dialog = CommandLibraryDialog(self.registry, **kwargs)
        self.addCleanup(dialog.close)
        return dialog

    def visible(self, dialog):
        return [dialog.table.item(row, 0).text() for row in range(dialog.table.rowCount())
                if not dialog.table.isRowHidden(row)]

    def row(self, dialog, alias):
        return next(row for row in range(dialog.table.rowCount()) if dialog.table.item(row, 0).text() == alias)

    def test_metadata_roundtrip_export_and_legacy_defaults(self):
        template = deepcopy(self.registry.aliases()["svc"])
        self.assertEqual(command_category(template), "Services")
        template.category, template.favorite = "My operations", True
        self.registry.upsert(template)
        self.registry.load()
        saved = self.registry.aliases()["svc"]
        self.assertEqual((saved.category, saved.favorite), ("My operations", True))
        path = Path(self.temp.name) / "export.json"
        self.registry.export_file(path, [saved])
        imported = CommandRegistry._parse(path.read_bytes())[1][0]
        self.assertEqual((imported.category, imported.favorite), (saved.category, saved.favorite))
        data = json.loads(path.read_text())
        del data["commands"][0]["category"]
        del data["commands"][0]["favorite"]
        old = CommandRegistry._parse(json.dumps(data))[1][0]
        self.assertEqual(command_category(old), "Services")
        self.assertFalse(old.favorite)
        before = self.registry.path.read_bytes()
        for category, favorite in ((None, True), ("x" * 81, False), ("Test", "yes")):
            invalid = deepcopy(saved)
            invalid.category, invalid.favorite = category, favorite
            with self.assertRaises(ValueError):
                self.registry.upsert(invalid)
            self.assertEqual(self.registry.path.read_bytes(), before)

    def test_combined_filters_favorites_and_insert_guard(self):
        self.registry.upsert(CommandTemplate("PS only", "Get-Date", alias="ps-only"))
        dialog = self.library(backend="WMI/DCOM")
        self.assertNotIn("ps-only", self.visible(dialog))
        dialog.category_filter.setCurrentText("Services")
        self.assertEqual(set(self.visible(dialog)), {"services", "svc", "restart-service"})
        dialog.search.setText(" STATUS ")
        self.assertEqual(set(self.visible(dialog)), {"services", "svc", "restart-service"})
        dialog.search.setText("svc")
        self.assertEqual(self.visible(dialog), ["svc"])
        row = self.row(dialog, "svc")
        dialog.table.selectRow(row)
        dialog.table.item(row, 6).setCheckState(Qt.CheckState.Checked)
        self.assertTrue(self.registry.aliases()["svc"].favorite)
        dialog.favorites_only.setChecked(True)
        self.assertEqual(self.visible(dialog), ["svc"])
        dialog.search.clear()
        dialog.category_filter.setCurrentIndex(0)
        dialog.backend_filter.setCurrentIndex(0)
        dialog.favorites_only.setChecked(False)
        dialog.table.selectRow(self.row(dialog, "ps-only"))
        self.assertFalse(dialog.insert_button.isEnabled())
        dialog.choose_selected()
        self.assertIsNone(dialog.chosen_alias)
        dialog.table.selectRow(self.row(dialog, "svc"))
        self.assertTrue(dialog.insert_button.isEnabled())
        dialog.search.setText("no-matches")
        self.assertFalse(dialog.insert_button.isEnabled())
        dialog.choose_selected()
        self.assertIsNone(dialog.chosen_alias)
        dialog.search.clear()
        dialog.table.selectRow(self.row(dialog, "svc"))
        dialog.choose_selected()
        self.assertEqual(dialog.chosen_alias, "svc")

    def test_favorite_write_failure_restores_checkbox(self):
        dialog = self.library()
        row = self.row(dialog, "svc")
        with patch.object(self.registry, "_write", side_effect=OSError("disk full")), patch.object(QMessageBox, "warning"):
            dialog.table.item(row, 6).setCheckState(Qt.CheckState.Checked)
        self.assertFalse(self.registry.aliases()["svc"].favorite)
        self.assertEqual(dialog.table.item(self.row(dialog, "svc"), 6).checkState(), Qt.CheckState.Unchecked)

    def test_editor_preserves_favorite_and_custom_category(self):
        template = deepcopy(self.registry.aliases()["svc"])
        template.favorite = True
        dialog = CommandTemplateDialog(template)
        self.addCleanup(dialog.close)
        dialog.category.setCurrentText("Operations")
        saved = dialog.template()
        self.assertTrue(saved.favorite)
        self.assertEqual(saved.category, "Operations")

    def test_library_layout_and_capture(self):
        dialog = self.library(backend="WMI/DCOM")
        for dark in (False, True):
            dialog.setStyleSheet(stylesheet(dark))
            dialog.resize(1100, 560)
            dialog.show()
            self.app.processEvents()
            for widget in (dialog.search, dialog.table, dialog.insert_button, dialog.favorites_only):
                self.assertTrue(dialog.rect().contains(widget.mapTo(dialog, widget.rect().bottomRight())))
            folder = os.environ.get("REMOTEOPS_CAPTURE_DIR")
            if folder:
                target = Path(folder)
                target.mkdir(parents=True, exist_ok=True)
                dialog.grab().save(str(target / f"command-library-{'dark' if dark else 'light'}.png"))
