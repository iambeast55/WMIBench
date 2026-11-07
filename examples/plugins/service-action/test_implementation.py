import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext, PluginError


class CommandTests(unittest.TestCase):
    def test_start_once(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        result = run(ctx, {"service_name": "Spooler", "action": "StartService"})
        self.assertEqual(result[0]["ReturnValue"], 0)
        self.assertEqual(len(ctx.methods), 1)
        self.assertEqual(ctx.methods[0].path, 'Win32_Service.Name="Spooler"')

    def test_provider_failure_is_not_retried(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 2}], modifies_remote=True)
        with self.assertRaises(PluginError):
            run(ctx, {"service_name": "Spooler", "action": "StopService"})
        self.assertEqual(len(ctx.methods), 1)
