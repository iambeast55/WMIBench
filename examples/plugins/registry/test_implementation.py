import unittest

from implementation import run, parse_data, TYPES
from remoteops.commands.plugin_api import FakeContext, PluginError


class CommandTests(unittest.TestCase):
    def test_keys_and_value_types(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": ["Demo"]},
                                         {"ReturnValue": 0, "sNames": ["Flag", "Other"], "Types": [4, 99]}], modifies_remote=True)
        self.assertEqual(run(ctx, {"action": "keys"})[0]["Key"], "Demo")
        values = run(ctx, {"action": "values", "subkey": "SOFTWARE"})
        self.assertEqual(values[0]["Type"], "REG_DWORD")
        self.assertEqual(values[1]["TypeCode"], 99)
        self.assertEqual(ctx.methods[1].outputs["Types"], 0x2003)
        self.assertEqual(ctx.methods[0].namespace, "root/default")

    def test_empty_enumeration_and_mismatched_arrays(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": None, "Types": None}], modifies_remote=True)
        self.assertEqual(run(ctx, {"action": "values"}), [])
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": ["x"], "Types": []}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "mismatched"):
            run(ctx, {"action": "values"})

    def test_all_set_types_and_parameter_order(self):
        for kind, text, expected in (("SZ", "", ""), ("EXPAND_SZ", "%SystemRoot%", "%SystemRoot%"),
                                     ("DWORD", "0xffffffff", 2**32-1), ("QWORD", str(2**64-1), 2**64-1),
                                     ("BINARY", "00 ff 10", (0, 255, 16)), ("MULTI_SZ", '["a","b"]', ("a", "b"))):
            with self.subTest(kind=kind):
                ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
                run(ctx, {"action": "set", "subkey": "SOFTWARE\\Demo", "name": "Value", "type": kind, "data": text})
                plan = ctx.methods[0]
                _, suffix, field, _ = TYPES[kind]
                self.assertEqual(plan.method, "Set" + suffix)
                self.assertEqual(list(plan.inputs), ["hDefKey", "sSubKeyName", "sValueName", field])
                self.assertEqual(plan.inputs[field][1], expected)

    def test_all_get_types(self):
        for kind, (_, suffix, field, _) in TYPES.items():
            ctx = FakeContext(method_results=[{"ReturnValue": 0, field: "fixture"}], modifies_remote=True)
            rows = run(ctx, {"action": "get", "subkey": "SOFTWARE\\Demo", "name": "Value", "type": kind})
            self.assertEqual(ctx.methods[0].method, "Get" + suffix)
            self.assertEqual(rows[0]["Data"], "fixture")

    def test_create_key_delete_value_and_leaf_key(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        run(ctx, {"action": "create-key", "subkey": "SOFTWARE\\Demo"})
        self.assertEqual(ctx.methods[0].method, "CreateKey")
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        run(ctx, {"action": "delete-value", "subkey": "SOFTWARE\\Demo", "default_value": True})
        self.assertEqual(ctx.methods[0].inputs["sValueName"], (8, ""))
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": None}, {"ReturnValue": 0}], modifies_remote=True)
        run(ctx, {"action": "delete-key", "subkey": "SOFTWARE\\Demo"})
        self.assertEqual([plan.method for plan in ctx.methods], ["EnumKey", "DeleteKey"])

    def test_no_recursive_delete_or_retry(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0, "sNames": ["Child"]}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "Recursive"):
            run(ctx, {"action": "delete-key", "subkey": "SOFTWARE\\Demo"})
        self.assertEqual(len(ctx.methods), 1)
        ctx = FakeContext(method_results=[{"ReturnValue": 5}], modifies_remote=True)
        with self.assertRaisesRegex(PluginError, "provider code 5"):
            run(ctx, {"action": "create-key", "subkey": "SOFTWARE\\Demo"})
        self.assertEqual(len(ctx.methods), 1)

    def test_invalid_input_never_calls_remote(self):
        base = {"action": "set", "subkey": "SOFTWARE\\Demo", "name": "Value", "type": "DWORD", "data": "1"}
        for changes in ({"data": "-1"}, {"data": str(2**32)}, {"hive": "HKCU"}, {"subkey": ""},
                        {"subkey": "HKLM\\SOFTWARE"}, {"subkey": "\\SOFTWARE"}, {"subkey": "SOFTWARE\\\\Demo"},
                        {"default_value": True}, {"type": "MULTI_SZ", "data": '[null]'},
                        {"type": "BINARY", "data": "abc"}, {"type": "QWORD", "data": str(2**64)}):
            ctx = FakeContext(modifies_remote=True)
            with self.assertRaises(PluginError):
                run(ctx, {**base, **changes})
            self.assertEqual(ctx.methods, [])
        for args in ({"action": "delete-key"}, {"action": "delete-value", "subkey": "SOFTWARE\\Demo"},
                     {"action": "keys", "name": "Oops"}):
            with self.assertRaises(PluginError):
                run(FakeContext(modifies_remote=True), args)

    def test_unsigned_decimal_and_hex_ranges(self):
        self.assertEqual(parse_data("DWORD", "0010"), 10)
        self.assertEqual(parse_data("QWORD", "0xFFFFFFFFFFFFFFFF"), 2**64-1)
