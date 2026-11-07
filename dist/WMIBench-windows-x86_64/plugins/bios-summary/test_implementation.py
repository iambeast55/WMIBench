import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext


class CommandTests(unittest.TestCase):
    def test_inventory(self):
        ctx = FakeContext(query_results=[[{"Manufacturer": "Example", "SMBIOSBIOSVersion": "1.0"}]])
        self.assertEqual(run(ctx, {}), [{"Vendor": "Example", "Version": "1.0"}])
        self.assertEqual(ctx.queries[0].query.class_name, "Win32_BIOS")

    def test_empty_and_null(self):
        self.assertEqual(run(FakeContext(query_results=[[]]), {}), [])
        self.assertEqual(run(FakeContext(query_results=[[{}]]), {}),
                         [{"Vendor": None, "Version": None}])
