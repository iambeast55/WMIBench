from __future__ import annotations

import time
import sys
import traceback
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from threading import Event
from uuid import uuid4

from remoteops.ui.notices import inline_notice

from PySide6.QtCore import QObject, QRunnable, QSettings, QSize, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor, QFontDatabase, QIcon, QKeyEvent, QPainter, QPixmap,
    QTextBlockFormat, QTextCharFormat, QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QProgressBar,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTabBar,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from remoteops.commands.templates import CATEGORIES, TEMPLATES_BY_ALIAS, CommandRegistry, command_backends, command_category, command_line_from_values, parse_json, prepare_command, validate_template
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.authentication import DomainConnection, KERBEROS_METHODS, hostname, portable_kerberos, validate_secret
from remoteops.core.database import Repository
from remoteops.core.models import (
    AuthMethod,
    CommandResult,
    CommandTemplate,
    ConnectionMode,
    CredentialProfile,
    Host,
    SessionState,
    Transport,
)
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.theme import stylesheet
from remoteops.ui.results import FindBar, HistoryWidget
from remoteops.ui.errors import error_summary
from remoteops.ui.icons import refresh_icons, set_button_icon
from remoteops.ui.command_input import CommandInput
from remoteops.ui.host_tree import HostTree, ROLE


def tool_button(parent: QWidget, icon: QStyle.StandardPixmap, label: str) -> QToolButton:
    button = QToolButton(parent)
    set_button_icon(button, icon)
    button.setFixedSize(34, 34)
    button.setToolTip(label)
    button.setAccessibleName(label)
    return button


@dataclass(slots=True)
class WorkerOutcome:
    value: object | None = None
    error: str = ""


@dataclass(slots=True)
class PendingOperation:
    kind: str
    host_id: str
    terminal: QTextEdit
    started_at: float
    on_result: Callable | None = None


class WorkerSignals(QObject):
    finished = Signal(str, object)


class Worker(QRunnable):
    def __init__(self, operation_id: str, fn: Callable[[], object | None]) -> None:
        super().__init__()
        self.operation_id = operation_id
        self.fn = fn
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            value = self.fn()
        except Exception as exc:
            self.signals.finished.emit(
                self.operation_id, WorkerOutcome(error=_format_exception(exc))
            )
        else:
            self.signals.finished.emit(self.operation_id, WorkerOutcome(value=value))


class MainWindow(QMainWindow):
    transfer_progress = Signal(str, object, object)
    plugin_progress = Signal(str, str)

    def __init__(self, repository: Repository, session_manager: SessionManager) -> None:
        super().__init__()
        self.repository = repository
        self.session_manager = session_manager
        self.session_manager.credential_provider = self.prompt_for_credential
        self.setWindowTitle("WMIBench")
        self.settings = QSettings(str(repository.db_path.parent / "settings.ini"), QSettings.Format.IniFormat)
        self.dark_mode = self.settings.value("dark_mode", False, type=bool)
        self.setStyleSheet(stylesheet(self.dark_mode))
        self.setMinimumSize(820, 560)

        self.hosts: list[Host] = []
        self.profiles: list[CredentialProfile] = []
        self.host_tabs_by_id: dict[str, HostTerminalTab] = {}
        self.state_overrides: dict[str, SessionState] = {}
        self.pending_operations: dict[str, PendingOperation] = {}
        self.transfer_cancels: dict[str, Event] = {}
        self.plugin_cancels: dict[str, Event] = {}
        self.reported_errors = {}
        self.batch_dialog = None
        self.closing = False
        self.closed_sessions = False
        self.command_registry = session_manager.registry or CommandRegistry(self.repository.db_path.parent / "commands.json")
        session_manager.registry = self.command_registry
        session_manager.plugin_progress = self.plugin_progress.emit
        self.plugin_progress.connect(self.on_plugin_progress)
        self.thread_pool = QThreadPool.globalInstance()
        self.operation_timer = QTimer(self)
        self.operation_timer.setInterval(1000)
        self.operation_timer.timeout.connect(self.update_pending_operations)

        self._build_actions()
        self._build_layout()
        self.reload_data()
        self.transfer_progress.connect(self.on_transfer_progress)
        self.keepalive_timer = QTimer(self)
        self.keepalive_timer.setInterval(5000)
        self.keepalive_timer.timeout.connect(self.check_sessions)
        self.keepalive_timer.start()

    def _build_actions(self) -> None:
        toolbar = QToolBar("WMIBench")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(16, 16))
        self.addToolBar(toolbar)
        brand = QLabel("WMIBench")
        brand.setObjectName("brand")
        toolbar.addWidget(brand)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.auth_profiles_button = QPushButton("Credentials")
        self.commands_button = QPushButton("Commands")
        set_button_icon(self.auth_profiles_button, QStyle.StandardPixmap.SP_DirHomeIcon)
        set_button_icon(self.commands_button, "fa5s.terminal")
        toolbar.addWidget(self.auth_profiles_button)
        toolbar.addWidget(self.commands_button)
        self.batch_button = QPushButton("Multi-host")
        set_button_icon(self.batch_button, QStyle.StandardPixmap.SP_ComputerIcon)
        self.batch_button.clicked.connect(self.show_batch_dialog)
        toolbar.addWidget(self.batch_button)
        self.theme_button = tool_button(self, QStyle.StandardPixmap.SP_DesktopIcon, "Toggle dark mode")
        self.theme_button.setCheckable(True)
        self.theme_button.setChecked(self.dark_mode)
        self.theme_button.toggled.connect(self.set_dark_mode)
        toolbar.addWidget(self.theme_button)
        settings = tool_button(self, QStyle.StandardPixmap.SP_FileDialogContentsView, "More actions")
        set_button_icon(settings, "fa5s.ellipsis-v")
        menu = QMenu(settings)
        menu.addAction("Add credential", self.add_profile)
        menu.addSeparator()
        menu.addAction("Use test backend", self.use_fake_backend)
        settings.setMenu(menu)
        settings.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        toolbar.addWidget(settings)
        self.auth_profiles_button.clicked.connect(self.show_auth_profiles)
        self.commands_button.clicked.connect(self.show_command_library)

    def _build_layout(self) -> None:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.setHandleWidth(1)

        left = self.host_rail = QWidget()
        left.setObjectName("hostRail")
        left.setMinimumWidth(170)
        left.setMaximumWidth(320)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(12, 14, 12, 10)
        left_layout.setSpacing(12)
        heading = QHBoxLayout()
        title = QLabel("HOSTS")
        title.setObjectName("sectionTitle")
        heading.addWidget(title)
        heading.addStretch()
        add_group = tool_button(self, "fa5s.folder-plus", "Add group")
        add_group.clicked.connect(self.add_group)
        heading.addWidget(add_group)
        self.add_host_button = tool_button(self, QStyle.StandardPixmap.SP_FileDialogNewFolder, "Add host")
        self.add_host_button.clicked.connect(self.add_host)
        heading.addWidget(self.add_host_button)
        left_layout.addLayout(heading)
        self.host_search = QLineEdit()
        self.host_search.setPlaceholderText("Search hosts")
        self.host_search.setClearButtonEnabled(True)
        self.host_search.textChanged.connect(self.filter_hosts)
        left_layout.addWidget(self.host_search)
        self.host_tree = HostTree()
        self.host_tree.itemSelectionChanged.connect(self.activate_selected_host)
        self.host_tree.itemDoubleClicked.connect(lambda item, _column: self.edit_current_host() if item.parent() else None)
        self.host_tree.customContextMenuRequested.connect(self.show_host_menu)
        self.host_tree.order_changed.connect(self.save_inventory_order)
        self.host_tree.itemExpanded.connect(self.save_group_expansion)
        self.host_tree.itemCollapsed.connect(self.save_group_expansion)
        left_layout.addWidget(self.host_tree)
        self.host_count = QLabel()
        self.host_count.setObjectName("muted")
        left_layout.addWidget(self.host_count)
        splitter.addWidget(left)

        self.host_tabs = QStackedWidget()
        self.host_tabs.currentChanged.connect(self.sync_host_selection_from_tab)
        splitter.addWidget(self.host_tabs)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 990])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage("WMI / WinRM")

    def filter_hosts(self) -> None:
        query = self.host_search.text().strip().casefold()
        self.host_tree.blockSignals(True)
        visible = self.host_tree.filter_hosts(query)
        if not query:
            collapsed = self.collapsed_groups()
            for name, item in self.host_tree.group_items.items():
                item.setExpanded(name not in collapsed)
        self.host_tree.blockSignals(False)
        self.host_count.setText(f"{visible} of {len(self.hosts)} hosts")

    def reload_data(self) -> None:
        current_host_id = self.current_host_id()
        self.hosts = self.repository.list_hosts()
        self.profiles = self.repository.list_credential_profiles()
        self._reload_host_table()
        self._reload_host_tabs()
        self.select_host(current_host_id if current_host_id in self.host_tabs_by_id else (self.hosts[0].id if self.hosts else None))
        self.refresh_statuses()

    def _reload_host_table(self) -> None:
        self.host_tree.populate(self.hosts, self.repository.list_host_groups(), self.collapsed_groups())
        states = self.session_manager.states()
        for host in self.hosts:
            state = self.state_overrides.get(
                host.id,
                states.get((host.id, host.auth_profile_id), SessionState.DISCONNECTED),
            )
            self.host_tree.host_items[host.id].setIcon(0, status_icon(state))
        self.filter_hosts()

    def _reload_host_tabs(self) -> None:
        self.host_tabs.blockSignals(True)
        # Keep live terminals and worker destinations intact when inventory changes.
        existing_ids = {host.id for host in self.hosts}
        for host_id in list(self.host_tabs_by_id):
            if host_id not in existing_ids:
                tab = self.host_tabs_by_id.pop(host_id)
                self.host_tabs.removeWidget(tab)
                tab.deleteLater()

        if not self.hosts:
            if not self.host_tabs.count():
                empty = QWidget()
                content = QVBoxLayout(empty)
                content.addStretch()
                title = QLabel("No hosts")
                title.setObjectName("hostTitle")
                title.setAlignment(Qt.AlignmentFlag.AlignCenter)
                content.addWidget(title)
                add = QPushButton("Add host")
                add.setObjectName("primary")
                add.clicked.connect(self.add_host)
                content.addWidget(add, 0, Qt.AlignmentFlag.AlignHCenter)
                content.addStretch()
                self.host_tabs.addWidget(empty)
            self.host_tabs.blockSignals(False)
            return

        for host in self.hosts:
            if host.id in self.host_tabs_by_id:
                tab = self.host_tabs_by_id[host.id]
                tab.host = host
            else:
                if self.host_tabs.count() and not isinstance(self.host_tabs.widget(0), HostTerminalTab):
                    empty = self.host_tabs.widget(0)
                    self.host_tabs.removeWidget(empty)
                    empty.deleteLater()
                tab = HostTerminalTab(self, host)
                self.host_tabs_by_id[host.id] = tab
                self.host_tabs.addWidget(tab)
            tab.reload_profiles()
            tab.refresh()
        self.host_tabs.blockSignals(False)

    def refresh_statuses(self) -> None:
        states = self.session_manager.states()
        for row, host in enumerate(self.hosts):
            state = self.state_overrides.get(
                host.id,
                states.get((host.id, host.auth_profile_id), SessionState.DISCONNECTED),
            )
            self.host_tree.host_items[host.id].setIcon(0, status_icon(state))
            tab = self.host_tabs_by_id.get(host.id)
            if tab:
                tab.refresh()

    def current_host_id(self) -> str | None:
        current = self.host_tabs.currentWidget()
        if isinstance(current, HostTerminalTab):
            return current.host.id
        return None

    def host_context_menu(self, host):
        menu = QMenu(self)
        state = self.state_for_host(host)
        busy = self.session_manager.host_busy(host.id) or self.host_tabs_by_id[host.id].busy
        connect = menu.addAction("Connect")
        disconnect = menu.addAction("Disconnect")
        menu.addSeparator()
        edit = menu.addAction("Edit host")
        connect.setEnabled(not busy and state in {SessionState.DISCONNECTED, SessionState.ERROR})
        disconnect.setEnabled(not busy and state in {SessionState.CONNECTED, SessionState.ERROR})
        edit.setEnabled(not busy and state in {SessionState.DISCONNECTED, SessionState.ERROR})
        terminal = self.host_tabs_by_id[host.id].terminal
        connect.triggered.connect(lambda: self.connect_host(host, terminal))
        disconnect.triggered.connect(lambda: self.disconnect_host(host, terminal))
        edit.triggered.connect(lambda: (self.select_host(host.id), self.edit_current_host()))
        move = menu.addMenu("Move to group")
        for name in [*self.repository.list_host_groups(), None]:
            action = move.addAction(name or "Ungrouped")
            action.setEnabled(name != host.group_name)
            action.triggered.connect(lambda checked=False, name=name: self.host_tree.move_host(host.id, name))
        siblings = dict(self.host_tree.inventory())[host.group_name]
        position = siblings.index(host.id)
        up = menu.addAction("Move up", lambda: self.host_tree.move_host(host.id, host.group_name, position - 1))
        down = menu.addAction("Move down", lambda: self.host_tree.move_host(host.id, host.group_name, position + 1))
        up.setEnabled(position > 0)
        down.setEnabled(position < len(siblings) - 1)
        menu.addSeparator()
        delete = menu.addAction("Delete host", lambda: self.delete_host(host))
        delete.setEnabled(not busy)
        return menu

    def show_host_menu(self, position):
        item = self.host_tree.itemAt(position)
        identity = item.data(0, ROLE) if item else None
        if identity and identity[0] == "host":
            host = next(h for h in self.hosts if h.id == identity[1])
            self.select_host(host.id)
            menu = self.host_context_menu(host)
        else:
            name = identity[1] if identity else None
            menu = QMenu(self)
            menu.addAction("Add host", self.add_host)
            menu.addAction("Add group", self.add_group)
            if name is not None:
                menu.addAction("Rename group", lambda: self.rename_group(name))
                menu.addAction("Delete group", lambda: self.delete_group(name))
                groups = self.repository.list_host_groups()
                index = groups.index(name)
                up = menu.addAction("Move up", lambda: self.host_tree.move_group(name, index - 1))
                down = menu.addAction("Move down", lambda: self.host_tree.move_group(name, index + 1))
                up.setEnabled(index > 0)
                down.setEnabled(index < len(groups) - 1)
            if item:
                self.host_tree.setCurrentItem(item)
        menu.exec(self.host_tree.viewport().mapToGlobal(position))
        menu.deleteLater()

    def current_host(self) -> Host | None:
        host_id = self.current_host_id()
        for host in self.hosts:
            if host.id == host_id:
                return host
        return None

    def select_host(self, host_id: str | None) -> None:
        if host_id is None:
            return
        self.host_tree.select_host(host_id)
        tab = self.host_tabs_by_id.get(host_id)
        if tab:
            self.host_tabs.setCurrentWidget(tab)

    def activate_selected_host(self) -> None:
        host_id = self.host_tree.selected_host_id()
        if host_id:
            self.select_host(host_id)

    def sync_host_selection_from_tab(self) -> None:
        host_id = self.current_host_id()
        if host_id:
            self.select_host(host_id)

    def add_host(self) -> None:
        dialog = HostDialog(self.profiles, parent=self)
        item = self.host_tree.currentItem()
        if item:
            group = item.parent() or item
            dialog.group_name.setText(group.data(0, ROLE)[1] or "")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            host = self.repository.save_host(dialog.host())
            self.reload_data()
            self.select_host(host.id)

    def collapsed_groups(self):
        try:
            value = json.loads(self.settings.value("collapsed_host_groups", "[]"))
            return value if isinstance(value, list) else []
        except (TypeError, ValueError):
            return []

    def save_group_expansion(self, *_):
        if not self.host_search.text().strip():
            self.settings.setValue("collapsed_host_groups", json.dumps(
                [name for name, item in self.host_tree.group_items.items() if not item.isExpanded()]))

    def inventory_change(self, operation):
        try:
            operation()
        except (ValueError, OSError, sqlite3.Error) as exc:
            inline_notice(self, "Host inventory", str(exc))
        self.reload_data()

    def save_inventory_order(self, groups):
        self.inventory_change(lambda: self.repository.save_inventory_order(groups))

    def add_group(self):
        name, accepted = QInputDialog.getText(self, "Add group", "Name")
        if accepted:
            self.inventory_change(lambda: self.repository.create_host_group(name))

    def rename_group(self, name):
        new, accepted = QInputDialog.getText(self, "Rename group", "Name", text=name)
        if accepted:
            collapsed = self.collapsed_groups()
            self.inventory_change(lambda: self.repository.rename_host_group(name, new))
            if name in collapsed and new.strip() in self.host_tree.group_items:
                self.host_tree.group_items[new.strip()].setExpanded(False)

    def delete_group(self, name):
        if QMessageBox.question(self, "Delete group", f"Delete {name}? Hosts become ungrouped.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.inventory_change(lambda: self.repository.delete_host_group(name))

    def delete_host(self, host):
        tab = self.host_tabs_by_id[host.id]
        if tab.busy or self.session_manager.host_busy(host.id):
            return
        if QMessageBox.question(self, "Delete host", f"Delete {host.name}? History and credentials remain.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        def removed(outcome):
            if not outcome.error:
                self.inventory_change(lambda: self.repository.delete_host(host.id))
        self.set_host_busy(host.id, True)
        self.set_host_state(host.id, SessionState.DISCONNECTING)
        self.start_worker("disconnect", host.id, tab.terminal,
                          lambda: self.session_manager.disconnect(host), on_result=removed)

    def edit_current_host(self) -> None:
        host = self.current_host()
        if host is None:
            inline_notice(self, "Edit host", "No host selected.")
            return
        if self.state_for_host(host) not in {SessionState.DISCONNECTED, SessionState.ERROR}:
            inline_notice(self, "Host in use", "Disconnect the host before editing it.")
            return

        dialog = HostDialog(self.profiles, host=host, parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.repository.save_host(dialog.host())
            self.reload_data()

    def add_profile(self) -> None:
        dialog = CredentialDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_data()

    def profile_in_use(self, profile_id):
        return any(host.auth_profile_id == profile_id and (
            self.state_for_host(host) not in {SessionState.DISCONNECTED, SessionState.ERROR}
            or self.session_manager.host_busy(host.id)) for host in self.hosts)

    def edit_profile(self, profile):
        if self.profile_in_use(profile.id):
            inline_notice(self, "Credential in use", "Disconnect assigned hosts before editing it.")
            return
        dialog = CredentialDialog(self, profile=profile)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.reload_data()

    def delete_profile(self, profile):
        if self.profile_in_use(profile.id):
            inline_notice(self, "Credential in use", "Disconnect assigned hosts before deleting it.")
            return
        count = sum(host.auth_profile_id == profile.id for host in self.hosts)
        if QMessageBox.question(self, "Delete credential", f"Delete {profile.name} and its saved secret?\n"
                                f"Assignments cleared: {count}.",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.repository.delete_credential_profile(profile.id)
        except Exception:
            inline_notice(self, "Credential not deleted", "Database update failed.")
            return
        self.reload_data()

    def show_auth_profiles(self) -> None:
        AuthProfilesDialog(self.profiles, self).exec()

    def show_command_library(self) -> None:
        dialog = CommandLibraryDialog(self.command_registry, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.command_registry.load()
            except (ValueError, OSError) as exc:
                inline_notice(self, "Command library not reloaded", str(exc))

    def show_batch_dialog(self):
        from remoteops.ui.batch_dialog import BatchDialog

        if self.batch_dialog is not None:
            return
        try:
            self.command_registry.load()
        except (ValueError, OSError) as exc:
            inline_notice(self, "Command library unavailable", str(exc))
            return
        dialog = self.batch_dialog = BatchDialog(self)
        try:
            dialog.exec()
        finally:
            self.batch_dialog = None
            dialog.deleteLater()

    def set_dark_mode(self, enabled):
        self.dark_mode = enabled
        self.settings.setValue("dark_mode", enabled)
        self.setStyleSheet(stylesheet(enabled))
        refresh_icons(self)

    def check_sessions(self):
        if self.closing:
            return
        self.session_manager.schedule_keepalives()
        for host_id, error in self.session_manager.errors().items():
            if self.reported_errors.get(host_id) != error:
                tab = self.host_tabs_by_id.get(host_id)
                if tab and not tab.busy:
                    self.present_error(host_id, tab.terminal, error)
                    self.reported_errors[host_id] = error
        self.refresh_statuses()

    def connect_host(self, host: Host, terminal: QTextEdit) -> None:
        append_terminal(terminal, f"wmibench> connect {host.name}", "prompt")
        append_terminal(terminal, f"endpoint: {host.endpoint}", "muted")
        append_terminal(terminal, f"auth: {self.profile_name(host.auth_profile_id)}", "muted")
        profile = self.profile_for_host(host)
        credential = self.resolve_credential_for_profile(profile)
        if profile and credential is None and (profile.method in {AuthMethod.NTLM, AuthMethod.NTLM_HASH, AuthMethod.BASIC} or portable_kerberos(profile)):
            append_terminal(terminal, "[connection cancelled: credential required]", "muted")
            return
        append_terminal(terminal, "[connecting...]", "progress")
        self.set_host_state(host.id, SessionState.CONNECTING)
        self.set_host_busy(host.id, True)

        self.start_worker(
            "connect",
            host.id,
            terminal,
            lambda: self.session_manager.connect_with_credential(host, profile, credential),
        )

    def disconnect_host(self, host: Host, terminal: QTextEdit) -> None:
        append_terminal(terminal, f"wmibench> disconnect {host.name}", "prompt")
        append_terminal(terminal, "[disconnecting...]", "progress")
        self.set_host_state(host.id, SessionState.DISCONNECTING)
        self.set_host_busy(host.id, True)

        self.start_worker(
            "disconnect", host.id, terminal, lambda: self.session_manager.disconnect(host)
        )

    def run_host_command(self, host: Host, command_line: str, terminal: QTextEdit, *, on_result=None, prepared_command=None):
        if not command_line.strip():
            return
        append_terminal(terminal, f"wmibench> {command_line}", "prompt")
        try:
            command = prepared_command or self.session_manager.prepare(command_line, host)
        except (ValueError, OSError) as exc:
            append_terminal(terminal, str(exc), "error")
            return
        if command.help_text:
            append_terminal(terminal, command.help_text, "stdout")
            return
        if command.alias == "restart-service" and (host.connection_mode is ConnectionMode.WINRM or command.wmi_plan is None):
            answer = QMessageBox.question(
                self, "Restart service",
                f"Restart {command.values['service_name']} on {host.name}?\n"
                + ("Dependent services are not stopped." if host.connection_mode is ConnectionMode.WMI
                   else "Dependent services may also be affected."),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                append_terminal(terminal, "[service restart cancelled]", "muted")
                return
        profile = self.profile_for_host(host)
        connected = self.state_for_host(host) is SessionState.CONNECTED
        credential = None if connected else self.resolve_credential_for_profile(profile)
        if not connected and profile and credential is None and (profile.method in {AuthMethod.NTLM, AuthMethod.NTLM_HASH, AuthMethod.BASIC} or portable_kerberos(profile)):
            append_terminal(terminal, "[run cancelled: credential required]", "muted")
            return
        append_terminal(terminal, "[running...]", "progress")
        cancel = Event() if command.plugin is not None else None
        if cancel is not None:
            self.plugin_cancels[host.id] = cancel
        self.set_host_state(host.id, SessionState.RUNNING)
        self.set_host_busy(host.id, True)

        def run() -> CommandResult:
            try:
                options = {"cancel": cancel} if cancel is not None else {}
                return self.session_manager.run_prepared_with_credential(host, command, profile, credential, **options)
            except Exception as exc:
                return CommandResult(
                    host_id=host.id,
                    host_name=host.name,
                    command=command.script,
                    stderr=_format_exception(exc),
                    exit_code=1,
                )

        self.start_worker("run", host.id, terminal, run, on_result=on_result)
        return True

    @Slot(str, str)
    def on_plugin_progress(self, host_id, message):
        tab = self.host_tabs_by_id.get(host_id)
        if tab:
            append_terminal(tab.terminal, message, "progress")

    def cancel_host_operation(self, host_id):
        if host_id in self.transfer_cancels:
            self.transfer_cancels[host_id].set()
        elif host_id in self.plugin_cancels:
            self.plugin_cancels[host_id].set()
            tab = self.host_tabs_by_id.get(host_id)
            if tab:
                append_terminal(tab.terminal, "[cancellation requested; waiting for a command checkpoint]", "muted")

    def transfer_file(self, host, direction, remote_path=None):
        from remoteops.ui.transfer_dialog import TransferDialog
        dialog = TransferDialog(direction, self)
        if remote_path is not None:
            if direction == "upload":
                dialog.set_remote_directory(remote_path)
            else:
                dialog.remote.setText(remote_path)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        tab = self.host_tabs_by_id[host.id]
        from_tools = remote_path is not None
        cancel = self.transfer_cancels[host.id] = Event()
        local_path, remote_path, overwrite = dialog.local.text(), dialog.remote.text(), dialog.overwrite.isChecked()
        self.set_host_state(host.id, SessionState.RUNNING)
        tab.set_busy(True)
        append_terminal(tab.terminal, f"[{direction}: {dialog.remote.text()}]", "progress")
        self.start_worker("transfer", host.id, tab.terminal, lambda: self.session_manager.transfer(
            host, direction, local_path, remote_path, overwrite, cancel,
            lambda done, total: self.transfer_progress.emit(host.id, done, total),
        ), on_result=tab.tools_panel.transferred if from_tools else None)

    @Slot(str, object, object)
    def on_transfer_progress(self, host_id, done, total):
        tab = self.host_tabs_by_id.get(host_id)
        if tab:
            tab.progress.setRange(0, 100)
            tab.progress.setValue(int(done / total * 100) if total else 0)
            tab.activity_label.setText(f"Transfer / {done:,} of {total:,} bytes")

    def start_worker(
        self,
        kind: str,
        host_id: str,
        terminal: QTextEdit,
        fn: Callable[[], object | None],
        *, on_result=None,
    ) -> None:
        operation_id = str(uuid4())
        self.pending_operations[operation_id] = PendingOperation(
            kind=kind,
            host_id=host_id,
            terminal=terminal,
            started_at=time.monotonic(),
            on_result=on_result,
        )
        worker = Worker(operation_id, fn)
        worker.signals.finished.connect(self.on_worker_finished)
        self.thread_pool.start(worker)
        if not self.operation_timer.isActive():
            self.operation_timer.start()

    @Slot(str, object)
    def on_worker_finished(self, operation_id: str, outcome: object) -> None:
        operation = self.pending_operations.pop(operation_id, None)
        if operation is None:
            return
        if not isinstance(outcome, WorkerOutcome):
            outcome = WorkerOutcome(error=f"Unexpected worker result: {outcome!r}")

        if operation.kind == "connect":
            self.finish_connect(operation.host_id, operation.terminal, outcome)
        elif operation.kind == "disconnect":
            self.finish_disconnect(operation.host_id, operation.terminal, outcome)
        elif operation.kind == "run":
            self.plugin_cancels.pop(operation.host_id, None)
            self.finish_run(operation.host_id, operation.terminal, outcome)
        elif operation.kind == "tool":
            self.plugin_cancels.pop(operation.host_id, None)
            self.set_host_busy(operation.host_id, False)
            self.clear_host_state(operation.host_id)
            self.refresh_statuses()
            tab = self.host_tabs_by_id.get(operation.host_id)
            if tab:
                tab.history.reload()
            if outcome.error:
                self.present_error(operation.host_id, operation.terminal, outcome.error)
            elif isinstance(outcome.value, CommandResult) and not outcome.value.succeeded:
                self.present_error(operation.host_id, operation.terminal, outcome.value.stderr)
            self.finish_activity(operation.host_id, "Tool finished")
        elif operation.kind == "transfer":
            self.transfer_cancels.pop(operation.host_id, None)
            self.set_host_busy(operation.host_id, False)
            self.clear_host_state(operation.host_id)
            if outcome.error:
                self.present_error(operation.host_id, operation.terminal, outcome.error)
            else:
                append_terminal(operation.terminal, str(outcome.value), "success")
            self.finish_activity(operation.host_id, "Transfer failed or cancelled" if outcome.error else "Transfer complete")
        elif operation.kind == "shutdown":
            self.closed_sessions = True
            self.close()
        if operation.on_result:
            operation.on_result(outcome)
        if not self.pending_operations:
            self.operation_timer.stop()

    def update_pending_operations(self) -> None:
        if not self.pending_operations:
            self.operation_timer.stop()
            return

        for operation in self.pending_operations.values():
            elapsed = int(time.monotonic() - operation.started_at)
            tab = self.host_tabs_by_id.get(operation.host_id)
            if tab and operation.kind != "transfer":
                tab.activity_label.setText(f"{operation.kind.capitalize()} ({elapsed} s)")

    def finish_connect(self, host_id: str, terminal: QTextEdit, outcome: WorkerOutcome) -> None:
        self.set_host_busy(host_id, False)
        if outcome.error:
            self.set_host_state(host_id, SessionState.ERROR)
            append_terminal(terminal, "[connection failed]", "error")
            self.present_error(host_id, terminal, outcome.error)
        else:
            self.clear_host_state(host_id)
            self.reported_errors.pop(host_id, None)
            append_terminal(terminal, "[connected]", "success")
        self.refresh_statuses()
        self.finish_activity(host_id, "Connection failed" if outcome.error else "Session established")
        terminal.verticalScrollBar().setValue(terminal.verticalScrollBar().maximum())

    def finish_disconnect(self, host_id: str, terminal: QTextEdit, outcome: WorkerOutcome) -> None:
        self.set_host_busy(host_id, False)
        if outcome.error:
            self.set_host_state(host_id, SessionState.ERROR)
            append_terminal(terminal, "[disconnect failed]", "error")
            self.present_error(host_id, terminal, outcome.error)
        else:
            self.set_host_state(host_id, SessionState.DISCONNECTED)
            append_terminal(terminal, "[disconnected]", "success")
        self.refresh_statuses()
        self.finish_activity(host_id, "Disconnect failed" if outcome.error else "Session closed")
        terminal.verticalScrollBar().setValue(terminal.verticalScrollBar().maximum())

    def finish_run(self, host_id: str, terminal: QTextEdit, outcome: WorkerOutcome) -> None:
        self.set_host_busy(host_id, False)
        if outcome.error:
            self.clear_host_state(host_id)
            append_terminal(terminal, "[failed]", "error")
            self.present_error(host_id, terminal, outcome.error)
        elif isinstance(outcome.value, CommandResult):
            self.append_terminal_result(outcome.value, terminal)
            tab = self.host_tabs_by_id.get(host_id)
            if tab:
                tab.history.reload()
            host = next((host for host in self.hosts if host.id == host_id), None)
            session_state = self.session_manager.states().get(
                (host_id, host.auth_profile_id if host else None)
            )
            if outcome.value.succeeded or session_state is SessionState.CONNECTED:
                self.clear_host_state(host_id)
            else:
                self.set_host_state(host_id, SessionState.ERROR)
        else:
            self.clear_host_state(host_id)
        self.refresh_statuses()
        result = outcome.value
        if isinstance(result, CommandResult):
            duration = result.duration_seconds
            suffix = f" / {duration:.2f}s" if duration is not None else ""
            self.finish_activity(host_id, ("Completed" if result.succeeded else "Command failed") + suffix)
        else:
            self.finish_activity(host_id, "Command failed" if outcome.error else "Completed")

    def finish_activity(self, host_id: str, message: str) -> None:
        tab = self.host_tabs_by_id.get(host_id)
        if tab:
            tab.activity_label.setText(message)
            if self.host_tabs.currentWidget() is tab and tab.view_tabs.currentIndex() == 0:
                tab.command_input.setFocus()

    def append_terminal_result(self, result: CommandResult, terminal: QTextEdit, *, batch=False) -> None:
        if batch:
            append_terminal(terminal, f"wmibench [batch]> {result.command_line}", "prompt")
        duration = "" if result.duration_seconds is None else f" in {result.duration_seconds:.2f}s"
        status = "ok" if result.succeeded else "failed"
        append_terminal(terminal, f"[{status}{duration}]", "success" if result.succeeded else "error")
        if result.stdout.strip():
            append_terminal(terminal, result.stdout.rstrip(), "stdout")
        if result.stderr.strip():
            self.present_error(result.host_id, terminal, result.stderr.rstrip())
        terminal.verticalScrollBar().setValue(terminal.verticalScrollBar().maximum())

    def present_error(self, host_id, terminal, detail):
        host = next((host for host in self.hosts if host.id == host_id), None)
        state = self.session_manager.states().get((host_id, host.auth_profile_id if host else None))
        summary = error_summary(detail, state)
        append_terminal(terminal, summary, "error")
        if detail.strip() != summary.strip():
            append_terminal(terminal, detail.rstrip(), "error")
        if state is SessionState.ERROR:
            self.reported_errors[host_id] = detail

    def use_fake_backend(self) -> None:
        if self.pending_operations:
            inline_notice(self, "Operations active", "Finish current operations before switching backends.")
            return
        self.session_manager.disconnect_all()
        self.session_manager.use_fake_backend()
        self.reload_data()
        self.statusBar().showMessage("TEST BACKEND / No remote connection")
        QMessageBox.information(
            self,
            "Fake backend enabled",
            "Commands run locally; no remote session is used.",
        )

    def prompt_for_credential(self, profile: CredentialProfile) -> ResolvedCredential | None:
        label = {AuthMethod.NTLM_HASH: "NT hash", AuthMethod.KERBEROS_RC4: "RC4 key",
                 AuthMethod.KERBEROS_AES: "AES key"}.get(profile.method, "Password")
        secret, ok = QInputDialog.getText(
            self,
            "Credential required",
            f"{label} for {profile.username}",
            QLineEdit.EchoMode.Password,
        )
        if not ok:
            return None
        return ResolvedCredential(username=profile.username, secret=secret)

    def profile_name(self, profile_id: str | None) -> str:
        if profile_id is None:
            return "None"
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile.name
        return "Missing profile"

    def profile_for_host(self, host: Host) -> CredentialProfile | None:
        if host.auth_profile_id is None:
            return None
        for profile in self.profiles:
            if profile.id == host.auth_profile_id:
                return profile
        return None

    def resolve_credential_for_profile(
        self, profile: CredentialProfile | None
    ) -> ResolvedCredential | None:
        if profile is None:
            return None

        try:
            credential = self.session_manager.credential_store.resolve(profile)
        except (RuntimeError, OSError) as exc:
            inline_notice(self, "Credential unavailable", str(exc))
            return None
        needs_secret = profile.method in {AuthMethod.NTLM, AuthMethod.NTLM_HASH, AuthMethod.BASIC} or portable_kerberos(profile)
        if credential.secret or not needs_secret:
            return credential
        return self.prompt_for_credential(profile)

    def state_for_host(self, host: Host) -> SessionState:
        if host.id in self.state_overrides:
            return self.state_overrides[host.id]
        return self.session_manager.states().get(
            (host.id, host.auth_profile_id), SessionState.DISCONNECTED
        )

    def set_host_state(self, host_id: str, state: SessionState) -> None:
        self.state_overrides[host_id] = state
        self.refresh_statuses()

    def clear_host_state(self, host_id: str) -> None:
        self.state_overrides.pop(host_id, None)
        self.refresh_statuses()

    def set_host_busy(self, host_id: str, busy: bool) -> None:
        tab = self.host_tabs_by_id.get(host_id)
        if tab:
            tab.set_busy(busy)

    def _status_item(self, state: SessionState) -> QTableWidgetItem:
        item = QTableWidgetItem("")
        item.setIcon(status_icon(state))
        item.setToolTip(state.value)
        return item

    def closeEvent(self, event):
        if self.batch_dialog is not None and self.batch_dialog.running:
            self.batch_dialog.cancel_queued()
            event.ignore()
            return
        if self.closed_sessions:
            event.accept()
            return
        event.ignore()
        if self.pending_operations:
            self.statusBar().showMessage("Active operations must finish before closing.")
            return
        if not self.closing:
            self.closing = True
            for timer in self.findChildren(QTimer):
                if timer is not self.operation_timer:
                    timer.stop()
            self.setEnabled(False)
            terminal = next(iter(self.host_tabs_by_id.values())).terminal if self.host_tabs_by_id else QTextEdit(self)
            self.start_worker("shutdown", "", terminal, self.session_manager.shutdown)


class HostTerminalTab(QWidget):
    def __init__(self, window: MainWindow, host: Host) -> None:
        super().__init__()
        self.window = window
        self.host = host
        self.busy = False
        self.inspection_identity = (host.endpoint, host.auth_profile_id)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 4)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self.status_label = QLabel()
        self.status_text = QLabel()
        header.addWidget(self.status_label)
        header.addWidget(self.status_text)
        self.endpoint_label = ElidedLabel()
        self.endpoint_label.setObjectName("muted")
        self.endpoint_label.setMinimumWidth(0)
        self.endpoint_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        header.addWidget(self.endpoint_label, 1)

        self.connect_button = tool_button(self, QStyle.StandardPixmap.SP_MediaPlay, "Connect")
        self.connect_button.setObjectName("primary")
        self.disconnect_button = tool_button(self, QStyle.StandardPixmap.SP_MediaStop, "Disconnect")
        self.edit_button = tool_button(self, QStyle.StandardPixmap.SP_FileDialogContentsView, "Edit host")
        header.addWidget(self.connect_button)
        header.addWidget(self.disconnect_button)
        self.auth_combo = QComboBox()
        self.auth_combo.setFixedWidth(160)
        self.auth_combo.setAccessibleName("Host credential")
        self.auth_combo.currentIndexChanged.connect(self.change_profile)
        header.addWidget(self.auth_combo)
        header.addWidget(self.edit_button)
        layout.addLayout(header)

        surface = QWidget()
        surface.setObjectName("terminalSurface")
        console = QVBoxLayout(surface)
        console.setContentsMargins(0, 0, 0, 0)
        console.setSpacing(0)
        terminal_header = QWidget()
        terminal_header.setObjectName("terminalHeader")
        header_layout = QVBoxLayout(terminal_header)
        header_layout.setContentsMargins(8, 4, 8, 4)
        view_row = QHBoxLayout()
        self.view_tabs = QTabBar()
        self.view_tabs.setObjectName("hostViews")
        self.view_tabs.setDrawBase(False)
        self.view_tabs.setExpanding(False)
        self.view_tabs.addTab("Terminal")
        self.view_tabs.addTab("History")
        self.view_tabs.addTab("Services")
        self.view_tabs.addTab("Events")
        self.view_tabs.addTab("Tools")
        self.view_tabs.addTab("Explorer")
        self.view_tabs.setUsesScrollButtons(True)
        self.view_tabs.setElideMode(Qt.TextElideMode.ElideRight)
        view_row.addWidget(self.view_tabs, 1)
        self.transport_label = QLabel()
        view_row.addWidget(self.transport_label)
        header_layout.addLayout(view_row)
        terminal_tools = QWidget()
        tools = QHBoxLayout(terminal_tools)
        tools.setContentsMargins(0, 0, 0, 0)
        header_layout.addWidget(terminal_tools)
        self.wrap_button = tool_button(self, QStyle.StandardPixmap.SP_FileDialogListView, "Wrap output")
        self.wrap_button.setCheckable(True)
        copy_button = tool_button(self, QStyle.StandardPixmap.SP_FileIcon, "Copy all output")
        export_button = tool_button(self, QStyle.StandardPixmap.SP_DialogSaveButton, "Save output")
        clear_button = tool_button(self, QStyle.StandardPixmap.SP_TrashIcon, "Clear output")
        for button in [self.wrap_button, copy_button, export_button, clear_button]:
            tools.addWidget(button)
        self.transfer_button = tool_button(self, QStyle.StandardPixmap.SP_DirIcon, "File transfers")
        transfer_menu = QMenu(self.transfer_button)
        transfer_menu.addAction("Upload file", lambda: self.window.transfer_file(self.host, "upload"))
        transfer_menu.addAction("Download file", lambda: self.window.transfer_file(self.host, "download"))
        self.transfer_button.setMenu(transfer_menu)
        self.transfer_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.cancel_transfer = tool_button(self, QStyle.StandardPixmap.SP_DialogCancelButton, "Cancel transfer or Python command")
        self.cancel_transfer.clicked.connect(lambda: self.window.cancel_host_operation(self.host.id))
        tools.addWidget(self.transfer_button)
        tools.addWidget(self.cancel_transfer)
        console.addWidget(terminal_header)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        progress_policy = self.progress.sizePolicy()
        progress_policy.setRetainSizeWhenHidden(True)
        self.progress.setSizePolicy(progress_policy)
        console.addWidget(self.progress)
        self.terminal = QTextEdit()
        self.terminal.setObjectName("terminal")
        self.terminal.setReadOnly(True)
        self.terminal.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.terminal.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self.terminal.setAccessibleName(f"Terminal output for {self.host.name}")
        append_terminal(self.terminal, "Not connected", "muted")
        self.output_stack = QStackedWidget()
        terminal_page = QWidget()
        terminal_layout = QVBoxLayout(terminal_page)
        terminal_layout.setContentsMargins(0, 0, 0, 0)
        terminal_layout.setSpacing(0)
        self.output_find = FindBar(self.terminal)
        tools.insertWidget(0, self.output_find, 1)
        terminal_layout.addWidget(self.terminal)
        self.history = HistoryWidget(self.window.repository, self.host.id)
        self.output_stack.addWidget(terminal_page)
        self.output_stack.addWidget(self.history)
        from remoteops.ui.inspection import ServicePanel, EventPanel
        self.services = ServicePanel(self.run_inspection)
        self.events = EventPanel(self.run_inspection)
        for panel in (self.services, self.events):
            self.output_stack.addWidget(panel)
        from remoteops.ui.tools_panel import ToolsPanel
        self.tools_panel = ToolsPanel(self.run_tool,
                                     lambda direction, path: self.window.transfer_file(self.host, direction, path),
                                     lambda: self.window.cancel_host_operation(self.host.id))
        self.output_stack.addWidget(self.tools_panel)
        from remoteops.ui.explorer import ExplorerPanel
        self.explorer = ExplorerPanel(self.run_tool, self.host.wmi_namespace)
        self.output_stack.addWidget(self.explorer)
        self.view_tabs.currentChanged.connect(self.output_stack.setCurrentIndex)
        self.view_tabs.currentChanged.connect(lambda index: self.history.reload() if index == 1 else None)
        console.addWidget(self.output_stack, 1)

        command_bar = QWidget()
        command_bar.setObjectName("commandBar")
        command_row = QHBoxLayout(command_bar)
        command_row.setContentsMargins(16, 6, 10, 6)
        prompt = QLabel(">")
        prompt.setObjectName("prompt")
        self.command_input = CommandInput()
        self.command_input.template_provider = self.completion_templates
        self.command_input.setObjectName("commandInput")
        self.command_input.setPlaceholderText("Command")
        self.command_input.setAccessibleName("Command")
        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("primary")
        set_button_icon(self.run_button, QStyle.StandardPixmap.SP_MediaPlay)
        command_row.addWidget(prompt)
        command_row.addWidget(self.command_input, 1)
        command_row.addWidget(self.run_button)
        console.addWidget(command_bar)
        self.view_tabs.currentChanged.connect(lambda index: terminal_tools.setVisible(index == 0))
        self.view_tabs.currentChanged.connect(lambda index: command_bar.setVisible(index == 0))
        layout.addWidget(surface, 1)
        footer = QWidget()
        footer.setObjectName("sessionFooter")
        footer_row = QHBoxLayout(footer)
        footer_row.setContentsMargins(0, 0, 0, 0)
        self.activity_label = QLabel("Ready")
        footer_row.addWidget(self.activity_label)
        footer_row.addStretch()
        self.group_label = ElidedLabel()
        self.group_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.group_label.setMaximumWidth(200)
        self.group_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        footer_row.addWidget(self.group_label)
        layout.addWidget(footer)

        self.wrap_button.toggled.connect(self.toggle_wrap)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.terminal.toPlainText()))
        export_button.clicked.connect(self.export_output)
        clear_button.clicked.connect(self.terminal.clear)
        self.connect_button.clicked.connect(lambda: self.window.connect_host(self.host, self.terminal))
        self.disconnect_button.clicked.connect(lambda: self.window.disconnect_host(self.host, self.terminal))
        self.edit_button.clicked.connect(self.window.edit_current_host)
        self.run_button.clicked.connect(self.run_command)
        self.command_input.returnPressed.connect(self.run_command)

    def run_inspection(self, alias, values, callback):
        if self.busy:
            callback(WorkerOutcome(error="Host is busy"))
            return
        try:
            registry = self.window.command_registry
            registry.load()
            template = registry.aliases().get(alias)
            builtin = TEMPLATES_BY_ALIAS[alias]
            if template is None or template.script != builtin.script or template.wmi is not None:
                raise ValueError(f"Saved '{alias}' differs from the built-in definition. Use the Command Library to inspect it.")
            line = command_line_from_values(template, values)
            command = prepare_command(line, [template])
        except (ValueError, OSError) as exc:
            callback(WorkerOutcome(error=str(exc)))
            return
        if not self.window.run_host_command(self.host, line, self.terminal, on_result=callback, prepared_command=command):
            callback(WorkerOutcome(error="Command was not dispatched or was cancelled."))

    def completion_templates(self):
        registry = self.window.command_registry
        registry.load()
        backend = "WMI/DCOM" if self.host.connection_mode is ConnectionMode.WMI else "WinRM"
        return [template for template in registry.templates if backend in command_backends(template)]

    def run_tool(self, action, args, callback):
        if self.busy or self.window.state_for_host(self.host) is not SessionState.CONNECTED:
            callback(WorkerOutcome(error="Connect this host and wait for its current operation to finish."))
            return
        cancel = Event()
        args = dict(args)
        background = args.pop("_background", False) is True and action == "processes"
        self.window.plugin_cancels[self.host.id] = cancel
        self.window.set_host_state(self.host.id, SessionState.RUNNING)
        self.set_busy(True)
        self.window.start_worker("tool", self.host.id, self.terminal,
                                 lambda: self.window.session_manager.run_tool(self.host, action, args, cancel,
                                                                             record_history=not background),
                                 on_result=callback)

    def reload_profiles(self) -> None:
        self.auth_combo.blockSignals(True)
        self.auth_combo.clear()
        self.auth_combo.addItem("No credential", None)
        for profile in self.window.profiles:
            self.auth_combo.addItem(profile.name, profile.id)
        index = self.auth_combo.findData(self.host.auth_profile_id)
        self.auth_combo.setCurrentIndex(max(index, 0))
        self.auth_combo.blockSignals(False)

    def change_profile(self) -> None:
        if self.busy:
            return
        self.host.auth_profile_id = self.auth_combo.currentData()
        self.window.repository.save_host(self.host)
        self.window.clear_host_state(self.host.id)

    def toggle_wrap(self, enabled: bool) -> None:
        mode = QTextEdit.LineWrapMode.WidgetWidth if enabled else QTextEdit.LineWrapMode.NoWrap
        self.terminal.setLineWrapMode(mode)

    def export_output(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save output", "session.txt", "Text files (*.txt)")
        if path:
            try:
                Path(path).write_text(self.terminal.toPlainText(), encoding="utf-8")
            except OSError as exc:
                inline_notice(self, "Save failed", str(exc))

    def refresh(self) -> None:
        state = self.window.state_for_host(self.host)
        identity = (self.host.endpoint, self.host.auth_profile_id)
        if identity != self.inspection_identity:
            self.services.invalidate()
            self.events.invalidate()
            self.tools_panel.invalidate()
            self.explorer.invalidate()
            self.inspection_identity = identity
        self.services.set_connected(state is SessionState.CONNECTED)
        if state in (SessionState.DISCONNECTED, SessionState.ERROR):
            self.services.fresh = False
        self.status_label.setPixmap(status_icon(state).pixmap(14, 14))
        self.status_label.setToolTip(state.value)
        self.status_text.setText(state.value.capitalize())
        self.endpoint_label.setText(self.host.endpoint)
        self.endpoint_label.setToolTip(self.host.endpoint)
        self.transport_label.setText("WMI" if self.host.connection_mode is ConnectionMode.WMI else "PSRP")
        self.group_label.setText(self.host.group_name or "Ungrouped")
        self.auth_combo.setToolTip(self.window.profile_name(self.host.auth_profile_id))
        busy_states = {
            SessionState.CONNECTING,
            SessionState.DISCONNECTING,
            SessionState.RUNNING,
        }
        busy = self.busy or state in busy_states
        transferring = self.host.id in self.window.transfer_cancels
        if not transferring:
            self.progress.setRange(0, 0)
        self.progress.setVisible(busy)
        self.connect_button.setToolTip("Connecting..." if state is SessionState.CONNECTING else "Connect")
        self.disconnect_button.setToolTip("Disconnecting..." if state is SessionState.DISCONNECTING else "Disconnect")
        self.connect_button.setEnabled(not busy and state is not SessionState.CONNECTED)
        self.disconnect_button.setEnabled(
            not busy and state is not SessionState.DISCONNECTED
        )
        self.run_button.setEnabled(not busy)
        self.command_input.setEnabled(not busy)
        self.services.set_busy(busy)
        self.events.set_busy(busy)
        self.tools_panel.set_busy(busy)
        self.tools_panel.set_connection(state in (SessionState.CONNECTED, SessionState.RUNNING),
                                        self.host.connection_mode is ConnectionMode.WMI)
        self.explorer.set_busy(busy)
        profile = next((profile for profile in self.window.profiles if profile.id == self.host.auth_profile_id), None)
        metadata_supported = self.host.connection_mode is ConnectionMode.WINRM or (
            sys.platform == "win32" and not portable_kerberos(profile)
            and (profile is None or profile.method is not AuthMethod.NTLM_HASH))
        self.explorer.set_connection(state in (SessionState.CONNECTED, SessionState.RUNNING), metadata_supported)
        editable = not busy and state in {SessionState.DISCONNECTED, SessionState.ERROR}
        self.edit_button.setEnabled(editable)
        self.auth_combo.setEnabled(editable)
        self.transfer_button.setEnabled(not busy and state is SessionState.CONNECTED and self.host.connection_mode is ConnectionMode.WINRM)
        self.transfer_button.setToolTip("File transfers" if self.host.connection_mode is ConnectionMode.WINRM else "WMI does not support file transfers")
        self.cancel_transfer.setEnabled(transferring or self.host.id in self.window.plugin_cancels)

    def run_command(self) -> None:
        command_line = self.command_input.text().strip()
        if not command_line or self.busy:
            return
        self.command_input.remember(command_line)
        self.command_input.clear()
        self.view_tabs.setCurrentIndex(0)
        self.window.run_host_command(self.host, command_line, self.terminal)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.activity_label.setText("Working (0 s)")
        self.refresh()


class ElidedLabel(QLabel):
    def __init__(self, text: str = "") -> None:
        super().__init__()
        self.full_text = text
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:
        self.full_text = text
        self.setToolTip(text)
        super().setText(self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, self.width()))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setText(self.full_text)


class AuthProfilesDialog(QDialog):
    def __init__(self, profiles: list[CredentialProfile], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Credentials")
        self.resize(900, 480)
        self.profiles = profiles
        layout = QVBoxLayout(self)
        table = self.table = QTableWidget(0, 4)
        table.setHorizontalHeaderLabels(["Name", "Username", "Method", "Secret"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.verticalHeader().hide()
        table.verticalHeader().setDefaultSectionSize(38)
        layout.addWidget(table)
        if isinstance(parent, MainWindow):
            actions = QHBoxLayout()
            add_button = QPushButton("Add credential")
            add_button.setObjectName("primary")
            self.edit_button = QPushButton("Edit")
            self.delete_button = QPushButton("Delete")
            set_button_icon(add_button, "fa5s.plus")
            set_button_icon(self.edit_button, "fa5s.edit")
            set_button_icon(self.delete_button, "fa5s.trash-alt")
            for button in (add_button, self.edit_button, self.delete_button):
                actions.addWidget(button)
            actions.addStretch()
            layout.addLayout(actions)
            add_button.clicked.connect(lambda: self.change(parent.add_profile))
            self.edit_button.clicked.connect(lambda: self.selected_action(parent.edit_profile))
            self.delete_button.clicked.connect(lambda: self.selected_action(parent.delete_profile))
            table.itemDoubleClicked.connect(lambda _item: self.selected_action(parent.edit_profile))
            table.itemSelectionChanged.connect(self.update_actions)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reload()

    def reload(self):
        parent = self.parentWidget()
        self.profiles = parent.repository.list_credential_profiles() if isinstance(parent, MainWindow) else self.profiles
        selected = self.table.currentRow()
        self.table.setRowCount(len(self.profiles))
        for row, profile in enumerate(self.profiles):
            stored = isinstance(parent, MainWindow) and parent.repository.get_encrypted_secret(profile.id) is not None
            secret_mode = "Saved in database" if stored else ("Prompt when needed" if profile.prompt_for_secret else "Legacy keychain / not saved")
            for col, value in enumerate([profile.name, profile.username, profile.method.value.upper(), secret_mode]):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.table.setItem(row, col, item)
        if self.profiles:
            self.table.selectRow(max(0, min(selected, len(self.profiles) - 1)))
        self.update_actions()

    def update_actions(self):
        if hasattr(self, "edit_button"):
            valid = 0 <= self.table.currentRow() < len(self.profiles)
            self.edit_button.setEnabled(valid)
            self.delete_button.setEnabled(valid)

    def change(self, action):
        action()
        self.reload()

    def selected_action(self, action):
        row = self.table.currentRow()
        if 0 <= row < len(self.profiles):
            self.change(lambda: action(self.profiles[row]))


class CommandLibraryDialog(QDialog):
    def __init__(self, registry: CommandRegistry, parent: QWidget | None = None, *, backend=None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.backend = backend
        self.chosen_alias = None
        self.setWindowTitle("Command Library")
        self.resize(1100, 560)
        layout = QVBoxLayout(self)
        command_path = QLabel(str(self.registry.path))
        command_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        command_path.setWordWrap(True)
        layout.addWidget(command_path)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search commands")
        self.search.setAccessibleName("Search commands")
        self.search.setClearButtonEnabled(True)
        self.category_filter = QComboBox()
        self.category_filter.setAccessibleName("Command category")
        self.backend_filter = QComboBox()
        self.backend_filter.setAccessibleName("Command backend")
        self.backend_filter.addItems(["All backends", "WMI/DCOM", "WinRM"])
        if backend:
            self.backend_filter.setCurrentText(backend)
        self.favorites_only = QCheckBox("Favorites")
        filters.addWidget(self.search, 1)
        filters.addWidget(self.category_filter)
        filters.addWidget(self.backend_filter)
        filters.addWidget(self.favorites_only)
        layout.addLayout(filters)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Alias", "Command", "Backends", "Description", "Definition", "Category", "Favorite"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.itemDoubleClicked.connect(lambda _item: self.choose_selected() if self.backend else self.edit_selected())
        self.table.itemChanged.connect(self.toggle_favorite)
        layout.addWidget(self.table)

        actions = QHBoxLayout()
        self.insert_button = QPushButton("Insert command")
        self.insert_button.setVisible(backend is not None)
        self.insert_button.clicked.connect(self.choose_selected)
        self.table.itemSelectionChanged.connect(self.update_insert)
        actions.addWidget(self.insert_button)
        add_button = QPushButton("Add")
        edit_button = QPushButton("Edit")
        delete_button = QPushButton("Delete")
        duplicate_button = tool_button(self, QStyle.StandardPixmap.SP_FileDialogNewFolder, "Duplicate command")
        import_button = tool_button(self, QStyle.StandardPixmap.SP_DialogOpenButton, "Import commands")
        export_button = tool_button(self, QStyle.StandardPixmap.SP_DialogSaveButton, "Export selected command")
        plugins_button = tool_button(self, "fa5s.code", "Python commands")
        plugins_button.clicked.connect(self.manage_plugins)
        reload_button = QPushButton("Reload File")
        reset_button = QPushButton("Reset Defaults")
        close_button = QPushButton("Close")
        actions.addWidget(add_button)
        actions.addWidget(edit_button)
        actions.addWidget(delete_button)
        actions.addWidget(duplicate_button)
        actions.addWidget(import_button)
        actions.addWidget(export_button)
        actions.addWidget(plugins_button)
        actions.addStretch(1)
        actions.addWidget(reload_button)
        actions.addWidget(reset_button)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        add_button.clicked.connect(self.add_command)
        edit_button.clicked.connect(self.edit_selected)
        delete_button.clicked.connect(self.delete_selected)
        duplicate_button.clicked.connect(self.duplicate_selected)
        import_button.clicked.connect(self.import_commands)
        export_button.clicked.connect(self.export_selected)
        reload_button.clicked.connect(self.reload_file)
        reset_button.clicked.connect(self.reset_defaults)
        close_button.clicked.connect(self.accept)
        self.search.textChanged.connect(self.filter_table)
        self.category_filter.currentTextChanged.connect(self.filter_table)
        self.backend_filter.currentTextChanged.connect(self.filter_table)
        self.favorites_only.toggled.connect(self.filter_table)

        self.reload_table()

    def reload_table(self) -> None:
        selected = self.selected_template()
        category = self.category_filter.currentText()
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItems(["All categories"] + sorted({command_category(t) for t in self.registry.templates}))
        self.category_filter.setCurrentIndex(max(0, self.category_filter.findText(category)))
        self.category_filter.blockSignals(False)
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for template in sorted(self.registry.templates, key=lambda t: (not t.favorite, command_category(t).casefold(), t.alias)):
            row = self.table.rowCount()
            self.table.insertRow(row)
            try:
                resolved, _source = self.registry.resolve(template.alias)
            except ValueError:
                resolved = template.script
            if template.wmi is not None:
                resolved = "WMI: " + template.wmi["class"] + " / " + ", ".join(template.wmi["properties"])
            if template.plugin is not None:
                resolved = "Python: " + template.plugin.folder + " @ " + template.plugin.digest[:12]
            values = [template.alias, template.name, ", ".join(command_backends(template)), template.description, resolved, command_category(template), ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, template.id)
                item.setToolTip(value)
                if col == 6:
                    if template.plugin is None:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
                    item.setCheckState(Qt.CheckState.Checked if template.favorite else Qt.CheckState.Unchecked)
                    item.setToolTip("Favorite command")
                self.table.setItem(row, col, item)
            if selected and selected.id == template.id:
                self.table.selectRow(row)
        self.table.blockSignals(False)
        self.filter_table()

    def filter_table(self):
        query = self.search.text().strip().casefold()
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, col).text() for col in range(6)]
            visible = (query in " ".join(values).casefold()
                       and (self.category_filter.currentIndex() == 0 or values[5] == self.category_filter.currentText())
                       and (self.backend_filter.currentIndex() == 0 or self.backend_filter.currentText() in values[2])
                       and (not self.favorites_only.isChecked() or self.table.item(row, 6).checkState() == Qt.CheckState.Checked))
            self.table.setRowHidden(row, not visible)
        self.update_insert()

    def update_insert(self):
        template = self.selected_template()
        self.insert_button.setEnabled(template is not None and self.backend in command_backends(template))

    def choose_selected(self):
        template = self.selected_template()
        if template is not None and self.backend in command_backends(template):
            self.chosen_alias = template.alias
            self.accept()

    def toggle_favorite(self, item):
        if item.column() != 6:
            return
        template = next(t for t in self.registry.templates if t.id == item.data(Qt.ItemDataRole.UserRole))
        changed = deepcopy(template)
        changed.favorite = item.checkState() == Qt.CheckState.Checked
        try:
            self.registry.upsert(changed)
        except (ValueError, OSError) as exc:
            inline_notice(self, "Favorite not saved", str(exc))
        self.reload_table()

    def selected_template(self) -> CommandTemplate | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows or self.table.isRowHidden(rows[0].row()):
            return None
        item = self.table.item(rows[0].row(), 0)
        if item is None:
            return None
        template_id = item.data(Qt.ItemDataRole.UserRole)
        for template in self.registry.templates:
            if template.id == template_id:
                return template
        return None

    def add_command(self) -> None:
        dialog = CommandTemplateDialog(parent=self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.registry.upsert(dialog.template())
            except (ValueError, OSError) as exc:
                inline_notice(self, "Command not saved", str(exc))
            self.reload_table()

    def edit_selected(self) -> None:
        template = self.selected_template()
        if template is None:
            inline_notice(self, "Edit command", "No command selected.")
            return

        if template.plugin is not None:
            self.manage_plugins()
            return

        dialog = CommandTemplateDialog(template, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.registry.upsert(dialog.template())
            except (ValueError, OSError) as exc:
                inline_notice(self, "Command not saved", str(exc))
            self.reload_table()

    def delete_selected(self) -> None:
        template = self.selected_template()
        if template is None:
            inline_notice(self, "Delete command", "No command selected.")
            return
        try:
            self.registry.delete(template.id)
        except (ValueError, OSError) as exc:
            inline_notice(self, "Command not deleted", str(exc))
        self.reload_table()

    def duplicate_selected(self):
        template = self.selected_template()
        if template is None:
            return
        if template.plugin is not None:
            self.manage_plugins()
            return
        duplicate = deepcopy(template)
        duplicate.id = str(uuid4())
        duplicate.name += " copy"
        duplicate.alias += "-copy"
        while duplicate.alias.lower() in self.registry.aliases():
            duplicate.alias += "-copy"
        dialog = CommandTemplateDialog(duplicate, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.registry.upsert(dialog.template())
            except (ValueError, OSError) as exc:
                inline_notice(self, "Command not saved", str(exc))
            self.reload_table()

    def import_commands(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Import commands", "", "JSON (*.json)")
        if not filename:
            return
        try:
            self.registry.import_file(Path(filename))
        except (ValueError, OSError) as exc:
            inline_notice(self, "Commands not imported", str(exc))
        self.reload_table()

    def export_selected(self):
        template = self.selected_template()
        if template is None:
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export command", template.alias + ".json", "JSON (*.json)")
        if filename:
            try:
                self.registry.export_file(Path(filename), [template])
            except (ValueError, OSError) as exc:
                inline_notice(self, "Command not exported", str(exc))

    def reload_file(self) -> None:
        try:
            self.registry.load()
        except (ValueError, OSError) as exc:
            inline_notice(self, "Invalid command file", str(exc))
            return
        self.reload_table()

    def manage_plugins(self):
        from remoteops.ui.plugin_dialog import PluginDialog
        window = self.parent()
        while window is not None and not isinstance(window, MainWindow):
            window = window.parent()
        dialog = PluginDialog(self.registry, self, window)
        selected = self.selected_template()
        if selected and selected.plugin is not None:
            dialog.select_folder(Path(selected.plugin.folder).name)
        dialog.exec()
        self.reload_table()

    def reset_defaults(self) -> None:
        if QMessageBox.question(self, "Reset command library", "Replace all saved commands with the built-in defaults?",
                                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        try:
            self.registry.reset_to_defaults()
        except (ValueError, OSError) as exc:
            inline_notice(self, "Library not reset", str(exc))
        self.reload_table()


class CommandTemplateDialog(QDialog):
    def __init__(
        self,
        template: CommandTemplate | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.existing_template = deepcopy(template)
        self.setWindowTitle("Edit Command" if template else "Add Command")
        self.resize(820, 760)
        layout = QFormLayout(self)

        self.name = QLineEdit(template.name if template else "")
        self.alias = QLineEdit(template.alias if template else "")
        self.description = QLineEdit(template.description if template else "")
        self.category = QComboBox()
        self.category.setEditable(True)
        self.category.addItems(sorted(CATEGORIES) + ["Custom"])
        self.category.setCurrentText(command_category(template) if template else "Custom")
        self.script = QPlainTextEdit(template.script if template else "")
        self.module_path = QLineEdit(template.module_path if template else "")
        self.module_path.setPlaceholderText("Optional local .ps1/.psm1 file; run in the persistent WinRM session")
        module_picker = QWidget()
        module_layout = QHBoxLayout(module_picker)
        module_layout.setContentsMargins(0, 0, 0, 0)
        module_layout.addWidget(self.module_path, 1)
        browse_module = QPushButton("Browse…")
        browse_module.clicked.connect(self.choose_module)
        module_layout.addWidget(browse_module)
        self.parameters = QPlainTextEdit(
            json.dumps(template.parameters, indent=2) if template and template.parameters else "{}"
        )
        self.script.setMinimumHeight(140)
        self.parameters.setMinimumHeight(150)

        layout.addRow("Name", self.name)
        layout.addRow("Alias", self.alias)
        layout.addRow("Description", self.description)
        layout.addRow("Category", self.category)
        self.implementation_tabs = QTabWidget()
        powershell_tab = QWidget()
        powershell_layout = QVBoxLayout(powershell_tab)
        powershell_layout.setContentsMargins(0, 0, 0, 0)
        powershell_layout.addWidget(QLabel("PowerShell script"))
        powershell_layout.addWidget(self.script, 1)
        powershell_layout.addWidget(QLabel("Optional local script to load first"))
        powershell_layout.addWidget(module_picker)
        powershell_tab.setToolTip("Loads the local script into the persistent WinRM session; nothing is written remotely. Blank editor: load only.")
        self.implementation_tabs.addTab(powershell_tab, "PowerShell")
        wmi_tab = QWidget()
        wmi_tab.setObjectName("wmiQueryEditor")
        wmi_layout = QFormLayout(wmi_tab)
        self.wmi_enabled = QCheckBox("Custom WMI query")
        self.wmi_enabled.setChecked(template is not None and template.wmi is not None)
        wmi_layout.addRow(self.wmi_enabled)
        self.wmi_form = QWidget()
        fields = QFormLayout(self.wmi_form)
        fields.setContentsMargins(0, 0, 0, 0)
        self.wmi_preset = QComboBox()
        self.wmi_preset.addItems(["Blank query", "BIOS inventory", "Service status"])
        definition = template.wmi if template and template.wmi is not None else {}
        self.wmi_class = QLineEdit(definition.get("class", ""))
        self.wmi_properties = QPlainTextEdit(json.dumps(definition.get("properties", {}), indent=2))
        self.wmi_properties.setFixedHeight(100)
        self.wmi_filters = QPlainTextEdit(json.dumps(definition.get("filters", []), indent=2))
        self.wmi_filters.setFixedHeight(70)
        self.wmi_columns = QLineEdit(", ".join(definition.get("columns", [])))
        self.wmi_rows = QSpinBox()
        self.wmi_rows.setRange(1, 10000)
        self.wmi_rows.setValue(definition.get("limits", {}).get("rows", 1000))
        self.wmi_timeout = QSpinBox()
        self.wmi_timeout.setRange(1, 60)
        self.wmi_timeout.setSuffix(" s")
        self.wmi_timeout.setValue(definition.get("limits", {}).get("timeout_seconds", 30))
        fields.addRow("Preset", self.wmi_preset)
        fields.addRow("Namespace", QLabel("root/cimv2"))
        fields.addRow("Class", self.wmi_class)
        fields.addRow("Properties JSON", self.wmi_properties)
        fields.addRow("Filters (JSON)", self.wmi_filters)
        fields.addRow("Column order", self.wmi_columns)
        limits = QHBoxLayout()
        limits.addWidget(QLabel("Rows"))
        limits.addWidget(self.wmi_rows)
        limits.addWidget(QLabel("Timeout"))
        limits.addWidget(self.wmi_timeout)
        fields.addRow("Limits", limits)
        wmi_layout.addRow(self.wmi_form)
        self.wmi_form.setEnabled(self.wmi_enabled.isChecked())
        self.wmi_enabled.toggled.connect(self.wmi_form.setEnabled)
        self.wmi_preset.activated.connect(self.apply_wmi_preset)
        self.wmi_scroll = QScrollArea()
        self.wmi_scroll.setWidgetResizable(True)
        self.wmi_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.wmi_scroll.setWidget(wmi_tab)
        self.implementation_tabs.addTab(self.wmi_scroll, "WMI")
        self.implementation_tabs.addTab(self.parameters, "Parameters (JSON)")
        if self.wmi_enabled.isChecked():
            self.implementation_tabs.setCurrentWidget(self.wmi_scroll)
        layout.addRow(self.implementation_tabs)
        preview_actions = QHBoxLayout()
        validate_button = QPushButton("Validate / Preview")
        set_button_icon(validate_button, QStyle.StandardPixmap.SP_DialogApplyButton)
        validate_button.clicked.connect(self.validate_preview)
        self.validation_status = QLabel()
        self.validation_status.setWordWrap(True)
        preview_actions.addWidget(validate_button)
        preview_actions.addWidget(self.validation_status, 1)
        layout.addRow(preview_actions)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setFixedHeight(70)
        layout.addRow("WQL preview (defaults)", self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)

    def accept(self) -> None:
        try:
            validate_template(self.template())
        except ValueError as exc:
            inline_notice(self, "Invalid command", str(exc))
            return
        super().accept()

    def template(self) -> CommandTemplate:
        if self.existing_template:
            template = self.existing_template
            template.name = self.name.text().strip()
            template.alias = self.alias.text().strip()
            template.description = self.description.text().strip()
            template.category = self.category.currentText().strip()
            template.parameters = self.parameters_value()
            template.script = self.script.toPlainText().strip()
            template.module_path = self.module_path.text().strip()
            template.wmi = self.wmi_value()
            return template

        return CommandTemplate(
            name=self.name.text().strip(),
            alias=self.alias.text().strip(),
            description=self.description.text().strip(),
            category=self.category.currentText().strip(),
            parameters=self.parameters_value(),
            script=self.script.toPlainText().strip(),
            module_path=self.module_path.text().strip(),
            wmi=self.wmi_value(),
        )

    def choose_module(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select PowerShell script", "", "PowerShell scripts (*.ps1 *.psm1);;All files (*)"
        )
        if path:
            self.module_path.setText(path)

    def wmi_value(self):
        if not self.wmi_enabled.isChecked():
            return None
        definition = {"operation": "query", "namespace": "root/cimv2", "class": self.wmi_class.text().strip(),
                      "properties": parse_json(self.wmi_properties.toPlainText()),
                      "filters": parse_json(self.wmi_filters.toPlainText()),
                      "limits": {"rows": self.wmi_rows.value(), "timeout_seconds": self.wmi_timeout.value()}}
        if self.wmi_columns.text().strip():
            definition["columns"] = [name.strip() for name in self.wmi_columns.text().split(",")]
        return definition

    def apply_wmi_preset(self, index):
        definitions = (
            ("", {}, [], {}),
            ("Win32_BIOS", {"Manufacturer": "string", "SMBIOSBIOSVersion": "string", "SerialNumber": "string"}, [], {}),
            ("Win32_Service", {"Name": "string", "DisplayName": "string", "State": "string", "StartMode": "string"},
             [{"property": "Name", "operator": "eq", "parameter": "service_name"}],
             {"service_name": "Spooler", "_positional": ["service_name"]}),
        )
        class_name, properties, filters, parameters = definitions[index]
        self.wmi_class.setText(class_name)
        self.wmi_properties.setPlainText(json.dumps(properties, indent=2))
        self.wmi_filters.setPlainText(json.dumps(filters, indent=2))
        self.wmi_columns.clear()
        self.parameters.setPlainText(json.dumps(parameters, indent=2))
        self.preview.clear()
        self.validation_status.clear()

    def validate_preview(self):
        self.preview.clear()
        try:
            template = self.template()
            validate_template(template)
            self.validation_status.setText("Definition valid")
            try:
                prepared = prepare_command(template.alias, [template])
            except ValueError as exc:
                self.validation_status.setText("Preview unavailable")
                self.preview.setPlainText(str(exc))
                return
            self.preview.setPlainText(prepared.wmi_plan.query.wql if prepared.wmi_plan else "")
        except ValueError as exc:
            self.validation_status.setText("Invalid definition")
            self.preview.setPlainText(str(exc))

    def parameters_value(self) -> dict[str, object]:
        text = self.parameters.toPlainText().strip()
        if not text:
            return {}
        value = parse_json(text)
        if not isinstance(value, dict):
            raise ValueError("Parameters must be a JSON object")
        return value


class HostDialog(QDialog):
    def __init__(
        self,
        profiles: list[CredentialProfile],
        host: Host | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.existing_host = host
        self.setWindowTitle("Edit Host" if host else "Add Host")
        layout = QFormLayout(self)

        self.name = QLineEdit(host.name if host else "")
        self.address = QLineEdit(host.address if host else "")
        self.kerberos_hostname = QLineEdit(host.kerberos_hostname if host else "")
        self.kerberos_hostname.setPlaceholderText("FQDN when connecting by IP")
        self.port = QLineEdit(str(host.port if host else 5985))
        self.group_name = QLineEdit(host.group_name if host and host.group_name else "")
        if isinstance(parent, MainWindow):
            from PySide6.QtWidgets import QCompleter
            self.group_name.setCompleter(QCompleter(parent.repository.list_host_groups(), self))
        self.tags = QLineEdit(", ".join(host.tags) if host else "")
        self.transport = QComboBox()
        self.transport.addItems([Transport.HTTP.value, Transport.HTTPS.value])
        if host:
            self.transport.setCurrentText(host.transport.value)

        self.connection_mode = QComboBox()
        self.connection_mode.addItem("WinRM / PSRP", ConnectionMode.WINRM.value)
        self.connection_mode.addItem(
            "WMI / DCOM" if sys.platform == "win32" else "WMI / DCOM (experimental)",
            ConnectionMode.WMI.value,
        )
        self.connection_mode.setCurrentIndex(1 if host and host.connection_mode is ConnectionMode.WMI else 0)
        self.namespace = QLineEdit(host.wmi_namespace if host else "root/cimv2")
        self.keepalive = QSpinBox()
        self.keepalive.setRange(0, 3600)
        self.keepalive.setSingleStep(15)
        self.keepalive.setSuffix(" seconds")
        self.keepalive.setSpecialValueText("Disabled")
        self.keepalive.setValue(host.keepalive_seconds if host else 60)

        self.profile = QComboBox()
        self.profile.addItem("None", None)
        for profile in profiles:
            self.profile.addItem(profile.name, profile.id)
        if host and host.auth_profile_id:
            index = self.profile.findData(host.auth_profile_id)
            if index >= 0:
                self.profile.setCurrentIndex(index)

        layout.addRow("Name", self.name)
        layout.addRow("Address", self.address)
        layout.addRow("Kerberos hostname", self.kerberos_hostname)
        layout.addRow("Connection", self.connection_mode)
        layout.addRow("Port", self.port)
        layout.addRow("Transport", self.transport)
        layout.addRow("WMI namespace", self.namespace)
        layout.addRow("Idle health check", self.keepalive)
        layout.addRow("Auth profile", self.profile)
        layout.addRow("Group", self.group_name)
        layout.addRow("Tags", self.tags)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.connection_mode.currentIndexChanged.connect(self.update_mode)
        self.update_mode()

    def update_mode(self):
        wmi = ConnectionMode(self.connection_mode.currentData()) is ConnectionMode.WMI
        self.port.setEnabled(not wmi)
        self.transport.setEnabled(not wmi)
        self.namespace.setEnabled(wmi)

    def accept(self):
        try:
            if not self.name.text().strip() or not self.address.text().strip():
                raise ValueError("Name and address are required")
            if not 1 <= int(self.port.text()) <= 65535:
                raise ValueError("Port must be 1-65535")
            if not self.namespace.text().strip():
                raise ValueError("WMI namespace is required")
            if self.kerberos_hostname.text().strip():
                hostname(self.kerberos_hostname.text())
        except ValueError as exc:
            inline_notice(self, "Invalid host", str(exc))
            return
        super().accept()

    def host(self) -> Host:
        if self.existing_host:
            host = self.existing_host
            host.name = self.name.text().strip()
            host.address = self.address.text().strip()
            host.kerberos_hostname = self.kerberos_hostname.text().strip()
            host.port = int(self.port.text().strip() or "5985")
            host.transport = Transport(self.transport.currentText())
            host.auth_profile_id = self.profile.currentData()
            host.group_name = self.group_name.text().strip() or None
            host.tags = [tag.strip() for tag in self.tags.text().split(",") if tag.strip()]
            host.connection_mode = ConnectionMode(self.connection_mode.currentData())
            host.wmi_namespace = self.namespace.text().strip()
            host.keepalive_seconds = self.keepalive.value()
            return host

        return Host(
            name=self.name.text().strip(),
            address=self.address.text().strip(),
            kerberos_hostname=self.kerberos_hostname.text().strip(),
            port=int(self.port.text().strip() or "5985"),
            transport=Transport(self.transport.currentText()),
            auth_profile_id=self.profile.currentData(),
            group_name=self.group_name.text().strip() or None,
            tags=[tag.strip() for tag in self.tags.text().split(",") if tag.strip()],
            connection_mode=ConnectionMode(self.connection_mode.currentData()),
            wmi_namespace=self.namespace.text().strip(),
            keepalive_seconds=self.keepalive.value(),
        )


class CredentialDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, *, profile=None) -> None:
        super().__init__(parent)
        self.original = deepcopy(profile) if profile else CredentialProfile("", "", method=AuthMethod.NTLM)
        self.setWindowTitle("Edit Credential" if profile else "Add Credential")
        self.resize(540, 300)
        layout = QFormLayout(self)

        self.name = QLineEdit(self.original.name)
        self.username = QLineEdit(self.original.username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        if profile and not profile.prompt_for_secret:
            self.password.setPlaceholderText("Keep existing")
        self.save_password = QCheckBox("Save in database (encrypted)")
        self.save_password.setChecked(not profile or not profile.prompt_for_secret)
        self.password.setEnabled(self.save_password.isChecked())
        self.save_password.toggled.connect(self.password.setEnabled)
        self.method = QComboBox()
        for method, label in ((AuthMethod.KERBEROS, "Kerberos / Password"),
                              (AuthMethod.KERBEROS_RC4, "Kerberos / RC4 key"),
                              (AuthMethod.KERBEROS_AES, "Kerberos / AES key"),
                              (AuthMethod.NTLM, "NTLM / Password"),
                              (AuthMethod.NTLM_HASH, "NTLM / Password hash")):
            self.method.addItem(label, method)
        if self.original.method is AuthMethod.BASIC:
            self.method.addItem("Basic (existing profile)", AuthMethod.BASIC)
        self.method.setCurrentIndex(self.method.findData(self.original.method))

        layout.addRow("Name", self.name)
        layout.addRow("Username", self.username)
        layout.addRow("Method", self.method)
        self.secret_label = QLabel("Password")
        layout.addRow(self.secret_label, self.password)
        layout.addRow("", self.save_password)
        self.domain = QLineEdit(self.original.domain)
        self.domain.setPlaceholderText("example.com")
        self.domain_controller = QLineEdit(self.original.domain_controller)
        self.domain_controller.setPlaceholderText("192.0.2.1")
        self.dns_server = QLineEdit(self.original.dns_server)
        self.dns_server.setPlaceholderText("Use domain controller")
        layout.addRow("Domain", self.domain)
        layout.addRow("DC IP", self.domain_controller)
        layout.addRow("DNS server IP", self.dns_server)
        self.validation = QLabel()
        self.validation.setWordWrap(True)
        self.validation.setTextFormat(Qt.TextFormat.PlainText)
        layout.addRow(self.validation)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self.method.currentIndexChanged.connect(self.update_secret_type)
        self.update_secret_type()

    def update_secret_type(self):
        method = AuthMethod(self.method.currentData())
        self.secret_label.setText({AuthMethod.NTLM_HASH: "NT hash", AuthMethod.KERBEROS_RC4: "RC4 key",
                                   AuthMethod.KERBEROS_AES: "AES key"}.get(method, "Password"))
        sizes = "32 or 64 hexadecimal characters" if method is AuthMethod.KERBEROS_AES else "32 hexadecimal characters"
        self.password.setPlaceholderText(sizes if method in (AuthMethod.NTLM_HASH, AuthMethod.KERBEROS_RC4, AuthMethod.KERBEROS_AES)
                                         else "Keep existing")
        for field in (self.domain, self.domain_controller, self.dns_server):
            field.setEnabled(method in KERBEROS_METHODS)

    def profile(self) -> CredentialProfile:
        profile = deepcopy(self.original)
        profile.name = self.name.text().strip()
        profile.username = self.username.text().strip()
        profile.method = AuthMethod(self.method.currentData())
        profile.domain = self.domain.text().strip()
        profile.domain_controller = self.domain_controller.text().strip()
        profile.dns_server = self.dns_server.text().strip()
        profile.prompt_for_secret = not self.save_password.isChecked()
        return profile

    def accept(self):
        profile = self.profile()
        if not profile.name or not profile.username:
            self.validation.setText("Name and username required.")
            return
        try:
            if profile.method in KERBEROS_METHODS:
                DomainConnection(profile.domain, profile.domain_controller, profile.dns_server)
            if self.secret():
                validate_secret(profile.method, self.secret())
        except ValueError as exc:
            self.validation.setText(str(exc))
            return
        parent = self.parentWidget()
        if isinstance(parent, MainWindow):
            try:
                parent.session_manager.credential_store.save_profile(profile, self.secret() or None)
            except (ValueError, RuntimeError, OSError) as exc:
                self.validation.setText(str(exc))
                return
            except Exception:
                self.validation.setText("Database save failed.")
                return
        super().accept()

    def secret(self) -> str:
        if not self.save_password.isChecked():
            return ""
        return self.password.text()


def status_icon(state: SessionState) -> QIcon:
    colors = {
        SessionState.CONNECTED: QColor("#28a745"),
        SessionState.RUNNING: QColor("#e3a008"),
        SessionState.CONNECTING: QColor("#2680eb"),
        SessionState.DISCONNECTING: QColor("#6f42c1"),
        SessionState.ERROR: QColor("#dc3545"),
        SessionState.DISCONNECTED: QColor("#89929b"),
    }
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(colors[state])
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(2, 2, 10, 10)
    painter.end()
    return QIcon(pixmap)


TERMINAL_COLORS = {
    "prompt": "#6bd4b1",
    "muted": "#9aa4b2",
    "progress": "#f6c177",
    "success": "#85cba6",
    "error": "#ff7b72",
    "stderr": "#ff9b94",
    "stdout": "#dce2e5",
}


def append_terminal(terminal: QTextEdit, text: str, role: str = "stdout") -> None:
    color = TERMINAL_COLORS.get(role, TERMINAL_COLORS["stdout"])
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setFamilies(["Cascadia Mono", "Consolas", "DejaVu Sans Mono", "Courier New"])
    font.setPixelSize(15)
    character_format = QTextCharFormat()
    character_format.setFont(font)
    character_format.setForeground(QColor(color))
    block_format = QTextBlockFormat()
    block_format.setTopMargin(12 if role == "prompt" else 4)
    block_format.setBottomMargin(4)
    cursor = QTextCursor(terminal.document())
    cursor.movePosition(QTextCursor.MoveOperation.End)
    if not terminal.document().isEmpty():
        cursor.insertBlock()
    cursor.setBlockFormat(block_format)
    # Plain text insertion preserves PowerShell table spacing and literal error text.
    cursor.insertText(text, character_format)
    terminal.verticalScrollBar().setValue(terminal.verticalScrollBar().maximum())


def _format_exception(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).strip()
