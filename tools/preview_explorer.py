"""Render explorer and completion fixtures without remote connections."""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CommandResult, ConnectionMode, Host, SessionState
from remoteops.remoting.session import FakeSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.main_window import CredentialDialog, MainWindow, WorkerOutcome


def main():
    app = QApplication.instance() or QApplication([])
    if os.name == "nt":
        for filename in ("segoeui.ttf", "segoeuib.ttf", "consola.ttf"):
            QFontDatabase.addApplicationFont(f"C:/Windows/Fonts/{filename}")
        app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    output = Path("dist/explorer-qa")
    output.mkdir(parents=True, exist_ok=True)
    schema = [
        {"Kind": "Class", "Name": "Win32_Process", "Qualifiers": {"Description": "Process inventory"}},
        *[{"Kind": "Property", "Name": name, "Type": kind, "Key": name == "Handle", "Qualifiers": {"Read": True}}
          for name, kind in (("Caption", "string"), ("CommandLine", "string"), ("CreationDate", "datetime"),
                             ("ExecutablePath", "string"), ("Handle", "string"), ("Name", "string"),
                             ("ParentProcessId", "uint32"), ("ProcessId", "uint32"), ("SessionId", "uint32"))],
        {"Kind": "Method", "Name": "Terminate", "Static": False, "Qualifiers": {}},
        {"Kind": "Parameter", "Method": "Terminate", "Name": "Reason", "Type": "uint32", "Direction": "In"},
        {"Kind": "Parameter", "Method": "Terminate", "Name": "ReturnValue", "Type": "uint32", "Direction": "Out"},
    ]
    with tempfile.TemporaryDirectory() as directory:
        repository = Repository(Path(directory) / "preview.sqlite3")
        host = repository.save_host(Host(name="LAB-01", address="192.0.2.10", group_name="Lab", connection_mode=ConnectionMode.WMI))
        repository.save_host(Host(name="LAB-02", address="192.0.2.11", group_name="Lab"))
        repository.create_host_group("Production")
        manager = SessionManager(repository=repository, session_factory=FakeSession)
        window = MainWindow(repository, manager)
        manager.connect(host)
        window.refresh_statuses()
        tab = window.host_tabs_by_id[host.id]
        panel = tab.explorer

        def fixture(action, args, callback):
            rows = schema if action == "explorer-describe" else [{"Name": name} for name in (
                "Win32_OperatingSystem", "Win32_Process", "Win32_Service", "Win32_LogicalDisk")]
            callback(WorkerOutcome(value=CommandResult(host.id, host.name, "fixture", records=rows, exit_code=0)))

        panel.dispatch = fixture
        window.show()
        tab.view_tabs.setCurrentIndex(5)
        app.processEvents()
        panel.describe("Win32_Process")
        for dark in (False, True):
            window.set_dark_mode(dark)
            for width, height in ((1200, 780), (820, 560)):
                window.resize(width, height)
                for page, label in ((0, "properties"), (1, "methods"), (3, "python")):
                    panel.pages.setCurrentIndex(page)
                    app.processEvents()
                    assert window.width() == width, (window.width(), width)
                    for widget in (panel.namespace, panel.query_button, panel.copy_method, panel.status):
                        assert window.rect().contains(widget.mapTo(window, widget.rect().bottomRight())), widget.objectName()
                    window.grab().save(str(output / f"{'dark' if dark else 'light'}-{width}-{label}.png"))
        tab.view_tabs.setCurrentIndex(0)
        tab.command_input.setFocus()
        tab.command_input.setText("events --co")
        tab.command_input.show_completions()
        app.processEvents()
        window.grab().save(str(output / "completion-window.png"))
        tab.command_input.completion.popup().grab().save(str(output / "completion-popup.png"))
        tab.command_input.completion.popup().hide()
        dialog = CredentialDialog(window)
        dialog.method.setCurrentIndex(dialog.method.findData(AuthMethod.KERBEROS_AES))
        dialog.name.setText("Domain admin")
        dialog.username.setText("user@example.com")
        dialog.domain.setText("example.com")
        dialog.domain_controller.setText("192.0.2.1")
        for dark in (False, True):
            window.set_dark_mode(dark)
            dialog.show()
            app.processEvents()
            dialog.grab().save(str(output / f"{'dark' if dark else 'light'}-credentials.png"))
        dialog.close()
        dialog.deleteLater()
        window.close()
        window.thread_pool.waitForDone(5000)
        app.processEvents()
        window.deleteLater()
        app.processEvents()
    print(output.resolve())


if __name__ == "__main__":
    main()
