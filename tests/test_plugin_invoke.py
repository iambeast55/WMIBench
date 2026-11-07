import struct
from pathlib import Path
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remoteops.commands.plugin_api import FakeContext, PluginContext, PluginError
from remoteops.commands.wmi_invocation import compile_method, object_path, validate_path
from remoteops.commands.wmi_definitions import compile_definition
from remoteops.commands.templates import PreparedCommand
from remoteops.core.models import AuthMethod, ConnectionMode, CredentialProfile, Host, SessionState
from remoteops.core.credentials import ResolvedCredential
from remoteops.remoting.wmi import NativeWmiClient, WmiSession
from remoteops.remoting.dcom_session import DcomWmiSession
from remoteops.remoting.wmi_methods import method_request, _parameters
from remoteops.remoting.wmi_query import decode_instance, WmiQueryError
from remoteops.remoting.rpc_probe import RpcProbeError
from test_wmi_methods import CID, read_request, ENUM_OBJECT, INPUTS


class InvocationApiTests(unittest.TestCase):
    def test_powershell_file_loads_local_functions_without_staging(self):
        with tempfile.NamedTemporaryFile(suffix=".ps1", mode="w", encoding="utf-8", delete=False) as file:
            file.write("function Read-Me { param($Arg) $Arg }")
            path = file.name
        try:
            ctx = FakeContext(backend="winrm", powershell_results=[{"records": [], "stdout": ""}])
            ctx.powershell_file(path)
            self.assertEqual(ctx.scripts, ["function Read-Me { param($Arg) $Arg }"])
            self.assertIn("powershell_file", ctx.capabilities)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_powershell_accepts_decoded_script_bytes(self):
        ctx = FakeContext(backend="winrm", powershell_results=[{"records": []}])
        self.assertEqual(ctx.powershell("Write-Output ok".encode("utf-8")), {"records": []})
        self.assertEqual(ctx.scripts, ["Write-Output ok"])

    def test_missing_output_is_explicit_and_does_not_invent_provider_code(self):
        ctx = FakeContext(method_results=[{}], modifies_remote=True)
        self.assertIn("invoke_allow_no_output", ctx.capabilities)
        self.assertEqual(ctx.invoke("Example", "Change", allow_no_output=True), {})
        self.assertTrue(ctx.methods[0].allow_no_output)
        self.assertFalse(compile_method("Example", "Change").allow_no_output)
        with self.assertRaisesRegex(PluginError, "missing integer"):
            ctx.require_success({})
        for value in (1, "true", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "boolean"):
                compile_method("Example", "Change", allow_no_output=value)

    def test_path_escaping_and_targets(self):
        self.assertEqual(object_path("Win32_Service", {"Name": 'a"b\\c'}), 'Win32_Service.Name="a\\"b\\\\c"')
        self.assertEqual(object_path("Example", {"Index": 4, "Enabled": True}), "Example.Index=4,Enabled=true")
        self.assertEqual(object_path("Example", singleton=True), "Example=@")
        self.assertEqual(object_path("StdRegProv"), "StdRegProv")
        for path in ('//other/root/cimv2:Example', 'root/cimv2:Example', 'Example.Key="x" trailing',
                     'Example.Key="x",key="y"', 'Example.Key="x\n"', 'Example.Key="a\\x"'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_path(path)
        for keys in ({"Name": None}, {"Name": 1.1}, {"Name": "x\0"}, {"Name": "x", "name": "y"}):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                object_path("Example", keys)

    def test_invoke_records_typed_plan_and_requires_mutation_declaration(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
        result = ctx.invoke(ctx.object_path("Win32_Service", {"Name": "Spooler"}), "StartService")
        self.assertEqual(result, {"ReturnValue": 0})
        self.assertEqual(ctx.methods[0].outputs, {"ReturnValue": 19})
        ctx.require_success(result)
        denied = FakeContext(method_results=[result])
        with self.assertRaisesRegex(PluginError, "modifies_remote"):
            denied.invoke("Example", "Change")
        self.assertEqual(denied.methods, [])
        with self.assertRaisesRegex(PluginError, "no backend fallback"):
            FakeContext(backend="winrm", modifies_remote=True).invoke("Example", "Change")

    def test_provider_codes_are_not_transport_errors(self):
        ctx = FakeContext(method_results=[{"ReturnValue": 5}], modifies_remote=True)
        result = ctx.invoke("Example", "Change")
        self.assertEqual(result["ReturnValue"], 5)
        with self.assertRaisesRegex(PluginError, "provider code 5"):
            ctx.require_success(result)
        self.assertEqual(ctx.require_success(result, (0, 5)), result)
        with self.assertRaisesRegex(PluginError, "missing integer"):
            ctx.require_success({})
        self.assertEqual(len(ctx.methods), 1)

    def test_namespace_types_inputs_outputs_and_null(self):
        plan = compile_method("StdRegProv", "GetStringValue",
                              {"hDefKey": ("uint32", 0x80000002), "sSubKeyName": ("string", "SOFTWARE"),
                               "sValueName": ("string", None)},
                              {"ReturnValue": "uint32", "sValue": "string"}, "root/default")
        self.assertEqual(plan.namespace, "root/default")
        self.assertEqual(plan.inputs["sValueName"], (8, None))
        original = {"Names": ("string[]", ["one", "two"])}
        plan = compile_method("Example", "Change", original)
        original["Names"][1].append("three")
        self.assertEqual(plan.inputs["Names"][1], ("one", "two"))

    def test_invalid_values_fail_before_callback(self):
        for pair in (("uint32", -1), ("uint32", True), ("string", 4), ("boolean", 1),
                     ("real64", float("nan")), ("object", {}), ("string[]", [None]),
                     ("char16", "ab"), ("string", "bad\0"), ("uint64", 2**64)):
            ctx = FakeContext(modifies_remote=True)
            with self.subTest(pair=pair), self.assertRaises(ValueError):
                ctx.invoke("Example", "Change", inputs={"Input": pair})
            self.assertEqual(ctx.methods, [])
        for opts in ({"outputs": {}}, {"outputs": {"Result": "object"}}, {"timeout_seconds": 0},
                     {"namespace": "//other/root"}, {"inputs": {"X": ("string", "a"), "x": ("string", "b")}}):
            with self.subTest(opts=opts), self.assertRaises(ValueError):
                compile_method("Example", "Change", **opts)

    def test_cancellable_wait_and_no_second_method_after_cancel(self):
        cancel = threading.Event()
        calls = []

        def invoke(plan, timeout):
            calls.append(plan)
            cancel.set()
            return {"ReturnValue": 0}

        ctx = PluginContext(backend="wmi_dcom", invoke=invoke, modifies_remote=True, cancel=cancel)
        self.assertEqual(ctx.invoke("Example", "Change"), {"ReturnValue": 0})
        with self.assertRaisesRegex(PluginError, "cancelled"):
            ctx.invoke("Example", "Change")
        with self.assertRaisesRegex(PluginError, "cancelled"):
            ctx.sleep(10)
        self.assertEqual(len(calls), 1)
        with self.assertRaisesRegex(PluginError, "deadline"):
            FakeContext(timeout_seconds=0).sleep(1)
        for duration in (-1, float("nan"), True):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                FakeContext().sleep(duration)

    def test_extended_query_does_not_expand_json_command_schema(self):
        ctx = FakeContext(query_results=[[]])
        ctx.query(class_name="MSFT_Example", namespace="root/standardcimv2",
                  properties={"Name": "string", "Addresses": "string[]"}, take=5)
        self.assertEqual(ctx.queries[0].query.take, 5)
        self.assertEqual(ctx.queries[0].query.namespace, "root/standardcimv2")
        self.assertEqual(ctx.queries[0].query.properties["Addresses"], 0x2008)
        definition = {"operation": "query", "namespace": "root/standardcimv2", "class": "Example",
                      "properties": {"Values": "uint32[]"}}
        with self.assertRaises(ValueError):
            compile_definition(definition, {}, {})
        with self.assertRaises(ValueError):
            ctx.query(class_name="Example", properties={"Values": "uint32[]"},
                      filters=[{"property": "Values", "operator": "eq", "value": 1}])
        with self.assertRaises(ValueError):
            ctx.query(class_name="Example", properties={"Value": "uint32"}, rows=5, take=6)


class GenericCodecTests(unittest.TestCase):
    def test_lookup_sorted_without_reordering_ids_or_padding_values(self):
        obj = _parameters({"ZByte": (17, 7), "Alpha": (11, True)})
        class_start = 57  # 48-byte custom OBJREF, 8-byte encoding header, 1-byte flags.
        class_size = struct.unpack_from("<I", obj, class_start)[0]
        self.assertEqual(struct.unpack_from("<I", obj, class_start + 9)[0], 4)
        cursor = class_start + 13
        cursor += struct.unpack_from("<I", obj, cursor)[0]  # Derivation list.
        cursor += struct.unpack_from("<I", obj, cursor)[0]  # Qualifier block.
        self.assertEqual(struct.unpack_from("<I", obj, cursor)[0], 2)
        cursor += 4
        lookups = [struct.unpack_from("<II", obj, cursor + i * 8) for i in range(2)]
        cursor += 16
        self.assertEqual(obj[cursor:cursor + 4], b"\x05\xff\xff\xff")
        heap = obj[cursor + 8:class_start + class_size]
        names, orders, offsets = [], [], []
        for name_ref, info_ref in lookups:
            names.append(heap[name_ref + 1:heap.index(b"\0", name_ref + 1)].decode("ascii"))
            kind, order, offset = struct.unpack_from("<IHI", heap, info_ref)
            orders.append(order)
            offsets.append(offset)
        self.assertEqual(names, ["Alpha", "ZByte"])
        self.assertEqual(orders, [1, 0])
        self.assertEqual(offsets, [1, 0])
        instance = class_start + class_size
        self.assertEqual(obj[instance + 9:instance + 13], b"\0\x07\xff\xff")

    def test_legacy_enum_fixture_remains_identical(self):
        self.assertEqual(_parameters(INPUTS), ENUM_OBJECT)

    def test_generic_packed_scalar_values_round_trip(self):
        inputs = {"ZByte": (17, 255), "ABool": (11, True), "S16": (2, -123), "S32": (3, -999),
                  "U16": (18, 65535), "U32": (19, 2**32 - 1), "S8": (16, -128),
                  "S64": (20, -(2**63)), "U64": (21, 2**64 - 1), "Real": (4, 1.5),
                  "Double": (5, -1.25), "Text": (8, "cafe\u00e9\u96ea"), "Null": (8, None),
                  "Date": (101, "20261018000000.000000+000"), "Ref": (102, 'Example.Key="A"'), "Char": (103, "Z")}
        request = method_request((5, 7), CID, "Example", "Change", inputs, outputs={"ReturnValue": 19})
        obj = read_request(self, request, "Example", "Change")
        decoded = decode_instance(obj, "__PARAMETERS", {name: kind for name, (kind, _) in inputs.items()})
        self.assertEqual(decoded, {name: value for name, (_, value) in inputs.items()})

    def test_generic_arrays_round_trip(self):
        inputs = {"Names": (0x2008, ("one", "\u96ea")), "Numbers": (0x2013, [0, 2**32 - 1]),
                  "Flags": (0x200B, [True, False]), "Empty": (0x2015, []), "Null": (0x2008, None)}
        request = method_request((5, 7), CID, 'Example.Key="test"', "Change", inputs, outputs={"ReturnValue": 19})
        obj = read_request(self, request, 'Example.Key="test"', "Change")
        self.assertEqual(decode_instance(obj, "__PARAMETERS", {k: t for k, (t, _) in inputs.items()}),
                         {k: list(v) if isinstance(v, tuple) else v for k, (_, v) in inputs.items()})

    def test_generic_no_inputs_and_bounded_request(self):
        request = method_request((5, 7), CID, "Example", "Change", {}, outputs={"Result": 8})
        self.assertIsNone(read_request(self, request, "Example", "Change"))
        with self.assertRaisesRegex(ValueError, "request exceeds"):
            method_request((5, 7), CID, "Example", "Change", {"Values": (0x2008, ["x" * 1024] * 1024)},
                           outputs={"ReturnValue": 19})


class InvocationAdapterTests(unittest.TestCase):
    def test_native_null_output_only_accepted_when_explicitly_allowed(self):
        from contextlib import nullcontext
        from remoteops.commands.wmi_catalog import WmiCommandError
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        client.services.ExecMethod.return_value = None
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            with self.assertRaisesRegex(WmiCommandError, "no output object; outcome unconfirmed"):
                client.invoke("PS_ScheduledTask", "StartByPath", {}, 10, outputs={"ReturnValue": 19})
            self.assertEqual(client.services.ExecMethod.call_count, 1)
            self.assertEqual(client.invoke("PS_ScheduledTask", "StartByPath", {}, 10,
                                          outputs={"ReturnValue": 19}, allow_no_output=True), {})
            self.assertEqual(client.services.ExecMethod.call_count, 2)
            client.services.ExecMethod.side_effect = RuntimeError("provider rejected request")
            with self.assertRaisesRegex(RuntimeError, "provider rejected"):
                client.invoke("PS_ScheduledTask", "StartByPath", {}, 10,
                              outputs={"ReturnValue": 19}, allow_no_output=True)
            self.assertEqual(client.services.ExecMethod.call_count, 3)

    def test_native_optional_output_still_checks_returned_schema_and_codes(self):
        from contextlib import nullcontext
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        client.services.ExecMethod.return_value = SimpleNamespace(Properties_=[
            SimpleNamespace(Name="ReturnValue", CIMType=19, IsArray=False, Value=5)])
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            self.assertEqual(client.invoke("Example", "Change", {}, 10,
                outputs={"ReturnValue": 19}, allow_no_output=True), {"ReturnValue": 5})
            client.services.ExecMethod.return_value = SimpleNamespace(Properties_=[])
            with self.assertRaisesRegex(Exception, "outcome unconfirmed"):
                client.invoke("Example", "Change", {}, 10,
                              outputs={"ReturnValue": 19}, allow_no_output=True)

    def test_both_sessions_forward_no_output_policy_and_stay_connected(self):
        plan = compile_method("PS_ScheduledTask", "StartByPath", allow_no_output=True)
        command = PreparedCommand("task", "task", "Task", "", {}, native_wmi=True, method_plan=plan)
        for adapter in (WmiSession, DcomWmiSession):
            with self.subTest(adapter=adapter.__name__):
                client = Mock()
                client.invoke.return_value = {}
                with patch.object(adapter, "client_factory", return_value=client):
                    session = adapter(Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI),
                                      CredentialProfile("Ops", "user", method=AuthMethod.NTLM))
                    try:
                        session.connect(ResolvedCredential("user", "fixture"))
                        result = session.run_prepared(command)
                        self.assertTrue(result.succeeded, result.stderr)
                        self.assertEqual(result.records, [{}])
                        self.assertEqual(session.state, SessionState.CONNECTED)
                        client.invoke.assert_called_once()
                        self.assertIs(client.invoke.call_args.kwargs["allow_no_output"], True)
                    finally:
                        session.disconnect()

    def test_native_process_create_uses_services_execmethod_once(self):
        from contextlib import nullcontext
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        parameter = SimpleNamespace(CIMType=8, IsArray=False, Value=None)
        parameters = client.services.Get.return_value.Methods_.return_value.InParameters.SpawnInstance_.return_value
        parameters.Properties_.return_value = parameter
        client.services.ExecMethod.return_value = SimpleNamespace(Properties_=[
            SimpleNamespace(Name="ReturnValue", CIMType=19, IsArray=False, Value=0),
            SimpleNamespace(Name="ProcessId", CIMType=19, IsArray=False, Value=4321),
        ])
        command_line = r'"C:\Windows\System32\notepad.exe"'
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            output = client.invoke("Win32_Process", "Create", {"CommandLine": (8, command_line)}, 10,
                                   outputs={"ReturnValue": 19, "ProcessId": 19})
        self.assertEqual(output, {"ReturnValue": 0, "ProcessId": 4321})
        self.assertEqual(parameter.Value, command_line)
        client.services.Get.assert_called_once_with("Win32_Process")
        client.services.Get.return_value.Methods_.assert_called_once_with("Create")
        parameters.Properties_.assert_called_once_with("CommandLine")
        client.services.ExecMethod.assert_called_once_with("Win32_Process", "Create", parameters)

    def test_native_preparation_errors_do_not_dispatch_or_disconnect(self):
        from contextlib import nullcontext
        from remoteops.commands.templates import prepare_command
        plan = compile_method("Win32_Process", "Create", {"CommandLine": ("string", "notepad.exe")})
        command = PreparedCommand("create", "create", "Create", "", {}, native_wmi=True, method_plan=plan)
        for failure in (AttributeError("missing member"), TypeError("invalid parameter binding")):
            with self.subTest(failure=type(failure).__name__):
                client = NativeWmiClient.__new__(NativeWmiClient)
                client.services = Mock(spec_set=["Get", "ExecMethod", "ExecQuery"])
                client.services.Get.side_effect = failure
                client.services.ExecQuery.return_value = []
                session = WmiSession(Host("Lab", "example.invalid"), CredentialProfile("Ops", "user"))
                session.client = client
                session.namespace_clients = {"root/cimv2": client}
                session.state = SessionState.CONNECTED
                with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
                    result = session.run_prepared(command)
                    self.assertFalse(result.succeeded)
                    self.assertIn("action was not sent", result.stderr)
                    self.assertNotIn("outcome unconfirmed", result.stderr)
                    self.assertEqual(session.state, SessionState.CONNECTED)
                    self.assertTrue(session.run_prepared(prepare_command("services")).succeeded)
                client.services.ExecMethod.assert_not_called()

    def test_native_builtin_method_uses_services_execmethod(self):
        from contextlib import nullcontext
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        client.services.ExecMethod.return_value = SimpleNamespace(Properties_=[
            SimpleNamespace(Name="ReturnValue", Value=0),
        ])
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            output = client.invoke('Win32_Service.Name="Spooler"', "StartService", {}, 10)
        self.assertEqual(output, {"ReturnValue": 0})
        client.services.ExecMethod.assert_called_once_with('Win32_Service.Name="Spooler"', "StartService", None)

    def test_both_adapters_forward_generic_plan_once_and_keep_confirmed_failures(self):
        host = Host("Lab", "example.invalid", connection_mode=ConnectionMode.WMI)
        profile = CredentialProfile("Fixture", "fixture", method=AuthMethod.NTLM)
        credential = ResolvedCredential("fixture", "fixture-secret")
        plan = compile_method('Win32_Service.Name="Spooler"', "PauseService")
        command = PreparedCommand("service-action", "service-action", "Service action", "", {}, native_wmi=True, method_plan=plan)
        for adapter in (WmiSession, DcomWmiSession):
            with self.subTest(adapter=adapter.__name__):
                client = Mock()
                client.invoke.return_value = {"ReturnValue": 2}
                with patch.object(adapter, "client_factory", return_value=client):
                    session = adapter(host, profile)
                    session.connect(credential)
                    try:
                        result = session.run_prepared(command)
                        self.assertEqual(result.records, [{"ReturnValue": 2}])
                        self.assertEqual(session.state, SessionState.CONNECTED)
                        self.assertEqual(client.invoke.call_count, 1)
                        self.assertEqual(client.invoke.call_args.kwargs["outputs"], {"ReturnValue": 19})
                        client.invoke.side_effect = ValueError("invalid input")
                        self.assertFalse(session.run_prepared(command).succeeded)
                        self.assertEqual(session.state, SessionState.CONNECTED)
                        client.invoke.side_effect = RpcProbeError("broken transport")
                        self.assertFalse(session.run_prepared(command).succeeded)
                        self.assertEqual(session.state, SessionState.ERROR)
                        self.assertEqual(client.invoke.call_count, 3)
                    finally:
                        session.disconnect()

    def test_native_com_checks_provider_types_and_normalizes_outputs(self):
        from contextlib import nullcontext
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        output = SimpleNamespace(Properties_=[SimpleNamespace(Name="ReturnValue", CIMType=19, IsArray=False, Value=0),
                                               SimpleNamespace(Name="Numbers", CIMType=21, IsArray=True, Value=("10", "20"))])
        client.services.ExecMethod.return_value = output
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            result = client.invoke("Example", "Change", {}, 10, outputs={"ReturnValue": 19, "Numbers": 0x2015})
            self.assertEqual(result, {"ReturnValue": 0, "Numbers": [10, 20]})
            with self.assertRaisesRegex(Exception, "outcome unconfirmed"):
                client.invoke("Example", "Change", {}, 10, outputs={"Wrong": 8})

    def test_native_input_type_mismatch_prevents_dispatch(self):
        from contextlib import nullcontext
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Get", "ExecMethod"])
        parameters = client.services.Get.return_value.Methods_.return_value.InParameters.SpawnInstance_.return_value
        parameters.Properties_.return_value = SimpleNamespace(CIMType=19, IsArray=False, Value=None)
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()), self.assertRaisesRegex(ValueError, "input type"):
            client.invoke("Example", "Change", {"Value": (8, "wrong")}, 10, outputs={"ReturnValue": 19})
        client.services.ExecMethod.assert_not_called()
