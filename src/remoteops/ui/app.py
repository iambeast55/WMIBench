from __future__ import annotations

import os
import sys
from pathlib import Path

from remoteops.core.database import Repository
from remoteops.remoting.session_manager import SessionManager


def data_dir() -> Path:
    configured = os.environ.get("WMIBENCH_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser()

    # A portable bundle carries this marker beside the executable. This keeps
    # its command library and plugins beside the application without changing
    # the normal per-user data location for development and regular installs.
    if getattr(sys, "frozen", False):
        bundle_dir = Path(sys.executable).resolve().parent
        if (bundle_dir / "wmibench.portable").is_file():
            return bundle_dir

    # Use the WMIBench location for new installs. Existing RemoteOps data remains
    # usable until the user moves or migrates it.
    if sys.platform == "win32":
        root = Path.home() / "AppData" / "Local"
    else:
        root = Path.home() / ".local" / "share"
    current = root / "WMIBench"
    legacy = root / "RemoteOps"
    return legacy if not current.exists() and legacy.exists() else current


def run() -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 is not installed. Run: python -m pip install -e .")
        return 1

    from remoteops.ui.main_window import MainWindow

    folder = data_dir()
    database = folder / "wmibench.sqlite3"
    legacy_database = folder / "remoteops.sqlite3"
    if not database.exists() and legacy_database.exists():
        database = legacy_database
    repository = Repository(database)
    session_manager = SessionManager(repository=repository)

    app = QApplication(sys.argv)
    app.setApplicationName("WMIBench")
    app.setStyle("Fusion")
    window = MainWindow(repository=repository, session_manager=session_manager)
    window.resize(1200, 780)
    window.show()
    return app.exec()
