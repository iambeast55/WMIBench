import unittest
import xml.etree.ElementTree as ET

from implementation import run, task_xml, NAMESPACE, ACCOUNTS
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

    def test_run_stop_without_output_report_request_not_task_completion(self):
        for action in ("run", "stop"):
            ctx = FakeContext(method_results=[{}], modifies_remote=True)
            result = run(ctx, {"action": action, "name": "Cals"})
            self.assertEqual(result[0]["Status"], "Request completed")
            self.assertEqual(result[0]["TaskCompletion"], "Not checked")
            self.assertNotIn("ReturnValue", result[0])
            self.assertEqual(len(ctx.methods), 1)
            self.assertTrue(ctx.methods[0].allow_no_output)

    def test_no_output_policy_does_not_hide_provider_failures(self):
        for action in ("run", "stop"):
            ctx = FakeContext(method_results=[{"ReturnValue": 5}], modifies_remote=True)
            with self.assertRaisesRegex(PluginError, "provider code 5"):
                run(ctx, {"action": action, "name": "Cals"})
            self.assertEqual(len(ctx.methods), 1)
        for action in ("create", "export"):
            args = {"action": action, "name": "Cals"}
            if action == "create":
                args.update(executable=r"C:\Windows\System32\calc.exe", account="SYSTEM")
            ctx = FakeContext(query_results=[[]], method_results=[{}], modifies_remote=True)
            with self.assertRaisesRegex(PluginError, "missing integer"):
                run(ctx, args)
            self.assertFalse(ctx.methods[0].allow_no_output)

    def test_old_runtime_rejected_before_running_task(self):
        class OldContext(FakeContext):
            capabilities = ("query", "invoke", "delete_instance")
        ctx = OldContext(method_results=[{}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "Update WMIBench"):
            run(ctx, {"action": "run", "name": "Cals"})
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

    def test_service_account_xml_omits_api_only_logon_type(self):
        ns = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        for account, sid in ACCOUNTS.items():
            for schedule in ("demand", "once", "daily"):
                with self.subTest(account=account, schedule=schedule):
                    args = {"action": "create", "name": "New Calculator", "account": account,
                            "executable": r"C:\Windows\System32\calc.exe", "description": "The task calculator",
                            "working_directory": r"C:\Windows\System32\Tasks", "schedule": schedule}
                    if schedule != "demand":
                        args["start"] = "2026-11-01T09:00:00-05:00"
                    ctx = FakeContext(query_results=[[]], method_results=[{"ReturnValue": 0}], modifies_remote=True)
                    run(ctx, args)
                    method = ctx.methods[0]
                    root = ET.fromstring(method.inputs["Xml"][1])
                    self.assertIsNone(root.find("t:Principals/t:Principal/t:LogonType", ns))
                    self.assertEqual(root.find("t:Principals/t:Principal/t:UserId", ns).text, sid)
                    self.assertEqual(method.inputs["User"], (8, sid))
                    self.assertEqual(root.find("t:Actions/t:Exec/t:WorkingDirectory", ns).text, args["working_directory"])
                    self.assertEqual(len(ctx.methods), 1)

    def test_create_exception_names_operation_and_preserves_original_error_without_retry(self):
        ctx = FakeContext(query_results=[[]], method_results=[PluginError("0x80041001 Generic failure")], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "RegisterByXml failed.*New Calculator") as raised:
            run(ctx, {"action": "create", "name": "New Calculator", "account": "SYSTEM", "executable": r"C:\test.exe"})
        self.assertIn("0x80041001 Generic failure", str(raised.exception))
        self.assertIn("Check whether the task exists", str(raised.exception))
        self.assertEqual(len(ctx.methods), 1)

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
