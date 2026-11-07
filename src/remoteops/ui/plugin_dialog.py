"""Plugin folders, source previews, and offline tests."""
from __future__ import annotations

import sys
import json
from pathlib import Path

from remoteops.ui.notices import inline_notice

from PySide6.QtCore import QProcess, QProcessEnvironment, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices, QFontDatabase
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QHeaderView, QInputDialog,
    QPlainTextEdit, QPushButton, QSplitter, QStyle, QTableWidget,
    QTableWidgetItem, QToolButton, QVBoxLayout, QWidget,
)

from remoteops.ui.icons import set_button_icon
from remoteops.standalone import plugin_cli_arguments


class PluginDialog(QDialog):
    def __init__(self, registry, parent=None, window=None):
        super().__init__(parent)
        self.registry = registry
        self.store = registry.plugins
        self.window = window
        self.process = None
        self.setWindowTitle("Python commands")
        self.resize(980, 680)
        self.setMinimumSize(720, 480)
        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.buttons = {}
        actions = [
            ("Create command", "fa5s.file-code", self.create),
            ("Install folder", "fa5s.folder-plus", self.install),
            ("Open folder", QStyle.StandardPixmap.SP_DialogOpenButton, self.open_folder),
            ("Run offline tests", QStyle.StandardPixmap.SP_MediaPlay, self.test),
            ("Stop offline tests", QStyle.StandardPixmap.SP_MediaStop, self.stop_tests),
            ("Refresh folders", "fa5s.list", self.refresh),
        ]
        for title, icon, callback in actions:
            button = QToolButton(self)
            button.setFixedSize(34, 34)
            button.setToolTip(title)
            button.setAccessibleName(title)
            set_button_icon(button, icon)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
            self.buttons[title] = button
        toolbar.addStretch()
        self.run_button = QPushButton("Use command")
        set_button_icon(self.run_button, QStyle.StandardPixmap.SP_MediaPlay)
        self.run_button.clicked.connect(self.run_selected)
        toolbar.addWidget(self.run_button)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Folder", "Command", "Status", "Revision"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self.preview)
        self.table.itemDoubleClicked.connect(lambda _item: self.open_folder())
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.details.setAccessibleName("Python command source and diagnostics")
        splitter.addWidget(self.table)
        splitter.addWidget(self.details)
        splitter.setSizes([280, 320])
        layout.addWidget(splitter, 1)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.refresh()

    def selected(self):
        rows = self.table.selectionModel().selectedRows()
        return self.store.root / self.table.item(rows[0].row(), 0).text() if rows else None

    def refresh(self):
        selected = self.selected()
        try:
            self.registry.load()
        except (ValueError, OSError) as exc:
            inline_notice(self, "Commands unavailable", str(exc))
            return
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for folder in self.store.folders():
            active = self.store.active(folder)
            try:
                snapshot = self.store.validate(folder)
                alias = snapshot.manifest["alias"]
                status = "Ready" if active else self.store.errors.get(folder.name, "Unavailable")
            except (ValueError, OSError, SyntaxError) as exc:
                alias = active.manifest["alias"] if active else ""
                status = "Invalid: " + str(exc)
            row = self.table.rowCount()
            self.table.insertRow(row)
            for col, text in enumerate((folder.name, alias, status, active.digest[:12] if active else "")):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.table.setItem(row, col, item)
            if folder == selected:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.preview()

    def preview(self):
        folder = self.selected()
        busy = self.process is not None
        for title, button in self.buttons.items():
            button.setEnabled(not busy and (folder is not None or title in {
                "Create command", "Install folder", "Open folder", "Refresh folders"}))
        self.buttons["Stop offline tests"].setEnabled(busy)
        active = self.store.active(folder) if folder else None
        host = self.window.current_host() if self.window else None
        self.run_button.setEnabled(not busy and active is not None and host is not None
                                   and host.connection_mode.value in active.backends)
        if busy:
            return
        if not folder:
            self.details.clear()
            return
        try:
            snapshot = self.store.validate(folder)
            self.details.setPlainText(json.dumps(snapshot.manifest, indent=2) + "\n\n" + snapshot.source)
        except (ValueError, OSError, SyntaxError) as exc:
            self.details.setPlainText(str(exc))

    def guard(self, fn):
        try:
            return fn()
        except (ValueError, OSError, SyntaxError) as exc:
            inline_notice(self, "Python command", str(exc))
            return None

    def create(self):
        alias, ok = QInputDialog.getText(self, "Create Python command", "Command alias")
        if ok and self.guard(lambda: self.store.scaffold(alias)):
            self.refresh()
            self.select_folder(alias)

    def select_folder(self, name):
        for row in range(self.table.rowCount()):
            if self.table.item(row, 0).text() == name:
                self.table.selectRow(row)
                break

    def install(self):
        source = QFileDialog.getExistingDirectory(self, "Install Python command folder")
        if source:
            destination = self.guard(lambda: self.store.install(Path(source)))
            self.refresh()
            if destination:
                self.select_folder(destination.name)

    def open_folder(self):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.selected() or self.store.root)))

    def test(self):
        folder = self.selected()
        if folder is None or self.process is not None:
            return
        process = QProcess(self)
        self.process = process
        environment = QProcessEnvironment.systemEnvironment()
        if not getattr(sys, "frozen", False):
            environment.insert("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
        environment.insert("PYTHONUNBUFFERED", "1")
        process.setProcessEnvironment(environment)
        process.setWorkingDirectory(str(folder))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self.test_output)
        process.finished.connect(self.test_finished)
        process.errorOccurred.connect(self.test_error)
        self.details.clear()
        self.preview()
        process.start(sys.executable, plugin_cli_arguments(["test", str(folder), "--trust"]))
        QTimer.singleShot(120000, self, lambda: self.stop_tests() if self.process is process else None)

    def test_output(self):
        if self.process:
            output = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
            if self.details.document().characterCount() < 100000:
                self.details.insertPlainText(output[:100000])

    def test_finished(self, code, _status):
        if self.process is None:
            return
        self.test_output()
        output = self.details.toPlainText() + f"\nExit code: {code}"
        self.process.deleteLater()
        self.process = None
        self.preview()
        self.details.setPlainText(output)

    def test_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.details.setPlainText(self.process.errorString())
            self.test_finished(1, None)

    def stop_tests(self):
        if self.process:
            self.process.kill()

    def reject(self):
        if self.process:
            self.stop_tests()
            return
        super().reject()

    def closeEvent(self, event):
        if self.process:
            self.stop_tests()
            event.ignore()
        else:
            super().closeEvent(event)

    def run_selected(self):
        try:
            self.registry.load()
        except (ValueError, OSError) as exc:
            inline_notice(self, "Commands unavailable", str(exc))
            return
        folder = self.selected()
        snapshot = self.store.active(folder) if folder else None
        host = self.window.current_host() if self.window else None
        if snapshot is None or host is None:
            return
        tab = self.window.host_tabs_by_id.get(host.id)
        if tab is None or tab.busy or self.window.session_manager.host_busy(host.id):
            inline_notice(self, "Host busy", "Host operation active.")
            return
        tab.command_input.setText(snapshot.manifest["alias"])
        tab.view_tabs.setCurrentIndex(0)
        self.accept()
        if isinstance(self.parent(), QDialog):
            self.parent().accept()
        tab.command_input.setFocus()
