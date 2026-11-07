"""Command argument editor."""
from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
    QPlainTextEdit, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from remoteops.commands.templates import (
    argument_schema, command_backends, command_line_from_values,
    command_read_only_modes, prepare_command,
)


class ArgumentDialog(QDialog):
    def __init__(self, templates, initial="", parent=None, *, mode=None, read_only=False):
        super().__init__(parent)
        self.setWindowTitle("Command arguments")
        self.resize(620, 480)
        self.command_line = ""
        self.fields = {}
        self.templates = [deepcopy(t) for t in templates
                          if (not read_only or command_read_only_modes(t))
                          and (mode is None or (mode in command_read_only_modes(t) if read_only else
                               ("WMI/DCOM" if mode == "wmi_dcom" else "WinRM") in command_backends(t)))]
        self.templates.sort(key=lambda t: (not t.favorite, t.alias))
        layout = QVBoxLayout(self)
        self.command = QComboBox()
        self.command.setAccessibleName("Command template")
        for template in self.templates:
            self.command.addItem(f"{template.alias} / {template.name}", template.alias)
        layout.addWidget(self.command)
        self.notice = QLabel()
        self.notice.setWordWrap(True)
        self.notice.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.notice)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        layout.addWidget(self.scroll, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(85)
        self.preview.setAccessibleName("Command preview")
        layout.addWidget(self.preview)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.apply_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.apply_button.setText("Insert command")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        alias = initial.strip().split(maxsplit=1)[0].lower() if initial.strip() else ""
        index = self.command.findData(alias)
        self.command.setCurrentIndex(max(0, index) if self.templates else -1)
        values = None
        if initial.strip():
            try:
                if index < 0:
                    raise ValueError("Command unavailable in this context")
                prepared = prepare_command(initial, self.templates)
                if prepared.help_text:
                    raise ValueError("Help requests do not contain command argument values")
                values = prepared.values
            except ValueError as exc:
                self.notice.setText(f"Argument load failed: {exc}")
        self.command.currentIndexChanged.connect(self.change_command)
        self.build_fields(values)

    @property
    def template(self):
        index = self.command.currentIndex()
        return self.templates[index] if index >= 0 else None

    def change_command(self):
        self.notice.clear()
        self.build_fields()

    def build_fields(self, values=None):
        self.fields = {}
        self.optional_fields = {}
        content = QWidget()
        content.setObjectName("argumentFields")
        form = QFormLayout(content)
        self.scroll.setWidget(content)
        if self.template is None:
            self.validate()
            return
        schema = argument_schema(self.template)
        if not schema:
            form.addRow(QLabel("No arguments"))
        ordered = dict.fromkeys([*self.template.parameters.get("_positional", []), *schema])
        for name in ordered:
            spec = schema[name]
            value = (values or {}).get(name, spec.get("default"))
            kind = spec.get("type", "str")
            if "choices" in spec:
                field = QComboBox()
                for choice in spec["choices"]:
                    field.addItem(str(choice), choice)
                field.setCurrentIndex(field.findData(value))
                field.currentIndexChanged.connect(self.validate)
            elif kind == "bool":
                field = QCheckBox()
                field.setChecked(str(value).lower() == "true")
                field.toggled.connect(self.validate)
            elif (kind == "int" and value is not None
                  and isinstance(spec.get("min"), int) and isinstance(spec.get("max"), int)
                  and -(2**31) <= spec["min"] <= spec["max"] < 2**31):
                field = QSpinBox()
                field.setRange(spec["min"], spec["max"])
                field.setValue(int(value))
                field.valueChanged.connect(self.validate)
            else:
                field = QLineEdit("" if value is None else str(value))
                if kind in ("int", "float"):
                    field.setPlaceholderText("Integer" if kind == "int" else "Number")
                field.textChanged.connect(self.validate)
            label = spec.get("description", name.replace("_", " ").capitalize())
            label += " *" if spec.get("required") else ""
            field.setAccessibleName(label)
            field.setToolTip(f"{name}: {kind}" + (f" / {spec.get('min', '*')} to {spec.get('max', '*')}" if "min" in spec or "max" in spec else ""))
            self.fields[name] = field
            if (self.template.plugin is not None and self.template.plugin.manifest["api_version"] >= 3
                    and not spec.get("required") and "default" not in spec):
                include = QCheckBox(name.replace("_", " ").capitalize())
                include.setToolTip(f"Include {name}. {spec.get('description', '')}")
                include.setAccessibleName(f"Include {name}")
                include.setChecked(name in (values or {}))
                field.setEnabled(include.isChecked())
                include.toggled.connect(field.setEnabled)
                include.toggled.connect(self.validate)
                self.optional_fields[name] = include
                form.addRow(include, field)
            else:
                caption = QLabel(label)
                caption.setWordWrap(True)
                caption.setMaximumWidth(220)
                form.addRow(caption, field)
        self.validate()

    def values(self):
        values = {}
        schema = argument_schema(self.template)
        for name, field in self.fields.items():
            if name in self.optional_fields and not self.optional_fields[name].isChecked():
                continue
            if isinstance(field, QComboBox):
                if field.currentIndex() < 0:
                    raise ValueError(f"Choose a value for {name}")
                value = field.currentData()
            elif isinstance(field, QCheckBox):
                value = field.isChecked()
            elif isinstance(field, QSpinBox):
                value = field.value()
            else:
                value = field.text()
            if schema[name].get("required") and value == "":
                raise ValueError(f"{name} is required")
            values[name] = value
        return values

    def validate(self):
        try:
            if self.template is None:
                raise ValueError("No supported commands")
            line = command_line_from_values(self.template, self.values())
        except ValueError as exc:
            self.command_line = ""
            self.preview.clear()
            self.status.setText(str(exc))
            self.apply_button.setEnabled(False)
            return False
        self.command_line = line
        self.preview.setPlainText(line)
        self.status.setText("Valid")
        self.apply_button.setEnabled(True)
        return True

    def accept(self):
        if self.validate():
            super().accept()
