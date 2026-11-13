import unittest
import xml.etree.ElementTree as ET

from implementation import run, task_xml, NAMESPACE
from remoteops.commands.plugin_api import FakeContext, PluginError


class CommandTests(unittest.TestCase):
    def test_list_is_query_only_with_correct_state_type(self):
        rows = [{"TaskPath": "\\", "TaskName": "Lab", "State": 3, "Description": "Test"}]
        ctx = FakeContext(query_results=[rows])
        output = run(ctx, {"action": "list", "name": "Lab"})
        self.assertEqual(output[0]["StateLabel"], "Ready")
        self.assertEqual(ctx.queries[0].query.namespace.casefold(), NAMESPACE.casefold())
        self.assertEqual(ctx.queries[0].query.properties["State"], 3)
        self.assertEqual(ctx.methods, [])

    def test_start_stop_export_use_documented_provider_and_order(self):
        for action, method in (("run", "StartByPath"), ("stop", "StopByPath"), ("export", "ExportByName")):
            with self.subTest(action=action):
                ctx = FakeContext(method_results=[{"ReturnValue": 0, "cmdletOutput": "<Task/>"}], modifies_remote=True)
                result = run(ctx, {"action": action, "name": "Lab", "path": "\\Folder\\"})
                self.assertEqual(ctx.methods[0].path, "PS_ScheduledTask")
                self.assertEqual(ctx.methods[0].method, method)
                self.assertEqual(list(ctx.methods[0].inputs), ["TaskName", "TaskPath"] if action == "export" else ["TaskPath", "TaskName"])
                self.assertEqual(result[0]["TaskName"], "Lab")

    def test_delete_is_exact_instance_not_a_method(self):
        ctx = FakeContext(delete_results=[{"Deleted": True}], modifies_remote=True)
        run(ctx, {"action": "delete", "name": "Lab", "path": "\\Folder\\"})
        self.assertEqual(ctx.deletions[0].path, 'MSFT_ScheduledTask.TaskPath="\\\\Folder\\\\",TaskName="Lab"')
        self.assertEqual(ctx.deletions[0].namespace.casefold(), NAMESPACE.casefold())
        self.assertEqual(ctx.methods, [])

    def test_create_full_signature_xml_escaping_and_no_implicit_run(self):
        ctx = FakeContext(query_results=[[]], method_results=[{"ReturnValue": 0}], modifies_remote=True)
        args = {"action": "create", "name": "Lab", "executable": r"C:\Tools\a&b.exe",
                "arguments": '<a> "quoted"', "account": "LOCAL SERVICE", "description": "Test"}
        run(ctx, args)
        self.assertEqual(len(ctx.methods), 1)
        method = ctx.methods[0]
        self.assertEqual(method.method, "RegisterByXml")
        self.assertEqual(list(method.inputs), ["Force", "Xml", "Password", "User", "TaskPath", "TaskName"])
        self.assertEqual(method.inputs["Force"], (11, False))
        self.assertEqual(method.inputs["Password"], (8, None))
        xml = ET.fromstring(method.inputs["Xml"][1])
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        self.assertEqual(xml.find("t:Actions/t:Exec/t:Command", ns).text, args["executable"])
        self.assertEqual(xml.find("t:Actions/t:Exec/t:Arguments", ns).text, args["arguments"])
        self.assertEqual(xml.find("t:Principals/t:Principal/t:UserId", ns).text, "S-1-5-19")
        self.assertIsNone(xml.find("t:Triggers", ns))

    def test_create_requires_explicit_account_and_aware_schedule(self):
        base = {"action": "create", "name": "Lab", "executable": r"C:\test.exe", "account": "SYSTEM"}
        for changes in ({"account": None}, {"schedule": "once"}, {"schedule": "daily", "start": "2026-11-01T09:00:00"},
                        {"start": "2026-11-01T09:00:00Z"}, {"executable": '"C:\\test.exe"'}, {"arguments": "\x01"}):
            with self.subTest(changes=changes):
                ctx = FakeContext(modifies_remote=True)
                with self.assertRaises(PluginError):
                    run(ctx, {**base, **changes})
                self.assertEqual(ctx.methods, [])
                self.assertEqual(ctx.queries, [])
        for schedule in ("once", "daily"):
            xml = task_xml({**base, "schedule": schedule, "start": "2026-11-01T09:00:00-05:00"})
            self.assertIn("2026-11-01T09:00:00-05:00", xml)
            self.assertIn("TimeTrigger" if schedule == "once" else "CalendarTrigger", xml)

    def test_create_refuses_existing_task(self):
        ctx = FakeContext(query_results=[[{"TaskName": "Lab", "TaskPath": "\\"}]], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "already exists"):
            run(ctx, {"action": "create", "name": "Lab", "executable": r"C:\test.exe", "account": "SYSTEM"})
        self.assertEqual(ctx.methods, [])

    def test_provider_failure_is_not_retried(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 5}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "provider code 5"):
            run(ctx, {"action": "run", "name": "Lab"})
        self.assertEqual(len(ctx.methods), 1)

    def test_invalid_targets_and_irrelevant_options_are_rejected(self):
        for args in ({"action": "delete"}, {"action": "run", "name": "*"},
                     {"action": "delete", "name": "Lab", "path": "\\..\\"},
                     {"action": "run", "name": "Lab", "account": "SYSTEM"}):
            ctx = FakeContext(modifies_remote=True)
            with self.assertRaises(PluginError):
                run(ctx, args)
            self.assertEqual(ctx.methods + ctx.queries + ctx.deletions, [])
