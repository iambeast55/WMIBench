"""Read-only multi-host selection, connection approval, and batch results."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from itertools import islice

from remoteops.ui.notices import inline_notice

from PySide6.QtCore import QObject, QRunnable, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QSplitter,
    QStyle, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from remoteops.commands.templates import command_read_only_modes, prepare_command
from remoteops.core.models import AuthMethod, CommandResult, SessionState
from remoteops.core.authentication import portable_kerberos
from remoteops.remoting.batch import BatchRunner, BatchTarget, BatchUpdate, batch_records, export_batch_csv
from remoteops.ui.errors import error_summary
from remoteops.ui.results import RecordTable, ResultViewer, icon_button
from remoteops.ui.icons import set_button_icon


class BatchSignals(QObject):
    updated = Signal(object)
    finished = Signal(str)


class BatchWorker(QRunnable):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner
        self.signals = BatchSignals()

    @Slot()
    def run(self):
        error = ""
        try:
            self.runner.run(self.signals.updated.emit)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        finally:
            self.signals.finished.emit(error)


class BatchDialog(QDialog):
    DISPLAY_LIMIT = 10000

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.manager = window.session_manager
        self.hosts = deepcopy(window.hosts)
        self.host_by_id = {host.id: host for host in self.hosts}
        self.profiles = {profile.id: deepcopy(profile) for profile in window.profiles}
        self.running = False
        self.cancellation_requested = False
        self.runner = self.worker = None
        self.updates = {}
        self.batch_hosts = []
        self.summary_rows = {}
        self.combined_dirty = False
        self.setWindowTitle("Multi-host execution")
        self.resize(1100, 740)
        self.setMinimumSize(820, 560)
        layout = QVBoxLayout(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Command"))
        self.command = QComboBox()
        self.command.setEditable(True)
        self.command.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.command.setAccessibleName("Multi-host command")
        self.templates = deepcopy(window.command_registry.templates)
        for template in sorted(self.templates, key=lambda t: (not t.favorite, t.alias)):
            if command_read_only_modes(template) or (template.plugin is not None and not template.plugin.modifies):
                self.command.addItem(template.alias)
                self.command.setItemData(self.command.count() - 1, template.description, Qt.ItemDataRole.ToolTipRole)
        self.command.setCurrentText("sysinfo")
        controls.addWidget(self.command, 1)
        controls.addWidget(QLabel("Parallel hosts"))
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 12)
        self.concurrency.setValue(4)
        controls.addWidget(self.concurrency)
        self.connect_new = QCheckBox("Connect disconnected hosts")
        controls.addWidget(self.connect_new)
        layout.addLayout(controls)
        self.allow_python = QCheckBox("Run Python plugins for this batch")
        self.allow_python.setChecked(False)
        self.allow_python.toggled.connect(lambda: self.refresh_preflight())
        layout.addWidget(self.allow_python)

        self.pages = QTabWidget()
        selection = QWidget()
        selection_layout = QVBoxLayout(selection)
        filter_row = QHBoxLayout()
        self.host_search = QLineEdit()
        self.host_search.setPlaceholderText("Search hosts, addresses, groups, or tags")
        self.host_search.setClearButtonEnabled(True)
        self.select_visible = QCheckBox("Select visible")
        self.select_visible.setTristate(True)
        filter_row.addWidget(self.host_search, 1)
        filter_row.addWidget(self.select_visible)
        selection_layout.addLayout(filter_row)
        self.host_table = QTableWidget(len(self.hosts), 6)
        self.host_table.setHorizontalHeaderLabels(["Use", "Host", "Address", "Backend", "Connection", "Preflight"])
        self._configure_table(self.host_table)
        self.host_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        for row, host in enumerate(self.hosts):
            checked = QTableWidgetItem()
            checked.setFlags(checked.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked.setCheckState(Qt.CheckState.Checked if host.id == window.current_host_id() else Qt.CheckState.Unchecked)
            self.host_table.setItem(row, 0, checked)
            for col, text in enumerate((host.name, host.address, self.backend_name(host), "", ""), start=1):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                self.host_table.setItem(row, col, item)
        selection_layout.addWidget(self.host_table)
        self.pages.addTab(selection, "Hosts")

        results = QWidget()
        results_layout = QVBoxLayout(results)
        self.summary_search = QLineEdit()
        self.summary_search.setPlaceholderText("Filter host outcomes")
        self.summary_search.setClearButtonEnabled(True)
        results_layout.addWidget(self.summary_search)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.summary = QTableWidget(0, 5)
        self.summary.setHorizontalHeaderLabels(["Host", "Status", "Backend", "Seconds", "Message"])
        self._configure_table(self.summary)
        for col, width in enumerate((160, 100, 110, 80)):
            self.summary.horizontalHeader().setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)
            self.summary.setColumnWidth(col, width)
        self.summary.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.summary.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.viewer = ResultViewer()
        splitter.addWidget(self.summary)
        splitter.addWidget(self.viewer)
        splitter.setSizes([260, 240])
        results_layout.addWidget(splitter)
        self.pages.addTab(results, "Host results")

        combined_page = QWidget()
        combined_layout = QVBoxLayout(combined_page)
        self.combined_count = QLabel("0 rows")
        combined_layout.addWidget(self.combined_count)
        self.combined = RecordTable()
        combined_layout.addWidget(self.combined)
        self.pages.addTab(combined_page, "Combined results")
        layout.addWidget(self.pages, 1)

        footer = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.export_button = icon_button(self, QStyle.StandardPixmap.SP_DialogSaveButton, "Export all batch results as CSV")
        self.export_button.setEnabled(False)
        self.run_button = QPushButton("Run selected")
        self.run_button.setObjectName("primary")
        set_button_icon(self.run_button, QStyle.StandardPixmap.SP_MediaPlay)
        self.cancel_button = QPushButton("Cancel queued")
        self.cancel_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        for button in (self.export_button, self.run_button, self.cancel_button, self.close_button):
            footer.addWidget(button)
        layout.addLayout(footer)
        self.preflight_timer = QTimer(self)
        self.preflight_timer.setSingleShot(True)
        self.preflight_timer.setInterval(150)
        self.preflight_timer.timeout.connect(self.refresh_preflight)
        self.command.currentTextChanged.connect(lambda: self.preflight_timer.start())
        self.connect_new.toggled.connect(self.refresh_preflight)
        self.host_table.itemChanged.connect(lambda item: self.refresh_preflight() if item.column() == 0 else None)
        self.host_search.textChanged.connect(self.filter_hosts)
        self.select_visible.checkStateChanged.connect(lambda state: self.check_visible(state != Qt.CheckState.Unchecked))
        self.summary_search.textChanged.connect(self.filter_summary)
        self.summary.itemSelectionChanged.connect(self.show_result)
        self.summary.model().layoutChanged.connect(self.filter_summary)
        self.pages.currentChanged.connect(self.page_changed)
        self.run_button.clicked.connect(self.start_batch)
        self.cancel_button.clicked.connect(self.cancel_queued)
        self.close_button.clicked.connect(self.reject)
        self.export_button.clicked.connect(self.export_all)
        self.state_timer = QTimer(self)
        self.state_timer.setInterval(1000)
        self.state_timer.timeout.connect(self.refresh_preflight)
        self.state_timer.start()
        self.combined_timer = QTimer(self)
        self.combined_timer.setSingleShot(True)
        self.combined_timer.setInterval(150)
        self.combined_timer.timeout.connect(self.refresh_combined)
        self.refresh_preflight()

    @staticmethod
    def _configure_table(table):
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(False)

    @staticmethod
    def backend_name(host):
        return "WMI/DCOM" if host.connection_mode.value == "wmi_dcom" else "WinRM"

    def selected_hosts(self):
        return [host for row, host in enumerate(self.hosts)
                if self.host_table.item(row, 0).checkState() == Qt.CheckState.Checked]

    def filter_hosts(self):
        query = self.host_search.text().strip().casefold()
        for row, host in enumerate(self.hosts):
            text = " ".join((host.name, host.address, host.group_name or "", *host.tags))
            self.host_table.setRowHidden(row, query not in text.casefold())
        self.sync_select_visible()

    def sync_select_visible(self):
        visible = [self.host_table.item(row, 0).checkState() == Qt.CheckState.Checked
                   for row in range(self.host_table.rowCount()) if not self.host_table.isRowHidden(row)]
        state = (Qt.CheckState.Checked if visible and all(visible) else
                 Qt.CheckState.PartiallyChecked if any(visible) else Qt.CheckState.Unchecked)
        self.select_visible.blockSignals(True)
        self.select_visible.setCheckState(state)
        self.select_visible.blockSignals(False)

    def check_visible(self, checked):
        self.host_table.blockSignals(True)
        for row in range(self.host_table.rowCount()):
            if not self.host_table.isRowHidden(row):
                self.host_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.host_table.blockSignals(False)
        self.refresh_preflight()

    def eligibility(self, host, command):
        profile = self.profiles.get(host.auth_profile_id)
        state = self.manager.session_state(host, profile)
        python_allowed = (self.allow_python.isChecked() and command.plugin is not None
                          and not command.plugin.modifies and host.connection_mode.value in command.plugin.backends)
        if command.help_text or (host.connection_mode.value not in command.read_only_modes and not python_allowed):
            return "Not verified read-only for this backend", state
        tab = self.window.host_tabs_by_id.get(host.id)
        if (tab and tab.busy) or self.manager.host_busy(host.id):
            return "Host is busy", state
        if host.auth_profile_id and profile is None:
            return "Credential profile is missing", state
        if state is SessionState.ERROR:
            return "Reconnect this host explicitly first", state
        if state in (SessionState.RUNNING, SessionState.CONNECTING, SessionState.DISCONNECTING):
            return "Host is busy", state
        if state is not SessionState.CONNECTED and profile is None and self.manager.session_factory is None:
            return "Credential profile required", state
        if state is not SessionState.CONNECTED and not self.connect_new.isChecked():
            return "New connection not approved", state
        return "", state

    def refresh_preflight(self):
        if self.running:
            return
        try:
            command = prepare_command(self.command.currentText(), self.templates)
            error = ""
        except ValueError as exc:
            command, error = None, str(exc)
        selected = {host.id for host in self.selected_hosts()}
        ready = 0
        for row, host in enumerate(self.hosts):
            reason, state = self.eligibility(host, command) if command else (error, self.manager.session_state(host, self.profiles.get(host.auth_profile_id)))
            label = reason or ("Reuse session" if state is SessionState.CONNECTED else "New connection")
            self.host_table.item(row, 4).setText(state.value.capitalize())
            self.host_table.item(row, 5).setText(label)
            self.host_table.item(row, 5).setToolTip(label)
            ready += int(host.id in selected and not reason)
        if self.pages.currentIndex() == 0 or not self.updates:
            self.status.setText(f"{len(selected)} selected / {ready} ready / {len(selected) - ready} skipped")
        self.run_button.setEnabled(bool(ready))
        self.sync_select_visible()

    def start_batch(self):
        if self.running:
            return
        targets = []
        try:
            # Freeze validated definitions before approval or credential prompts.
            if self.manager.registry:
                self.manager.registry.load()
                self.templates = deepcopy(self.manager.registry.templates)
            command = prepare_command(self.command.currentText(), self.templates)
            for host in self.selected_hosts():
                profile = self.profiles.get(host.auth_profile_id)
                reason, state = self.eligibility(host, command)
                targets.append(BatchTarget(host, command, profile,
                                           allow_connect=not reason and state is not SessionState.CONNECTED,
                                           skip_reason=reason, approve_python=self.allow_python.isChecked() and command.plugin is not None))
            eligible = [target for target in targets if not target.skip_reason]
            if not eligible:
                inline_notice(self, "No eligible hosts", "No selected host supports this command.")
                self.refresh_preflight()
                return
            new = [target for target in eligible if target.allow_connect]
            names = "\n".join(f"{target.host.name} ({target.host.address})" for target in new[:12])
            if len(new) > 12:
                names += f"\n... and {len(new) - 12} other selected hosts."
            text = (f"Run {self.command.currentText()} on {len(eligible)} hosts?\n"
                    f"Reuse {len(eligible) - len(new)} sessions; open {len(new)} new connections; "
                    f"skip {len(targets) - len(eligible)} hosts.")
            if names:
                text += "\n\nNew connections:\n" + names
            if command.plugin is not None:
                text += "\n\nPlugin version: " + command.plugin.digest[:12]
            if QMessageBox.question(self, "Confirm multi-host run", text,
                                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            credentials = {}
            for target in new:
                profile = target.profile
                if profile is None:
                    if self.manager.session_factory is None:
                        target.skip_reason = "A credential profile is required for a new connection."
                    continue
                if profile.id not in credentials:
                    try:
                        credentials[profile.id] = (self.window.resolve_credential_for_profile(profile), "")
                    except Exception as exc:
                        credentials[profile.id] = (None, f"Credential unavailable: {exc}")
                credential, failure = credentials[profile.id]
                if failure:
                    target.skip_reason = failure
                elif credential is None or ((profile.method in (AuthMethod.NTLM, AuthMethod.NTLM_HASH, AuthMethod.BASIC) or portable_kerberos(profile)) and not credential.secret):
                    target.skip_reason = "Credential missing; connection not attempted."
                else:
                    target.credential = credential
            self.runner = BatchRunner(self.manager, targets, self.concurrency.value())
        except (ValueError, OSError) as exc:
            inline_notice(self, "Batch not started", str(exc))
            return
        self.batch_hosts = [deepcopy(target.host) for target in targets]
        self.cancellation_requested = False
        self.updates.clear()
        self.summary.setSortingEnabled(False)
        self.summary.setRowCount(len(targets))
        self.summary_rows.clear()
        for row, target in enumerate(targets):
            self.summary_rows[target.host.id] = row
            for col, value in enumerate((target.host.name, "Queued", self.backend_name(target.host), "", "")):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                item.setData(Qt.ItemDataRole.UserRole, target.host.id)
                self.summary.setItem(row, col, item)
            target.credential = None
        self.viewer.text.clear()
        self.viewer.table.set_records([])
        self.combined.set_records([])
        self.combined_dirty = True
        self.export_button.setEnabled(False)
        self.set_running(True)
        self.pages.setCurrentIndex(1)
        self.worker = BatchWorker(self.runner)
        self.worker.signals.updated.connect(self.on_update)
        self.worker.signals.finished.connect(self.on_finished)
        self.window.thread_pool.start(self.worker)

    def set_running(self, running):
        self.running = running
        for widget in (self.command, self.concurrency, self.connect_new, self.host_table,
                       self.select_visible, self.run_button, self.close_button):
            widget.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    @Slot(object)
    def on_update(self, update):
        self.updates[update.host_id] = update
        row = self.summary_rows[update.host_id]
        message = error_summary(update.detail, update.session_state).replace("\n", " ") if update.status == "Failed" else update.detail
        duration = update.result.duration_seconds if update.result else None
        for col, text in ((1, update.status), (3, f"{duration:.2f}" if duration is not None else ""), (4, message)):
            self.summary.item(row, col).setText(text)
            self.summary.item(row, col).setToolTip(text)
        if duration is not None:
            self.summary.item(row, 3).setData(Qt.ItemDataRole.DisplayRole, round(duration, 2))
        self.combined_dirty = True
        self.combined_timer.start()
        self.filter_summary()
        self.show_result()
        self.update_progress()
        self.window.refresh_statuses()
        if update.result:
            tab = self.window.host_tabs_by_id.get(update.host_id)
            if tab:
                self.window.append_terminal_result(update.result, tab.terminal, batch=True)
                tab.history.reload()

    def update_progress(self):
        counts = Counter(item.status for item in self.updates.values())
        self.status.setText(" / ".join(f"{counts[name]} {name.lower()}" for name in ("Queued", "Running", "Succeeded", "Failed", "Skipped"))
                            + (" / Cancellation requested" if self.cancellation_requested and self.running else ""))

    def page_changed(self):
        self.refresh_combined()
        if self.running or (self.pages.currentIndex() != 0 and self.updates):
            self.update_progress()
        else:
            self.refresh_preflight()

    @Slot(str)
    def on_finished(self, error):
        self.set_running(False)
        if error:
            for host in self.batch_hosts:
                current = self.updates.get(host.id)
                if current is None or current.status in ("Queued", "Running"):
                    self.on_update(BatchUpdate(host.id, "Failed", detail=error,
                                              session_state=self.manager.session_state(host, self.profiles.get(host.auth_profile_id))))
        self.summary.setSortingEnabled(True)
        self.export_button.setEnabled(bool(self.updates))
        self.runner = self.worker = None
        self.refresh_combined()
        if self.summary.rowCount() and not self.summary.selectedItems():
            self.summary.selectRow(0)
        self.window.refresh_statuses()
        self.update_progress()

    def filter_summary(self):
        query = self.summary_search.text().strip().casefold()
        for row in range(self.summary.rowCount()):
            text = " ".join(self.summary.item(row, col).text() for col in range(5))
            self.summary.setRowHidden(row, query not in text.casefold())

    def show_result(self):
        item = self.summary.item(self.summary.currentRow(), 0)
        if item is None:
            return
        update = self.updates.get(item.data(Qt.ItemDataRole.UserRole))
        if update is None:
            return
        host = self.host_by_id[update.host_id]
        self.viewer.set_result(update.result or CommandResult(
            host.id, host.name, self.command.currentText(), stdout=update.status,
            stderr=update.detail, connection_mode=host.connection_mode.value,
        ))

    def refresh_combined(self, *_args):
        if self.pages.currentIndex() != 2 or not self.combined_dirty:
            return
        records = list(islice(batch_records(self.batch_hosts, self.updates), self.DISPLAY_LIMIT))
        total = sum(max(1, len(update.result.records)) if update.result else 1 for update in self.updates.values())
        self.combined.set_records(records)
        for col, width in enumerate((170, 130, 100)):
            self.combined.table.setColumnWidth(col, width)
        self.combined_count.setText(f"{len(records):,} of {total:,} rows" + (" / Display limit reached; full CSV available" if total > self.DISPLAY_LIMIT else ""))
        self.combined_dirty = False

    def export_all(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export all batch results", "multi-host-results.csv", "CSV files (*.csv)")
        if path:
            try:
                export_batch_csv(path, self.batch_hosts, self.updates)
            except OSError as exc:
                inline_notice(self, "Export failed", str(exc))

    def cancel_queued(self):
        if self.runner:
            self.runner.cancel()
            self.cancellation_requested = True
            self.cancel_button.setEnabled(False)
            self.status.setText("Cancellation requested / Already-dispatched hosts are finishing")

    def reject(self):
        if self.running:
            self.cancel_queued()
            return
        super().reject()

    def closeEvent(self, event):
        if self.running:
            self.cancel_queued()
            event.ignore()
        else:
            super().closeEvent(event)
