"""Validate task definitions in memory; never register, update or run a task."""
import sys
import unittest
from pathlib import Path

from remoteops.commands.plugins import read_plugin


@unittest.skipUnless(sys.platform == "win32", "Native Task Scheduler XML parser requires Windows")
class NativeTaskXmlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import win32com.client
        import pywintypes
        cls.com_error = pywintypes.com_error
        cls.service = win32com.client.Dispatch("Schedule.Service")
        cls.service.Connect()
        folder = Path(__file__).resolve().parents[1] / "examples/plugins/scheduled-task"
        namespace = {}
        exec(read_plugin(folder).source, namespace)
        cls.xml = staticmethod(namespace["task_xml"])

    def test_service_accounts_and_schedules_are_accepted_by_windows(self):
        for account in ("SYSTEM", "LOCAL SERVICE", "NETWORK SERVICE"):
            for schedule in ("demand", "once", "daily"):
                with self.subTest(account=account, schedule=schedule):
                    args = {"executable": r"C:\Windows\System32\calc.exe", "account": account,
                            "description": "The task calculator", "schedule": schedule,
                            "working_directory": r"C:\Windows\System32\Tasks"}
                    if schedule != "demand":
                        args["start"] = "2026-11-01T09:00:00-05:00"
                    definition = self.service.NewTask(0)
                    definition.XmlText = self.xml(args)
                    self.assertEqual(definition.Principal.LogonType, 5)
                    self.assertEqual(definition.Actions.Item(1).Path, args["executable"])
                    self.assertEqual(definition.Actions.Item(1).WorkingDirectory, args["working_directory"])

    def test_old_api_enum_in_xml_is_rejected(self):
        xml = self.xml({"account": "SYSTEM", "executable": r"C:\Windows\System32\calc.exe"})
        invalid = xml.replace("</UserId>", "</UserId><LogonType>ServiceAccount</LogonType>")
        definition = self.service.NewTask(0)
        with self.assertRaises(self.com_error):
            definition.XmlText = invalid


if __name__ == "__main__":
    unittest.main()
