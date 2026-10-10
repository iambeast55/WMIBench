"""Render the actual Qt interface with sample data and no network connections."""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFont, QFontDatabase

from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, CredentialProfile, Host, SessionState, utc_now
from remoteops.remoting.session import FakeSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.ui.main_window import MainWindow, append_terminal


def main():
    app = QApplication([])
    if sys.platform == "win32":
        # Qt's offscreen Windows plugin does not enumerate the system font directory.
        for filename in ["segoeui.ttf", "segoeuib.ttf", "consola.ttf", "cour.ttf"]:
            QFontDatabase.addApplicationFont(f"C:/Windows/Fonts/{filename}")
        app.setFont(QFont("Segoe UI", 10))
    app.setStyle("Fusion")
    output = Path(__file__).resolve().parents[1] / "dist" / "preview"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        repository = Repository(Path(temporary) / "preview.sqlite3")
        profile = repository.save_credential_profile(
            CredentialProfile(name="Infrastructure admins", username="EXAMPLE\\ops")
        )
        for name, address, group in [
            ("APP-SRV-01", "10.24.8.12", "Production"),
            ("DC-01", "10.24.8.2", "Infrastructure"),
            ("FILE-SRV-02", "10.24.8.24", "Production"),
            ("WEB-STAGING", "10.24.16.10", "Staging"),
        ]:
            repository.save_host(Host(name=name, address=address, group_name=group, auth_profile_id=profile.id))
        window = MainWindow(repository, SessionManager(repository=repository, session_factory=FakeSession))
        window.statusBar().showMessage("Design preview / Sample hosts and output")
        first = window.hosts[0]
        tab = window.host_tabs_by_id[first.id]
        window.set_host_state(first.id, SessionState.CONNECTED)
        window.set_host_state(window.hosts[2].id, SessionState.ERROR)
        tab.terminal.clear()
        for text, role in [
            ("remoteops> connect APP-SRV-01", "prompt"),
            ("[connected]", "success"),
            ("remoteops> sysinfo", "prompt"),
            ("[ok in 0.82s]", "success"),
            ("ComputerName    : APP-SRV-01\nOperatingSystem : Microsoft Windows Server 2022 Standard\nVersion         : 10.0.20348\nLastBootUpTime  : 09/01/2026 06:32:18\nTotalMemoryGB   : 32", "stdout"),
            ("remoteops> disk", "prompt"),
            ("[ok in 0.34s]", "success"),
            ("Drive  Label      SizeGB  FreeGB  FreePercent\n-----  -----      ------  ------  -----------\nC:     System        128    78.4         61.3\nD:     AppData       1024   612.8         59.8", "stdout"),
        ]:
            append_terminal(tab.terminal, text, role)
        tab.activity_label.setText("Completed / 0.34s")
        tab.command_input.setText("events System 20")
        result = CommandResult(first.id, first.name, "sample script", stdout="Disk sample output",
                               command_line="disk", records=[
                                   {"Drive": "C:", "Label": "System", "SizeGB": 128, "FreeGB": 78.4},
                                   {"Drive": "D:", "Label": "AppData", "SizeGB": 1024, "FreeGB": 612.8},
                               ], exit_code=0, completed_at=utc_now())
        repository.save_command_result(result)
        tab.result_table.set_records(result.records)
        tab.history.reload()
        window.show()
        for name, width, height in [("modern-ops-console", 1280, 820), ("modern-ops-compact", 820, 560)]:
            window.resize(width, height)
            app.processEvents()
            assert tab.run_button.geometry().right() < tab.run_button.parentWidget().width()
            assert tab.terminal.width() > 400
            assert window.grab().save(str(output / f"{name}.png"))
        window.set_host_state(first.id, SessionState.CONNECTING)
        tab.set_busy(True)
        window.resize(820, 560)
        app.processEvents()
        assert window.grab().save(str(output / "modern-ops-connecting.png"))
        window.clear_host_state(first.id)
        window.set_host_state(first.id, SessionState.CONNECTED)
        tab.set_busy(False)
        window.set_dark_mode(True)
        window.resize(1280, 820)
        for name, index in [("modern-ops-dark", 0), ("modern-ops-table", 1), ("modern-ops-history", 2)]:
            tab.view_tabs.setCurrentIndex(index)
            app.processEvents()
            assert window.grab().save(str(output / f"{name}.png"))
        window.close()
        window.thread_pool.waitForDone(5000)
        app.processEvents()
    print(f"Previews saved to {output}")


if __name__ == "__main__":
    main()
