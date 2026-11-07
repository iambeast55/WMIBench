"""Service and event views."""
import json
from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QSpinBox, QStyle, QVBoxLayout, QWidget,
)

from remoteops.core.models import utc_now
from remoteops.remoting.output import display_value
from remoteops.remoting.wmi import parse_wmi_date
from remoteops.ui.errors import error_summary
from remoteops.ui.results import RecordTable, icon_button
from remoteops.ui.icons import set_button_icon


def field(record, *names):
    folded = {key.casefold(): value for key, value in record.items()}
    return next((folded[name.casefold()] for name in names if name.casefold() in folded), None)


class InspectionPanel(QWidget):
    def __init__(self, dispatch):
        super().__init__()
        self.setObjectName("inspectionPanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.dispatch = dispatch
        self.records = []
        self.visible_records = []
        self.busy = False
        self.fresh = False
        self.content_layout = QVBoxLayout(self)
        self.controls = QHBoxLayout()
        self.content_layout.addLayout(self.controls)
        self.status = QLabel("Not loaded")
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.content_layout.addWidget(self.status)
        self.table = RecordTable()
        self.table.setMinimumHeight(180)
        self.table.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.details_dialog = QDialog(self)
        self.details_dialog.setWindowTitle("Record details")
        self.details_dialog.resize(820, 560)
        details_layout = QVBoxLayout(self.details_dialog)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumHeight(80)
        self.details.setStyleSheet("font-size: 15px;")
        details_layout.addWidget(self.details)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(self.details_dialog.hide)
        details_layout.addWidget(close)
        self.details_button = icon_button(self, "fa5s.expand-alt", "Open selected record details")
        self.details_button.setEnabled(False)
        self.controls.addWidget(self.details_button)
        self.details_button.clicked.connect(self.open_details)
        self.details.textChanged.connect(lambda: self.details_button.setEnabled(bool(self.details.toPlainText())))
        self.content_layout.addWidget(self.table, 1)
        self.table.table.selectionModel().selectionChanged.connect(self.show_details)
        self.table.table.doubleClicked.connect(self.open_details)

    def open_details(self, *_args):
        if self.details.toPlainText():
            self.details_dialog.show()
            self.details_dialog.raise_()

    def hideEvent(self, event):
        self.details_dialog.hide()
        super().hideEvent(event)

    def selected_record(self):
        indexes = self.table.table.selectionModel().selectedRows()
        if not indexes:
            return None
        row = self.table.proxy.mapToSource(indexes[0]).row()
        return self.visible_records[row] if 0 <= row < len(self.visible_records) else None

    def show_details(self, *_args):
        record = self.selected_record()
        self.details.setPlainText(json.dumps(record, indent=2, ensure_ascii=False, default=str) if record else "")

    def failed(self, outcome):
        result = outcome.value
        detail = outcome.error or (result.stderr if result else "No result returned")
        if outcome.error or result is None or not result.succeeded:
            self.fresh = False
            self.status.setText(error_summary(detail) + ("\nPrevious results retained." if self.records else ""))
            self.status.setToolTip(detail)
            self.details.setPlainText(detail)
            return True
        return False

    def invalidate(self):
        self.records = self.visible_records = []
        self.fresh = False
        self.table.set_records([])
        self.details.clear()
        self.details_dialog.hide()
        self.status.setText("Not loaded")
        self.status.setToolTip("")


class ServicePanel(InspectionPanel):
    def __init__(self, dispatch):
        super().__init__(dispatch)
        self.connected = False
        self.state_filter = QComboBox()
        self.state_filter.addItem("All states", "")
        self.state_filter.setAccessibleName("Service state filter")
        self.refresh_button = icon_button(self, QStyle.StandardPixmap.SP_BrowserReload, "Refresh services")
        self.inspect_button = QPushButton("Inspect selected")
        self.restart_button = QPushButton("Restart selected")
        set_button_icon(self.restart_button, QStyle.StandardPixmap.SP_BrowserReload)
        self.controls.addWidget(self.state_filter)
        self.controls.addStretch()
        self.controls.addWidget(self.inspect_button)
        self.controls.addWidget(self.restart_button)
        self.controls.addWidget(self.refresh_button)
        self.refresh_button.clicked.connect(self.refresh)
        self.inspect_button.clicked.connect(self.inspect)
        self.restart_button.clicked.connect(self.restart)
        self.state_filter.currentIndexChanged.connect(self.filter_records)
        self.table.table.selectionModel().selectionChanged.connect(self.update_actions)
        self.set_busy(False)

    def set_busy(self, busy):
        self.busy = busy
        self.refresh_button.setEnabled(not busy)
        self.update_actions()

    def set_connected(self, connected):
        self.connected = connected
        self.update_actions()

    def update_actions(self, *_args):
        record = self.selected_record()
        name = field(record, "Name") if record else None
        valid = isinstance(name, str) and bool(name) and self.fresh and not self.busy and self.connected
        self.inspect_button.setEnabled(valid)
        self.restart_button.setEnabled(valid)

    def refresh(self):
        self.fresh = False
        self.update_actions()
        self.status.setText("Loading services...")
        self.dispatch("services", {}, self.loaded)

    def loaded(self, outcome):
        if not self.failed(outcome):
            self.records = outcome.value.records
            self.fresh = True
            current = self.state_filter.currentData()
            self.state_filter.blockSignals(True)
            self.state_filter.clear()
            self.state_filter.addItem("All states", "")
            states = sorted({display_value(field(row, "State", "Status")) for row in self.records})
            for state in states:
                if state:
                    self.state_filter.addItem(state, state)
            self.state_filter.setCurrentIndex(max(0, self.state_filter.findData(current)))
            self.state_filter.blockSignals(False)
            self.filter_records()
            self.status.setToolTip("")
        self.update_actions()

    def filter_records(self):
        state = self.state_filter.currentData()
        self.visible_records = [row for row in self.records if not state or display_value(field(row, "State", "Status")) == state]
        self.details.clear()
        self.table.set_records([{"Name": field(row, "Name"), "Display name": field(row, "DisplayName"),
                                 "State": field(row, "State", "Status"), "Startup": field(row, "StartMode", "StartType")}
                                for row in self.visible_records])
        if self.fresh:
            self.status.setText(f"{len(self.visible_records)} of {len(self.records)} services")
        self.update_actions()

    def inspect(self):
        record = self.selected_record()
        if record and self.fresh and not self.busy and self.connected:
            self.dispatch("svc", {"service_name": field(record, "Name")}, self.inspected)

    def inspected(self, outcome):
        if not self.failed(outcome):
            self.details.setPlainText(json.dumps(outcome.value.records, indent=2, ensure_ascii=False, default=str))
            if self.isVisible():
                self.open_details()
        self.update_actions()

    def restart(self):
        record = self.selected_record()
        if record and self.fresh and not self.busy and self.connected:
            name = field(record, "Name")
            self.fresh = False
            self.update_actions()
            self.dispatch("restart-service", {"service_name": name}, self.restarted)

    def restarted(self, outcome):
        if self.failed(outcome):
            self.update_actions()
        else:
            self.refresh()


class EventPanel(InspectionPanel):
    def __init__(self, dispatch):
        super().__init__(dispatch)
        self.log = QComboBox()
        self.log.setEditable(True)
        self.log.addItems(["System", "Application", "Security"])
        self.log.setAccessibleName("Event log")
        self.count = QSpinBox()
        self.count.setRange(1, 10000)
        self.count.setValue(50)
        self.count.setAccessibleName("Event count")
        self.refresh_button = icon_button(self, QStyle.StandardPixmap.SP_BrowserReload, "Fetch recent events")
        self.controls.addWidget(QLabel("Log"))
        self.controls.addWidget(self.log, 1)
        self.controls.addWidget(QLabel("Count"))
        self.controls.addWidget(self.count)
        self.controls.addWidget(self.refresh_button)
        filters = QHBoxLayout()
        self.provider = QLineEdit()
        self.provider.setPlaceholderText("Loaded provider")
        self.event_id = QLineEdit()
        self.event_id.setMaxLength(10)
        self.event_id.setPlaceholderText("Loaded event ID")
        self.level = QComboBox()
        self.level.addItem("All loaded levels", "")
        self.age = QComboBox()
        for label, hours in (("Any loaded time", 0), ("Last hour", 1), ("Last 24 hours", 24), ("Last 7 days", 168)):
            self.age.addItem(label, hours)
        for widget in (self.provider, self.event_id, self.level, self.age):
            filters.addWidget(widget)
        self.content_layout.insertLayout(1, filters)
        self.refresh_button.clicked.connect(self.refresh)
        self.provider.textChanged.connect(self.filter_records)
        self.event_id.textChanged.connect(self.filter_records)
        self.level.currentIndexChanged.connect(self.filter_records)
        self.age.currentIndexChanged.connect(self.filter_records)
        self.loaded_log = ""

    def set_busy(self, busy):
        self.busy = busy
        for widget in (self.log, self.count, self.refresh_button):
            widget.setEnabled(not busy)

    def refresh(self):
        self.fresh = False
        self.requested_log = self.log.currentText()
        self.status.setText(f"Loading {self.requested_log} events...")
        self.dispatch("events", {"log": self.requested_log, "count": self.count.value()}, self.loaded)

    def loaded(self, outcome):
        if self.failed(outcome):
            return
        self.records = outcome.value.records
        self.loaded_log = self.requested_log
        self.fresh = True
        current = self.level.currentData()
        self.level.blockSignals(True)
        self.level.clear()
        self.level.addItem("All loaded levels", "")
        for level in sorted({display_value(field(row, "LevelDisplayName", "EventType")) for row in self.records}):
            if level:
                self.level.addItem(level, level)
        self.level.setCurrentIndex(max(0, self.level.findData(current)))
        self.level.blockSignals(False)
        self.status.setToolTip("")
        self.filter_records()

    @staticmethod
    def timestamp(row):
        value = field(row, "TimeCreated", "TimeGenerated")
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            try:
                parsed = parse_wmi_date(value)
            except ValueError:
                return None
        return parsed if parsed and parsed.tzinfo is not None else None

    def filter_records(self):
        provider = self.provider.text().casefold()
        event_id = self.event_id.text().strip()
        level = self.level.currentData()
        hours = self.age.currentData()
        cutoff = utc_now() - timedelta(hours=hours) if hours else None
        invalid = bool(event_id and (len(event_id) > 10 or not event_id.isascii() or not event_id.isdigit()))
        self.visible_records = []
        if not invalid:
            for row in self.records:
                if provider not in display_value(field(row, "ProviderName", "SourceName")).casefold():
                    continue
                if event_id and display_value(field(row, "Id", "EventCode")) != str(int(event_id)):
                    continue
                if level and display_value(field(row, "LevelDisplayName", "EventType")) != level:
                    continue
                timestamp = self.timestamp(row) if cutoff else None
                if cutoff and (timestamp is None or timestamp < cutoff):
                    continue
                self.visible_records.append(row)
        self.details.clear()
        self.table.set_records([{"Time": self.display_time(row),
                                 "ID": field(row, "Id", "EventCode"),
                                 "Provider": field(row, "ProviderName", "SourceName"),
                                 "Level": field(row, "LevelDisplayName", "EventType"),
                                 "Message": field(row, "Message")}
                                for row in self.visible_records])
        for column, width in enumerate((155, 60, 145, 85)):
            self.table.table.setColumnWidth(column, width)
        if invalid:
            self.status.setText("Event ID must be a nonnegative integer")
        elif self.fresh:
            self.status.setText(f"{self.loaded_log} / {len(self.visible_records)} of {len(self.records)} loaded events")

    @classmethod
    def display_time(cls, row):
        timestamp = cls.timestamp(row)
        return timestamp.astimezone().strftime("%b %d, %H:%M:%S") if timestamp else display_value(field(row, "TimeCreated", "TimeGenerated"))

    def show_details(self, *_args):
        record = self.selected_record()
        if record:
            message = display_value(field(record, "Message"))
            self.details.setPlainText(message + "\n\n" + json.dumps(record, indent=2, ensure_ascii=False, default=str))
        else:
            self.details.clear()
