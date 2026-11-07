import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext


class CommandTests(unittest.TestCase):
    def test_typed_inputs_and_array_output(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": ["VendorA", "VendorB"]}],
                          modifies_remote=True)
        self.assertEqual(run(ctx, {"subkey": "SOFTWARE"}),
                         [{"Subkey": "VendorA"}, {"Subkey": "VendorB"}])
        self.assertEqual(ctx.methods[0].namespace, "root/default")
        self.assertEqual(ctx.methods[0].inputs["hDefKey"], (19, 0x80000002))

    def test_null_list_is_empty(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": None}], modifies_remote=True)
        self.assertEqual(run(ctx, {"subkey": "SOFTWARE"}), [])
