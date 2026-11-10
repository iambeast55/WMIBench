import runpy
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from remoteops.core.credentials import SERVICE_NAME
from remoteops.ui.app import data_dir, run


class BrandingTests(unittest.TestCase):
    def test_new_module_entry_preserves_exit_code(self):
        with patch("remoteops.__main__.main", return_value=7) as main:
            with self.assertRaises(SystemExit) as raised:
                runpy.run_module("wmibench", run_name="__main__")
        self.assertEqual(raised.exception.code, 7)
        main.assert_called_once_with()

    def test_distribution_uses_wmibench_identity(self):
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["name"], "wmibench")
        scripts = config["project"]["scripts"]
        self.assertIn("wmibench", scripts)
        self.assertNotIn("remoteops", scripts)
        self.assertIn("src/wmibench", config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])

    def test_data_paths_and_credential_service_use_wmibench(self):
        home = Path.home()
        with patch("remoteops.ui.app.sys.platform", "win32"), patch("remoteops.ui.app.Path.exists", return_value=False):
            self.assertEqual(data_dir(), home / "AppData/Local/WMIBench")
        with patch("remoteops.ui.app.sys.platform", "linux"), patch("remoteops.ui.app.Path.exists", return_value=False):
            self.assertEqual(data_dir(), home / ".local/share/WMIBench")
        self.assertEqual(SERVICE_NAME, "WMIBench")

    def test_qt_application_name_changes_without_changing_database(self):
        app = Mock()
        app.exec.return_value = 0
        folder = Path("fixture-data")
        with patch("PySide6.QtWidgets.QApplication", return_value=app), \
                patch("remoteops.ui.main_window.MainWindow"), \
                patch("remoteops.ui.app.data_dir", return_value=folder), \
                patch("remoteops.ui.app.Repository") as repository, \
                patch("remoteops.ui.app.SessionManager"):
            self.assertEqual(run(), 0)
        app.setApplicationName.assert_called_once_with("WMIBench")
        repository.assert_called_once_with(folder / "wmibench.sqlite3")

    def test_repository_launchers_use_local_state(self):
        root = Path(__file__).resolve().parents[1]
        install = (root / "install.sh").read_text(encoding="utf-8")
        run = (root / "run.sh").read_text(encoding="utf-8")
        self.assertIn('"$ROOT/.venv/bin/python" -m pip install --editable "$ROOT"', install)
        self.assertIn('export WMIBENCH_DATA_DIR="$ROOT"', run)
        self.assertIn('exec "$PYTHON" -m wmibench "$@"', run)
