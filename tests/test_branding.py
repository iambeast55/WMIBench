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

    def test_distribution_and_legacy_entry_point_remain_compatible(self):
        root = Path(__file__).resolve().parents[1]
        config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(config["project"]["name"], "remoteops")
        scripts = config["project"]["scripts"]
        self.assertEqual(scripts["wmibench"], scripts["remoteops"])
        self.assertIn("src/wmibench", config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])

    def test_data_paths_and_credential_service_do_not_change(self):
        home = Path.home()
        with patch("remoteops.ui.app.sys.platform", "win32"):
            self.assertEqual(data_dir(), home / "AppData/Local/RemoteOps")
        with patch("remoteops.ui.app.sys.platform", "linux"):
            self.assertEqual(data_dir(), home / ".local/share/RemoteOps")
        self.assertEqual(SERVICE_NAME, "RemoteOps")

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
        repository.assert_called_once_with(folder / "remoteops.sqlite3")

    def test_linux_install_and_uninstall_share_compatible_locations(self):
        root = Path(__file__).resolve().parents[1] / "packaging/linux"
        install = (root / "install.sh").read_text(encoding="utf-8")
        uninstall = (root / "uninstall.sh").read_text(encoding="utf-8")
        for text in (install, uninstall):
            self.assertIn('${WMIBENCH_INSTALL_DIR:-${REMOTEOPS_INSTALL_DIR:-$HOME/.local/share/remoteops}}', text)
            self.assertIn('${WMIBENCH_BIN_DIR:-${REMOTEOPS_BIN_DIR:-$HOME/.local/bin}}', text)
            self.assertIn('"$BIN_DIR/wmibench"', text)
            self.assertIn('"$BIN_DIR/remoteops"', text)
            self.assertIn('"$APPLICATIONS_DIR/remoteops.desktop"', text)
        self.assertIn("Name=WMIBench", install)
        self.assertIn("Exec=$BIN_DIR/wmibench", install)
        self.assertIn("-m wmibench", install)
