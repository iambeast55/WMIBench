import unittest
from unittest.mock import patch

from remoteops.standalone import main, plugin_cli_arguments


class StandaloneTests(unittest.TestCase):
    def test_frozen_plugin_tests_relaunch_executable_not_python_module(self):
        with patch("sys.frozen", True, create=True):
            self.assertEqual(plugin_cli_arguments(["test", "folder", "--trust"]),
                             ["--plugin-cli", "test", "folder", "--trust"])

    def test_source_plugin_tests_use_module(self):
        with patch("sys.frozen", False, create=True):
            self.assertEqual(plugin_cli_arguments(["validate", "folder"]),
                             ["-m", "remoteops.commands.plugin_cli", "validate", "folder"])

    def test_cli_mode_does_not_open_app(self):
        with patch("remoteops.commands.plugin_cli.main", return_value=7) as cli, \
                patch("remoteops.ui.app.run") as run:
            self.assertEqual(main(["--plugin-cli", "validate", "folder"]), 7)
            cli.assert_called_once_with(["validate", "folder"])
            run.assert_not_called()

    def test_default_launches_app(self):
        with patch("remoteops.ui.app.run", return_value=0) as run:
            self.assertEqual(main([]), 0)
            run.assert_called_once()

    def test_bad_arguments_do_not_launch_app(self):
        with patch("remoteops.ui.app.run") as run, patch("sys.stderr"):
            self.assertEqual(main(["--typo"]), 2)
            run.assert_not_called()
