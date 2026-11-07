import ntpath

from remoteops.ui.notices import inline_notice

from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit, QMessageBox, QToolButton, QStyle
from remoteops.ui.icons import set_button_icon


class TransferDialog(QDialog):
    def __init__(self, direction, parent=None):
        super().__init__(parent)
        self.direction = direction
        self.setWindowTitle("Upload file" if direction == "upload" else "Download file")
        self.resize(620, 180)
        form = QFormLayout(self)
        self.local, self.remote = QLineEdit(), QLineEdit()
        self.remote_directory = None
        self.generated_path = None
        self.local.textChanged.connect(self.suggest_remote_file)
        self.remote.setPlaceholderText("C:\\Temp\\report.txt")
        row = QHBoxLayout()
        row.addWidget(self.local)
        browse = QToolButton()
        set_button_icon(browse, QStyle.StandardPixmap.SP_DialogOpenButton)
        browse.setToolTip("Choose local file")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        self.overwrite = QCheckBox("Replace destination if it exists")
        form.addRow("Local file", row)
        form.addRow("Remote file", self.remote)
        form.addRow(self.overwrite)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def browse(self):
        method = QFileDialog.getOpenFileName if self.direction == "upload" else QFileDialog.getSaveFileName
        path, _ = method(self, "Local file")
        if path:
            self.local.setText(path)

    def set_remote_directory(self, path):
        self.remote_directory = path
        self.remote.setText(path)
        self.suggest_remote_file(self.local.text())

    def suggest_remote_file(self, path):
        if (self.direction == "upload" and self.remote_directory and path
                and self.remote.text() in (self.remote_directory, self.generated_path)):
            self.generated_path = ntpath.join(self.remote_directory, ntpath.basename(path))
            self.remote.setText(self.generated_path)

    def accept(self):
        if not self.local.text().strip() or not self.remote.text().strip():
            inline_notice(self, "Missing file", "Choose both local and remote file paths.")
            return
        super().accept()
