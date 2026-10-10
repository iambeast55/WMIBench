from __future__ import annotations

import sys
from pathlib import Path

from remoteops.core.database import Repository
from remoteops.remoting.session_manager import SessionManager


def data_dir() -> Path:
    if sys.platform == "win32":
        root = Path.home() / "AppData" / "Local"
    else:
        root = Path.home() / ".local" / "share"
    return root / "RemoteOps"


def run() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is not installed. Run: python -m pip install -e .")
        return 1

    from remoteops.ui.main_window import MainWindow

    repository = Repository(data_dir() / "remoteops.sqlite3")
    session_manager = SessionManager(repository=repository)

    app = QApplication(sys.argv)
    app.setApplicationName("RemoteOps")
    app.setStyle("Fusion")
    window = MainWindow(repository=repository, session_manager=session_manager)
    window.resize(1200, 780)
    window.show()
    return app.exec()
