from PySide6.QtCore import QEvent, QStringListModel, Qt, QTimer
from PySide6.QtWidgets import QCompleter, QLineEdit

from remoteops.commands.completion import complete


class CommandInput(QLineEdit):
    def __init__(self):
        super().__init__()
        self.history = []
        self.history_index = 0
        self.draft = ""
        self.template_provider = lambda: []
        self.suggestions = []
        self.snapshot = None
        self.completion_model = QStringListModel(self)
        self.completion = QCompleter(self.completion_model, self)
        self.completion.setWidget(self)
        self.completion.setCompletionMode(QCompleter.CompletionMode.UnfilteredPopupCompletion)
        self.completion.setMaxVisibleItems(9)
        self.completion.activated[str].connect(self.accept_completion)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(120)
        self.timer.timeout.connect(self.show_completions)
        self.textEdited.connect(lambda: self.timer.start())
        self.textChanged.connect(self.dismiss_completion)
        self.cursorPositionChanged.connect(self.dismiss_completion)

    def dismiss_completion(self, *_):
        self.completion.popup().hide()
        self.snapshot = None

    def remember(self, command):
        if not self.history or self.history[-1] != command:
            self.history.append(command)
        self.history_index = len(self.history)
        self.draft = ""

    def show_completions(self):
        if not self.hasFocus() or not self.isEnabled():
            return
        try:
            self.suggestions = complete(self.text(), self.cursorPosition(), self.template_provider())
        except (ValueError, OSError):
            self.suggestions = []
        if not self.suggestions:
            self.dismiss_completion()
            return
        self.snapshot = (self.text(), self.cursorPosition())
        self.completion_model.setStringList([item.text for item in self.suggestions])
        self.completion.setCompletionPrefix("")
        self.completion.complete()
        self.completion.popup().setCurrentIndex(self.completion.completionModel().index(0, 0))

    def accept_completion(self, value):
        if self.snapshot != (self.text(), self.cursorPosition()):
            return
        suggestion = next((item for item in self.suggestions if item.text == value), None)
        if suggestion is None:
            return
        self.timer.stop()
        text = self.text()
        suffix = text[suggestion.end:]
        insertion = suggestion.text + (" " if not suffix or not suffix[0].isspace() else "")
        self.setText(text[:suggestion.start] + insertion + suffix)
        self.setCursorPosition(suggestion.start + len(insertion))
        self.dismiss_completion()

    def event(self, event):
        if event.type() == QEvent.Type.KeyPress and event.key() == Qt.Key.Key_Tab:
            if not self.completion.popup().isVisible():
                self.show_completions()
            else:
                self.accept_completion(self.completion.popup().currentIndex().data())
            return True
        return super().event(event)

    def keyPressEvent(self, event):
        popup = self.completion.popup()
        if popup.isVisible():
            if event.key() in {Qt.Key.Key_Enter, Qt.Key.Key_Return}:
                self.accept_completion(popup.currentIndex().data())
                return
            if event.key() == Qt.Key.Key_Escape:
                self.timer.stop()
                self.dismiss_completion()
                return
        if event.key() == Qt.Key.Key_Space and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.show_completions()
            return
        if event.key() in {Qt.Key.Key_Up, Qt.Key.Key_Down} and self.history and not popup.isVisible():
            self.timer.stop()
            if self.history_index == len(self.history):
                self.draft = self.text()
            delta = -1 if event.key() == Qt.Key.Key_Up else 1
            self.history_index = max(0, min(len(self.history), self.history_index + delta))
            self.setText(self.history[self.history_index] if self.history_index < len(self.history) else self.draft)
            return
        super().keyPressEvent(event)
