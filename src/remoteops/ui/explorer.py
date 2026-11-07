from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QApplication, QComboBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from remoteops.remoting.explorer import method_snippet, query_snippet
from remoteops.ui.icons import set_button_icon
from remoteops.ui.results import RecordTable, icon_button


class ExplorerPanel(QWidget):
    def __init__(self, dispatch, namespace="root/cimv2"):
        super().__init__()
        self.setObjectName("inspectionPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.dispatch = dispatch
        self.connected = self.busy = False
        self.metadata_supported = True
        self.generation = 0
        self.schema = []
        self.class_name = ""
        self.current_namespace = namespace.replace("\\", "/")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        row = QHBoxLayout()
        self.up = icon_button(self, "fa5s.arrow-up", "Parent namespace")
        self.namespace = QLineEdit(self.current_namespace)
        self.namespace.setAccessibleName("WMI namespace")
        self.open_button = icon_button(self, "fa5s.arrow-right", "Open namespace")
        self.refresh_button = icon_button(self, "fa5s.sync-alt", "Refresh")
        row.addWidget(self.up)
        row.addWidget(self.namespace, 1)
        row.addWidget(self.open_button)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        navigation = QWidget()
        nav = QVBoxLayout(navigation)
        nav.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter names")
        self.search.setClearButtonEnabled(True)
        nav.addWidget(self.search)
        self.browser = QTabWidget()
        self.namespaces = QListWidget()
        self.classes = QListWidget()
        self.namespaces.setAccessibleName("Child namespaces")
        self.classes.setAccessibleName("WMI classes")
        self.browser.addTab(self.classes, "Classes")
        self.browser.addTab(self.namespaces, "Namespaces")
        nav.addWidget(self.browser)
        navigation.setMinimumWidth(150)
        splitter.addWidget(navigation)
        details = QWidget()
        detail = QVBoxLayout(details)
        detail.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel("No class selected")
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        detail.addWidget(self.title)
        self.pages = QTabWidget()
        self.properties = RecordTable()
        self.pages.addTab(self.properties, "Properties")
        method_page = QWidget()
        method_layout = QVBoxLayout(method_page)
        method_layout.setContentsMargins(0, 0, 0, 0)
        self.methods = QComboBox()
        self.methods.setMinimumContentsLength(12)
        self.methods.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.methods.setAccessibleName("WMI method")
        self.parameters = RecordTable()
        method_layout.addWidget(self.methods)
        method_layout.addWidget(self.parameters)
        self.pages.addTab(method_page, "Methods")
        self.instances = RecordTable()
        self.pages.addTab(self.instances, "Instances")
        self.code = QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.code.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.code.setAccessibleName("Plugin example")
        self.pages.addTab(self.code, "Python")
        detail.addWidget(self.pages, 1)
        controls = QHBoxLayout()
        self.limit = QSpinBox()
        self.limit.setRange(1, 1000)
        self.limit.setValue(100)
        self.limit.setSuffix(" rows")
        self.limit.setAccessibleName("Instance row limit")
        self.query_button = QPushButton("Query")
        set_button_icon(self.query_button, "fa5s.play")
        self.copy_query = icon_button(self, "fa5s.code", "Copy ctx.query example")
        self.copy_method = icon_button(self, "fa5s.copy", "Copy ctx.invoke example")
        controls.addWidget(self.limit)
        controls.addWidget(self.query_button)
        controls.addStretch()
        controls.addWidget(self.copy_query)
        controls.addWidget(self.copy_method)
        detail.addLayout(controls)
        details.setMinimumWidth(300)
        splitter.addWidget(details)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([240, 700])
        layout.addWidget(splitter, 1)
        self.status = QLabel("Not loaded")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.open_button.clicked.connect(self.open_namespace)
        self.namespace.returnPressed.connect(self.open_namespace)
        self.namespace.textChanged.connect(self.update_controls)
        self.up.clicked.connect(self.parent_namespace)
        self.refresh_button.clicked.connect(self.refresh)
        self.browser.currentChanged.connect(self.browse_changed)
        self.search.textChanged.connect(self.filter_names)
        self.namespaces.itemDoubleClicked.connect(self.open_child)
        self.classes.currentTextChanged.connect(self.describe)
        self.methods.currentIndexChanged.connect(self.method_changed)
        self.query_button.clicked.connect(self.query)
        self.copy_query.clicked.connect(lambda: self.copy_code(False))
        self.copy_method.clicked.connect(lambda: self.copy_code(True))
        self.limit.valueChanged.connect(self.update_code)
        self.properties.table.selectionModel().selectionChanged.connect(self.update_code)
        self.update_controls()

    def invalidate(self):
        self.generation += 1
        self.classes.blockSignals(True)
        self.classes.clear()
        self.classes.blockSignals(False)
        self.namespaces.clear()
        self.clear_class()
        self.status.setText("Not loaded")

    def clear_class(self):
        self.schema = []
        self.class_name = ""
        self.title.setText("No class selected")
        self.methods.clear()
        for table in (self.properties, self.parameters, self.instances):
            table.set_records([])
        self.code.clear()
        self.update_controls()

    def set_connection(self, connected, metadata_supported=True):
        newly_connected = connected and not self.connected
        if self.connected and not connected:
            self.invalidate()
        self.connected = connected
        self.metadata_supported = metadata_supported
        self.browser.setTabEnabled(0, metadata_supported)
        if not metadata_supported:
            self.browser.blockSignals(True)
            self.browser.setCurrentIndex(1)
            self.browser.blockSignals(False)
            self.browser.setToolTip("Class inspection requires native Windows WMI or WinRM.")
        else:
            self.browser.setToolTip("")
        self.update_controls()
        if newly_connected and self.isVisible():
            QTimer.singleShot(0, self.ensure_loaded)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.ensure_loaded)

    def ensure_loaded(self):
        if self.connected and not self.busy and self.status.text() == "Not loaded":
            self.refresh()

    def set_busy(self, busy):
        self.busy = busy
        self.update_controls()

    def update_controls(self, *_):
        ready = self.connected and not self.busy
        for widget in (self.namespace, self.open_button, self.refresh_button, self.up, self.browser):
            widget.setEnabled(ready)
        self.up.setEnabled(ready and "/" in self.current_namespace)
        same_namespace = self.namespace.text().replace("\\", "/").strip("/") == self.current_namespace
        self.query_button.setEnabled(ready and same_namespace and bool(self.property_rows()))
        self.limit.setEnabled(not self.busy)
        self.copy_query.setEnabled(bool(self.property_rows()))
        self.copy_method.setEnabled(self.methods.currentData() is not None)

    def send(self, action, args, callback):
        if not self.connected or self.busy:
            return
        generation = self.generation
        self.status.setText("Loading...")

        def loaded(outcome):
            if generation != self.generation:
                return
            error = outcome.error or (outcome.value.stderr if outcome.value and not outcome.value.succeeded else "")
            if error:
                self.status.setText("Request failed. See terminal.")
                self.status.setToolTip(error)
                return
            self.status.setToolTip("")
            callback(outcome.value.records)
            self.update_controls()

        self.dispatch("explorer-" + action, args, loaded)

    def open_namespace(self):
        if not self.connected or self.busy:
            return
        self.current_namespace = self.namespace.text().replace("\\", "/").strip("/")
        self.invalidate()
        self.refresh()

    def parent_namespace(self):
        self.namespace.setText(self.current_namespace.rsplit("/", 1)[0])
        self.open_namespace()

    def open_child(self, item):
        self.namespace.setText(self.current_namespace + "/" + item.text())
        self.open_namespace()

    def browse_changed(self):
        if self.connected and not self.busy:
            self.refresh()

    def refresh(self):
        if not self.connected or self.busy:
            return
        if self.namespace.text().replace("\\", "/").strip("/") != self.current_namespace:
            self.open_namespace()
            return
        action = "classes" if self.browser.currentIndex() == 0 else "namespaces"
        target = self.classes if action == "classes" else self.namespaces
        if action == "classes":
            self.clear_class()

        def loaded(rows):
            target.blockSignals(True)
            target.clear()
            target.addItems(sorted((row["Name"] for row in rows), key=str.casefold))
            target.blockSignals(False)
            self.filter_names()
            self.status.setText(f"{len(rows):,} {action}")

        self.send(action, {"namespace": self.current_namespace}, loaded)

    def filter_names(self):
        query = self.search.text().casefold()
        for target in (self.classes, self.namespaces):
            for index in range(target.count()):
                target.item(index).setHidden(query not in target.item(index).text().casefold())

    def describe(self, name):
        if not name or self.busy:
            return
        self.clear_class()

        def loaded(rows):
            self.schema = rows
            self.class_name = name
            self.title.setText(name)
            class_row = next((r for r in rows if r.get("Kind") == "Class"), {})
            self.title.setToolTip(str(class_row.get("Qualifiers", {})))
            self.properties.set_records([{key: row.get(key) for key in ("Name", "Type", "Key", "Qualifiers")}
                                         for row in rows if row.get("Kind") == "Property"])
            self.properties.table.setColumnWidth(1, 110)
            self.properties.table.setColumnWidth(2, 65)
            self.methods.blockSignals(True)
            self.methods.clear()
            for row in rows:
                if row.get("Kind") == "Method":
                    self.methods.addItem(row["Name"] + (" (static)" if row.get("Static") else ""), row)
            self.methods.blockSignals(False)
            self.method_changed()
            self.update_code()
            self.status.setText(f"{len(self.property_rows()):,} queryable properties / {self.methods.count()} methods")

        self.send("describe", {"namespace": self.current_namespace, "class_name": name}, loaded)

    def property_rows(self):
        from remoteops.commands.wmi_definitions import CIM_TYPES
        rows = [r for r in self.schema if r.get("Kind") == "Property"
                and r.get("Type", "").removesuffix("[]") in CIM_TYPES]
        indexes = self.properties.table.selectionModel().selectedIndexes()
        names = {self.properties.proxy.index(index.row(), 0).data() for index in indexes}
        return [r for r in rows if not names or r["Name"] in names][:64]

    def method_changed(self):
        method = self.methods.currentData()
        self.parameters.set_records([{key: row.get(key) for key in ("Name", "Direction", "Type", "Qualifiers")}
                                    for row in self.schema if row.get("Kind") == "Parameter"
                                    and method and row.get("Method") == method["Name"]])
        self.methods.setToolTip(str(method.get("Qualifiers", {})) if method else "")
        self.parameters.table.setColumnWidth(1, 90)
        self.parameters.table.setColumnWidth(2, 110)
        self.update_controls()

    def update_code(self, *_):
        if self.class_name:
            self.code.setPlainText(query_snippet(self.current_namespace, self.class_name, self.property_rows(), self.limit.value()))
        self.update_controls()

    def copy_code(self, method=False):
        if not self.class_name:
            return
        selected = self.methods.currentData()
        if method and selected:
            source = method_snippet(self.current_namespace, self.class_name, selected, self.schema)
        else:
            source = query_snippet(self.current_namespace, self.class_name, self.property_rows(), self.limit.value())
        self.code.setPlainText(source)
        self.pages.setCurrentWidget(self.code)
        QApplication.clipboard().setText(source)

    def query(self):
        properties = self.property_rows()
        if not properties:
            return
        limit = self.limit.value()
        self.instances.set_records([])
        self.pages.setCurrentWidget(self.instances)

        def loaded(rows):
            self.instances.set_records(rows)
            self.status.setText(f"{len(rows):,} rows" + (f" (limit {limit:,})" if len(rows) == limit else ""))

        self.send("instances", {"namespace": self.current_namespace, "class_name": self.class_name,
                                "properties": [row["Name"] for row in properties], "limit": limit}, loaded)
