"""Remote commands, files, processes, and sessions."""
import ntpath

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox, QWidget

from remoteops.ui.errors import error_summary
from remoteops.ui.inspection import InspectionPanel
from remoteops.ui.results import icon_button


class ToolsPanel(InspectionPanel):
    def __init__(self, dispatch, transfer, cancel):
        super().__init__(dispatch)
        self.transfer = transfer
        self.cancel_operation = cancel
        self.connected = False
        self.wmi = False
        self.pending_action = None
        self.mode = QComboBox()
        for label, value in (("Command runner", "runner"), ("Files", "files"), ("Processes", "processes"), ("Users / sessions", "sessions")):
            self.mode.addItem(label, value)
        self.mode.setAccessibleName("Tool")
        self.controls.insertWidget(0, self.mode)
        self.refresh_button = icon_button(self, "fa5s.sync-alt", "Refresh")
        self.live = QCheckBox("Live")
        self.interval = QSpinBox()
        self.interval.setRange(3, 60)
        self.interval.setValue(5)
        self.interval.setSuffix(" s")
        self.interval.setAccessibleName("Process refresh interval")
        self.controls.addStretch()
        self.controls.addWidget(self.live)
        self.controls.addWidget(self.interval)
        self.controls.addWidget(self.refresh_button)
        self.fields = QWidget()
        fields = QHBoxLayout(self.fields)
        fields.setContentsMargins(0, 0, 0, 0)
        self.up = icon_button(self, "fa5s.arrow-up", "Parent directory")
        self.path = QLineEdit("C:\\")
        self.path.setAccessibleName("Remote directory")
        self.path.setPlaceholderText("Remote directory")
        self.accounts = QComboBox()
        self.accounts.addItem("Terminal sessions", "sessions")
        self.accounts.addItem("Local user accounts", "users")
        self.accounts.setAccessibleName("User inventory")
        for widget in (self.up, self.path, self.accounts):
            fields.addWidget(widget)
        self.content_layout.insertWidget(1, self.fields)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.editor.setAccessibleName("Remote command")
        self.editor.setMaximumHeight(180)
        self.editor.setMinimumHeight(90)
        self.content_layout.insertWidget(2, self.editor)
        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        self.output.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.output.setAccessibleName("Command output")
        self.content_layout.addWidget(self.output, 1)
        actions = QHBoxLayout()
        self.buttons = {}
        for key, label, icon in (("run", "Run", "fa5s.play"), ("cancel", "Cancel", "fa5s.stop"),
                                  ("upload", "Upload", "fa5s.upload"), ("download", "Download", "fa5s.download"),
                                  ("rename-file", "Rename", "fa5s.edit"), ("delete-file", "Delete file", "fa5s.trash-alt"),
                                  ("terminate", "End process", "fa5s.stop-circle"),
                                  ("disconnect-session", "Disconnect session", "fa5s.unlink"),
                                  ("logoff-session", "Sign out", "fa5s.sign-out-alt")):
            from remoteops.ui.icons import set_button_icon
            button = QPushButton(label)
            set_button_icon(button, icon)
            button.clicked.connect(lambda checked=False, key=key: self.act(key))
            actions.addWidget(button)
            self.buttons[key] = button
        actions.addStretch()
        self.content_layout.addLayout(actions)
        self.confirmation = QWidget()
        row = QHBoxLayout(self.confirmation)
        row.setContentsMargins(0, 0, 0, 0)
        self.confirm_text = QLabel()
        self.confirm_text.setTextFormat(Qt.TextFormat.PlainText)
        self.confirm_text.setWordWrap(True)
        row.addWidget(self.confirm_text, 1)
        confirm = QPushButton("Confirm")
        dismiss = QPushButton("Cancel")
        confirm.clicked.connect(self.confirm)
        dismiss.clicked.connect(self.dismiss)
        row.addWidget(confirm)
        row.addWidget(dismiss)
        self.content_layout.addWidget(self.confirmation)
        self.confirmation.hide()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
        self.elapsed = 0
        self.mode.currentIndexChanged.connect(self.changed)
        self.accounts.currentIndexChanged.connect(self.changed)
        self.refresh_button.clicked.connect(self.refresh)
        self.path.returnPressed.connect(self.refresh)
        self.up.clicked.connect(self.parent_directory)
        self.table.table.doubleClicked.disconnect()
        self.table.table.doubleClicked.connect(self.open_item)
        self.table.table.selectionModel().selectionChanged.connect(self.update_actions)
        self.changed()

    def set_connection(self, connected, wmi):
        if self.connected and not connected:
            self.fresh = False
            self.live.setChecked(False)
            self.dismiss()
        if self.wmi != wmi:
            self.wmi = wmi
            self.invalidate()
            self.changed()
        self.connected = connected
        self.accounts.setItemText(0, "WMI logon inventory" if wmi else "Terminal sessions")
        self.editor.setPlaceholderText("Executable and arguments" if wmi else "PowerShell script")
        self.editor.setToolTip("Returns a process ID" if wmi else "Remote PowerShell")
        self.update_actions()

    def set_busy(self, busy):
        self.busy = busy
        self.update_actions()

    def changed(self, *_):
        self.invalidate()
        self.dismiss()
        self.elapsed = 0
        mode = self.mode.currentData()
        self.editor.setVisible(mode == "runner")
        self.output.setVisible(mode == "runner")
        self.table.setVisible(mode != "runner")
        self.details_button.setVisible(mode != "runner")
        self.path.setVisible(mode == "files" or (mode == "runner" and self.wmi))
        self.up.setVisible(mode == "files")
        self.accounts.setVisible(mode == "sessions")
        self.live.setVisible(mode == "processes")
        self.interval.setVisible(mode == "processes")
        self.refresh_button.setVisible(mode != "runner")
        self.status.setText("Ready" if mode == "runner" else "Not loaded")
        self.update_actions()

    def update_actions(self, *_):
        mode = self.mode.currentData()
        ready = self.connected and not self.busy
        selected = self.selected_record() if self.fresh else None
        file = bool(selected and selected.get("Kind") == "File")
        session = bool(selected and selected.get("User") and selected.get("SessionId", 0) > 0)
        process = bool(selected and selected.get("ProcessId", 0) > 0 and selected.get("CreationDate"))
        self.mode.setEnabled(not self.busy)
        self.accounts.setEnabled(not self.busy)
        self.path.setEnabled(not self.busy)
        self.editor.setEnabled(not self.busy)
        self.up.setEnabled(ready)
        self.refresh_button.setEnabled(ready)
        visibility = {"run": mode == "runner", "cancel": mode in {"runner", "files"} and not self.wmi,
                      "upload": mode == "files", "download": mode == "files", "rename-file": mode == "files",
                      "delete-file": mode == "files", "terminate": mode == "processes",
                      "disconnect-session": mode == "sessions" and self.accounts.currentData() == "sessions",
                      "logoff-session": mode == "sessions" and self.accounts.currentData() == "sessions"}
        eligible = {"run": True, "cancel": self.busy, "upload": not self.wmi, "download": file and not self.wmi,
                    "rename-file": file, "delete-file": file, "terminate": process,
                    "disconnect-session": session and not self.wmi, "logoff-session": session and not self.wmi}
        for key, button in self.buttons.items():
            button.setVisible(visibility[key])
            button.setEnabled(bool(eligible[key] and (self.busy if key == "cancel" else ready)))
            if key in {"upload", "download", "disconnect-session", "logoff-session"}:
                button.setToolTip("Requires a WinRM connection" if self.wmi else "")
        self.buttons["run"].setText("Launch" if self.wmi else "Run")

    def tick(self):
        if (not self.isVisible() or not self.connected or self.busy or self.pending_action
                or self.mode.currentData() != "processes" or not self.live.isChecked()):
            self.elapsed = 0
            return
        self.elapsed += 1
        if self.elapsed >= self.interval.value():
            self.refresh(background=True)

    def refresh(self, _checked=False, *, background=False):
        if not self.connected or self.busy:
            return
        action = self.accounts.currentData() if self.mode.currentData() == "sessions" else self.mode.currentData()
        if action == "runner":
            return
        self.elapsed = 0
        self.dismiss()
        self.status.setText("Loading...")
        self.dispatch(action, {"path": self.path.text(), "_background": background}, self.loaded)

    def loaded(self, outcome):
        if not self.failed(outcome):
            old = self.selected_record()
            self.records = self.visible_records = outcome.value.records
            self.fresh = True
            self.table.set_records(self.records)
            if old:
                key = "ProcessId" if "ProcessId" in old else "Name" if "Name" in old else "SessionId"
                for index, record in enumerate(self.records):
                    if record.get(key) == old.get(key):
                        source = self.table.model.index(index, 0)
                        self.table.table.selectRow(self.table.proxy.mapFromSource(source).row())
                        break
            self.status.setText(f"{len(self.records):,} items")
            self.status.setToolTip("")
        else:
            self.live.setChecked(False)
        self.update_actions()

    def act(self, action):
        if action == "cancel":
            self.cancel_operation()
            return
        if not self.connected or self.busy:
            return
        record = dict(self.selected_record() or {})
        if action == "run":
            command = self.editor.toPlainText()
            if not command.strip():
                self.status.setText("Command required.")
                return
            self.status.setText("Launching..." if self.wmi else "Running...")
            self.dispatch("launch" if self.wmi else "runner", {"command": command, "directory": self.path.text()}, self.ran)
        elif action in {"upload", "download"}:
            self.transfer(action, record.get("Name", self.path.text()) if action == "download" else self.path.text())
        elif record and self.fresh:
            if action == "rename-file":
                name, ok = QInputDialog.getText(self, "Rename file", "New name", text=ntpath.basename(record["Name"]))
                if not ok:
                    return
                record["new_name"] = name
            label = record.get("Name") or record.get("User") or record.get("ProcessId")
            self.pending_action = action, record
            self.confirm_text.setText(f"{self.buttons[action].text()}: {label}?" + (" Unsaved work may be lost." if action in {"terminate", "logoff-session"} else ""))
            self.confirmation.show()

    def ran(self, outcome):
        result = outcome.value
        detail = outcome.error or (result.stderr if result else "No result")
        self.output.setPlainText((result.stdout if result else "") + ("\n" + detail if detail else ""))
        self.status.setText(error_summary(detail) if detail else "Process launched" if self.wmi else "Completed")

    def transferred(self, outcome):
        if outcome.error:
            self.status.setText(error_summary(outcome.error))
            self.status.setToolTip(outcome.error)
        elif self.mode.currentData() == "files":
            self.refresh()

    def confirm(self):
        if self.pending_action and self.connected and not self.busy:
            action, args = self.pending_action
            self.dismiss()
            self.fresh = False
            self.dispatch(action, args, self.mutated)

    def dismiss(self):
        self.pending_action = None
        self.confirmation.hide()

    def mutated(self, outcome):
        if not self.failed(outcome):
            self.refresh()
        self.update_actions()

    def open_item(self, *_):
        item = self.selected_record()
        if self.mode.currentData() == "files" and item and item.get("Kind") == "Directory":
            self.path.setText(item["Name"])
            self.refresh()
        else:
            self.open_details()

    def parent_directory(self):
        self.path.setText(ntpath.dirname(self.path.text().rstrip("\\")) + "\\")
        self.refresh()
