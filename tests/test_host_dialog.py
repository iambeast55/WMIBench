import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from remoteops.core.database import Repository
from remoteops.core.models import ConnectionMode, Host
from remoteops.ui.main_window import HostDialog


class HostDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repository = Repository(Path(self.temp.name) / "test.sqlite3")

    def dialog(self, host=None):
        dialog = HostDialog([], host)
        self.addCleanup(dialog.deleteLater)
        self.addCleanup(dialog.close)
        return dialog

    def test_new_hosts_save_in_both_modes(self):
        for mode in ConnectionMode:
            with self.subTest(mode=mode):
                dialog = self.dialog()
                dialog.name.setText("server-01")
                dialog.address.setText("192.0.2.10")
                dialog.connection_mode.setCurrentIndex(dialog.connection_mode.findData(mode.value))
                host = dialog.host()
                self.assertIs(host.connection_mode, mode)
                self.repository.save_host(host)
                loaded = self.repository.get_host(host.id)
                self.assertIs(loaded.connection_mode, mode)
                self.assertEqual(loaded.endpoint, host.endpoint)

    def test_edit_hosts_switches_and_persists_mode(self):
        host = self.repository.save_host(Host(name="server-01", address="192.0.2.10"))
        for mode in (ConnectionMode.WMI, ConnectionMode.WINRM):
            with self.subTest(mode=mode):
                dialog = self.dialog(host)
                dialog.connection_mode.setCurrentIndex(dialog.connection_mode.findData(mode.value))
                edited = dialog.host()
                self.assertEqual(edited.id, host.id)
                self.assertIs(edited.connection_mode, mode)
                self.repository.save_host(edited)
                host = self.repository.get_host(host.id)
                self.assertIs(host.connection_mode, mode)

    def test_mode_controls_on_load_and_change(self):
        for mode in ConnectionMode:
            with self.subTest(mode=mode):
                dialog = self.dialog(Host(name="server-01", address="192.0.2.10", connection_mode=mode))
                self.assertEqual(dialog.connection_mode.currentData(), mode.value)
                self.assertEqual(dialog.namespace.isEnabled(), mode is ConnectionMode.WMI)
                self.assertEqual(dialog.port.isEnabled(), mode is ConnectionMode.WINRM)
                self.assertEqual(dialog.transport.isEnabled(), mode is ConnectionMode.WINRM)
                for selected in (ConnectionMode.WMI, ConnectionMode.WINRM):
                    dialog.connection_mode.setCurrentIndex(dialog.connection_mode.findData(selected.value))
                    self.assertEqual(dialog.namespace.isEnabled(), selected is ConnectionMode.WMI)
                    self.assertEqual(dialog.port.isEnabled(), selected is ConnectionMode.WINRM)
                    self.assertEqual(dialog.transport.isEnabled(), selected is ConnectionMode.WINRM)
