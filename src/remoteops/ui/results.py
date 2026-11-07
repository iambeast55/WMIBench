from __future__ import annotations

import csv
from datetime import timedelta

from remoteops.ui.notices import inline_notice

from PySide6.QtCore import Qt, QSortFilterProxyModel, QTimer
from PySide6.QtGui import QColor, QStandardItem, QStandardItemModel, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QStyle, QTableView,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QToolButton, QVBoxLayout, QWidget,
    QAbstractItemView, QHeaderView,
)

from remoteops.core.models import utc_now
from remoteops.remoting.output import display_value
from remoteops.ui.icons import set_button_icon


def icon_button(parent, icon, label):
    button = QToolButton(parent)
    set_button_icon(button, icon)
    button.setToolTip(label)
    button.setAccessibleName(label)
    button.setFixedSize(34, 34)
    return button


class FindBar(QWidget):
    def __init__(self, editor):
        super().__init__()
        self.editor = editor
        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        self.query = QLineEdit()
        self.query.setMinimumWidth(40)
        self.query.setPlaceholderText("Find in output")
        self.count = QLabel()
        previous = icon_button(self, QStyle.StandardPixmap.SP_ArrowUp, "Previous match")
        next_button = icon_button(self, QStyle.StandardPixmap.SP_ArrowDown, "Next match")
        row.addWidget(self.query, 1)
        row.addWidget(self.count)
        row.addWidget(previous)
        row.addWidget(next_button)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(150)
        self.query.textChanged.connect(lambda: self.timer.start())
        self.timer.timeout.connect(self.highlight)
        editor.textChanged.connect(lambda: self.timer.start() if self.query.text() else None)
        next_button.clicked.connect(lambda: self.find(False))
        previous.clicked.connect(lambda: self.find(True))
        self.query.returnPressed.connect(lambda: self.find(False))

    def highlight(self):
        query = self.query.text()
        selections = []
        matches = 0
        if query:
            cursor = QTextCursor(self.editor.document())
            while True:
                cursor = self.editor.document().find(query, cursor)
                if cursor.isNull():
                    break
                matches += 1
                if len(selections) < 2000:
                    selected = QTextEdit.ExtraSelection()
                    selected.cursor = cursor
                    selected.format.setBackground(QColor("#e9c66c"))
                    selected.format.setForeground(QColor("#202020"))
                    selections.append(selected)
        self.editor.setExtraSelections(selections)
        self.count.setText(f"{matches} matches" if query else "")

    def find(self, backwards):
        query = self.query.text()
        if not query:
            return
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if not self.editor.find(query, flags):
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
            self.editor.setTextCursor(cursor)
            self.editor.find(query, flags)


class RecordTable(QWidget):
    def __init__(self):
        super().__init__()
        self.setObjectName("resultSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter table")
        self.count = QLabel("0 rows")
        copy = icon_button(self, QStyle.StandardPixmap.SP_FileIcon, "Copy selected cells")
        export = icon_button(self, QStyle.StandardPixmap.SP_DialogSaveButton, "Export visible rows as CSV")
        row.addWidget(self.search, 1)
        row.addWidget(self.count)
        row.addWidget(copy)
        row.addWidget(export)
        layout.addLayout(row)
        self.model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setSortRole(Qt.ItemDataRole.UserRole)
        self.table = QTableView()
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(36)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setWordWrap(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.table)
        self.search.textChanged.connect(self.filter)
        copy.clicked.connect(self.copy_selection)
        export.clicked.connect(self.export)

    def set_records(self, records):
        self.table.setSortingEnabled(False)
        self.model.clear()
        columns = list(dict.fromkeys(key for row in records for key in row))
        self.model.setHorizontalHeaderLabels(columns)
        for record in records:
            items = []
            for key in columns:
                value = record.get(key)
                item = QStandardItem(display_value(value))
                item.setData(value if isinstance(value, (int, float)) else display_value(value), Qt.ItemDataRole.UserRole)
                item.setToolTip(display_value(value))
                items.append(item)
            self.model.appendRow(items)
        self.table.setSortingEnabled(True)
        for index, name in enumerate(columns):
            self.table.setColumnWidth(index, 100 if name in {"ID", "State", "Level", "Startup"} else 220)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.filter(self.search.text())

    def filter(self, text):
        self.proxy.setFilterFixedString(text)
        self.count.setText(f"{self.proxy.rowCount()} / {self.model.rowCount()} rows")

    def copy_selection(self):
        indexes = self.table.selectionModel().selectedIndexes()
        if not indexes:
            return
        selected = {(index.row(), index.column()): str(index.data() or "") for index in indexes}
        rows = sorted({row for row, _ in selected})
        cols = sorted({col for _, col in selected})
        QApplication.clipboard().setText("\n".join("\t".join(selected.get((r, c), "") for c in cols) for r in rows))

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export table", "results.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow([self.proxy.headerData(c, Qt.Orientation.Horizontal) for c in range(self.proxy.columnCount())])
                for row in range(self.proxy.rowCount()):
                    values = [str(self.proxy.index(row, c).data() or "") for c in range(self.proxy.columnCount())]
                    writer.writerow(["'" + v if v.startswith(("=", "+", "-", "@")) else v for v in values])
        except OSError as exc:
            inline_notice(self, "Export failed", str(exc))


class ResultViewer(QTabWidget):
    def __init__(self):
        super().__init__()
        self.tabBar().setObjectName("resultTabs")
        text_page = QWidget()
        layout = QVBoxLayout(text_page)
        layout.setContentsMargins(0, 0, 0, 0)
        self.text = QTextEdit()
        self.text.setStyleSheet("font-family: 'Consolas', 'DejaVu Sans Mono', monospace; font-size: 15px;")
        self.text.setReadOnly(True)
        self.text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.find_bar = FindBar(self.text)
        self.find_bar.hide()
        layout.addWidget(self.find_bar)
        layout.addWidget(self.text)
        self.table = RecordTable()
        self.addTab(text_page, "Output")
        self.addTab(self.table, "Table")
        find = icon_button(self, "fa5s.search", "Find in result output")
        find.setCheckable(True)
        find.toggled.connect(self.find_bar.setVisible)
        self.setCornerWidget(find)

    def set_result(self, result):
        duration = f" / {result.duration_seconds:.2f}s" if result.duration_seconds is not None else ""
        content = f"{result.command_line or result.command}\n{'Succeeded' if result.succeeded else 'Failed'}{duration}\n\n{result.stdout}"
        if result.stderr:
            content += f"\n\n[stderr]\n{result.stderr}"
        self.text.setPlainText(content)
        self.table.set_records(result.records)


class HistoryWidget(QWidget):
    PAGE_SIZE = 100

    def __init__(self, repository, host_id):
        super().__init__()
        self.setObjectName("resultSurface")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.repository, self.host_id = repository, host_id
        self.offset = 0
        self.results = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search commands and output")
        self.outcome = QComboBox()
        for title, value in [("All results", "all"), ("Succeeded", "success"), ("Failed", "failed")]:
            self.outcome.addItem(title, value)
        self.period = QComboBox()
        for title, days in [("Any date", 0), ("24 hours", 1), ("7 days", 7), ("30 days", 30)]:
            self.period.addItem(title, days)
        self.all_hosts = QCheckBox("All hosts")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.outcome)
        filters.addWidget(self.period)
        filters.addWidget(self.all_hosts)
        layout.addLayout(filters)
        splitter = self.splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(7)
        self.list = QTableWidget(0, 5)
        self.list.setHorizontalHeaderLabels(["Time", "Host", "Command", "Mode", "Result"])
        self.list.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.list.horizontalHeader().setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.list.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 170), (1, 145), (3, 80), (4, 105)):
            self.list.setColumnWidth(col, width)
        self.list.setColumnHidden(1, True)
        self.list.setWordWrap(False)
        self.list.setShowGrid(False)
        self.list.setAlternatingRowColors(True)
        self.list.verticalHeader().setDefaultSectionSize(38)
        self.list.setMinimumHeight(100)
        self.list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.list.verticalHeader().hide()
        self.viewer = ResultViewer()
        self.viewer.setMinimumHeight(120)
        splitter.addWidget(self.list)
        splitter.addWidget(self.viewer)
        splitter.setSizes([230, 330])
        layout.addWidget(splitter, 1)
        paging = QHBoxLayout()
        self.page_label = QLabel()
        previous = icon_button(self, QStyle.StandardPixmap.SP_ArrowLeft, "Previous history page")
        next_button = icon_button(self, QStyle.StandardPixmap.SP_ArrowRight, "Next history page")
        self.previous, self.next_button = previous, next_button
        paging.addWidget(self.page_label)
        paging.addStretch()
        paging.addWidget(previous)
        paging.addWidget(next_button)
        layout.addLayout(paging)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.reload)
        self.search.textChanged.connect(lambda: self.timer.start())
        self.outcome.currentIndexChanged.connect(self.reload)
        self.period.currentIndexChanged.connect(self.reload)
        self.all_hosts.toggled.connect(self.reload)
        self.all_hosts.toggled.connect(lambda checked: self.list.setColumnHidden(1, not checked))
        self.list.itemSelectionChanged.connect(self.select)
        previous.clicked.connect(lambda: self.page(-1))
        next_button.clicked.connect(lambda: self.page(1))
        self.reload()

    def page(self, direction):
        self.offset = max(0, self.offset + direction * self.PAGE_SIZE)
        self.reload(reset=False)

    def reload(self, *_args, reset=True):
        if reset:
            self.offset = 0
        days = self.period.currentData()
        since = (utc_now() - timedelta(days=days)).isoformat() if days else ""
        results = self.repository.list_command_history(
            self.PAGE_SIZE + 1, host_id=None if self.all_hosts.isChecked() else self.host_id,
            search=self.search.text(), outcome=self.outcome.currentData(), since=since, offset=self.offset,
        )
        self.next_button.setEnabled(len(results) > self.PAGE_SIZE)
        self.previous.setEnabled(self.offset > 0)
        self.results = results[:self.PAGE_SIZE]
        self.list.setRowCount(0)
        self.viewer.text.clear()
        self.viewer.table.set_records([])
        for row, result in enumerate(self.results):
            self.list.insertRow(row)
            for col, value in enumerate([result.started_at.astimezone().strftime("%b %d, %H:%M:%S"), result.host_name,
                                         result.command_line or (result.command.splitlines()[0] if result.command else ""), result.connection_mode,
                                         "Succeeded" if result.succeeded else "Failed"]):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if col == 0:
                    item.setToolTip(result.started_at.astimezone().isoformat())
                if col == 3:
                    item.setText("WMI" if result.connection_mode == "wmi_dcom" else "WinRM")
                self.list.setItem(row, col, item)
        self.page_label.setText(f"{len(self.results)} runs / Page {self.offset // self.PAGE_SIZE + 1}")
        if self.results:
            self.list.selectRow(0)

    def select(self):
        row = self.list.currentRow()
        if 0 <= row < len(self.results):
            self.viewer.set_result(self.results[row])
