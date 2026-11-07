import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext


class CommandTests(unittest.TestCase):
    def test_two_queries(self):
        ctx = FakeContext(query_results=[
            [{"CSName": "LAB", "Caption": "Windows"}], [{"Manufacturer": "Example"}],
        ])
        self.assertEqual(run(ctx, {}), [{"Host": "LAB", "OS": "Windows", "Vendor": "Example"}])
        self.assertEqual(len(ctx.queries), 2)

    def test_missing_bios(self):
        ctx = FakeContext(query_results=[[{"CSName": "LAB", "Caption": "Windows"}], []])
        self.assertIsNone(run(ctx, {})[0]["Vendor"])

    def test_winrm_uses_only_explicit_powershell_helper(self):
        rows = [{"Host": "LAB", "OS": "Windows", "Vendor": "Example"}]
        ctx = FakeContext(backend="winrm", powershell_results=[{"records": rows, "stdout": ""}])
        self.assertEqual(run(ctx, {}), rows)
        self.assertEqual(len(ctx.scripts), 1)
        self.assertEqual(ctx.queries, [])
