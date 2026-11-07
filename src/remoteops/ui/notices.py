"""Dismissible, nonmodal feedback for routine validation and operation errors."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMainWindow, QToolButton, QWidget


def inline_notice(parent, title, message):
    if isinstance(parent, QMainWindow):
        parent.statusBar().showMessage(f"{title}: {message}")
        parent.statusBar().setToolTip(str(message))
        return
    notice = getattr(parent, "_inline_notice", None)
    if notice is None:
        notice = QWidget(parent)
        row = QHBoxLayout(notice)
        row.setContentsMargins(4, 4, 4, 4)
        notice.label = QLabel()
        notice.label.setTextFormat(Qt.TextFormat.PlainText)
        notice.label.setWordWrap(True)
        notice.label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        close = QToolButton()
        from remoteops.ui.icons import set_button_icon
        set_button_icon(close, "fa5s.times")
        close.setToolTip("Dismiss message")
        close.clicked.connect(notice.hide)
        row.addWidget(notice.label, 1)
        row.addWidget(close)
        layout = parent.layout()
        if hasattr(layout, "insertWidget"):
            layout.insertWidget(0, notice)
        else:
            layout.addRow(notice)
        parent._inline_notice = notice
    notice.label.setText(f"{title}: {message}")
    notice.show()
