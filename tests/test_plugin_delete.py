import struct
import threading
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import UUID

from remoteops.commands.plugin_api import FakeContext, PluginContext, PluginError
from remoteops.commands.templates import PreparedCommand
from remoteops.commands.wmi_invocation import compile_delete, compile_method
from remoteops.core.models import AuthMethod, CredentialProfile, Host, SessionState
from remoteops.remoting.dcom_session import DcomWmiSession
from remoteops.remoting.dcom_wire import DcomStatusError
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError
from remoteops.remoting.wmi import NativeWmiClient, WmiSession
from remoteops.remoting.wmi_methods import decode_delete, delete_request, method_request


class DeleteApiTests(unittest.TestCase):
    def test_instance_guard_and_typed_plan(self):
        path = 'MSFT_ScheduledTask.TaskName="Lab",TaskPath="\\\\"'
        ctx = FakeContext(delete_results=[{"Deleted": True}], modifies_remote=True)
        self.assertEqual(ctx.delete_instance(path), {"Deleted": True})
        self.assertEqual(ctx.deletions[0].path, path)
        self.assertIn("delete_instance", ctx.capabilities)
        for invalid in ("MSFT_ScheduledTask", "//other/root/cimv2:Example", 'Example.Name="*" trailing'):
            with self.assertRaises(ValueError):
                compile_delete(invalid)
        self.assertEqual(compile_delete("Example=@").path, "Example=@")
        for ctx in (FakeContext(), FakeContext(backend="winrm", modifies_remote=True)):
            with self.assertRaises(PluginError):
                ctx.delete_instance(path)
            self.assertEqual(ctx.deletions, [])

    def test_cancellation_does_not_replay_confirmed_delete(self):
        cancel = threading.Event()
        def delete(plan, budget):
            cancel.set()
            return {"Deleted": True}
        callback = Mock(side_effect=delete)
        ctx = PluginContext(backend="wmi_dcom", modifies_remote=True, cancel=cancel, delete_instance=callback)
        self.assertTrue(ctx.delete_instance('Example.Name="x"')["Deleted"])
        with self.assertRaisesRegex(PluginError, "Side effects"):
            ctx.delete_instance('Example.Name="x"')
        callback.assert_called_once()

    def test_long_xml_plan_and_single_fragment_limit(self):
        plan = compile_method("PS_ScheduledTask", "RegisterByXml", {"Xml": ("string", "x" * 5000)})
        self.assertEqual(len(plan.inputs["Xml"][1]), 5000)
        with self.assertRaisesRegex(ValueError, "request exceeds"):
            method_request((5, 7), UUID(int=0), plan.path, plan.method, plan.inputs, outputs=plan.outputs)
        with self.assertRaisesRegex(ValueError, "65536"):
            compile_method("Example", "Change", {"Xml": ("string", "x" * 65537)})

    def test_native_delete_uses_services_delete_once(self):
        client = NativeWmiClient.__new__(NativeWmiClient)
        client.services = Mock(spec_set=["Delete"])
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            self.assertTrue(client.delete_instance('Example.Name="x"', 10)["Deleted"])
        client.services.Delete.assert_called_once_with('Example.Name="x"', 0)
        with self.assertRaises(ValueError):
            client.delete_instance("Example", 10)
        self.assertEqual(client.services.Delete.call_count, 1)

    def test_both_sessions_preserve_provider_errors_but_not_lost_replies(self):
        command = PreparedCommand("delete", "delete", "Delete", "", {}, native_wmi=True,
                                  delete_plan=compile_delete('Example.Name="x"'))
        for adapter in (WmiSession, DcomWmiSession):
            with self.subTest(adapter=adapter.__name__):
                client = Mock()
                client.delete_instance.return_value = {"Deleted": True}
                with patch.object(adapter, "client_factory", return_value=client):
                    session = adapter(Host("Lab", "example.invalid"), CredentialProfile("Ops", "user", method=AuthMethod.NTLM))
                    session.connect(SimpleNamespace(username="user", secret="fixture"))
                    try:
                        self.assertTrue(session.run_prepared(command).succeeded)
                        client.delete_instance.side_effect = ValueError("provider rejected")
                        self.assertFalse(session.run_prepared(command).succeeded)
                        self.assertEqual(session.state, SessionState.CONNECTED)
                        client.delete_instance.side_effect = RpcProbeError("lost reply")
                        self.assertFalse(session.run_prepared(command).succeeded)
                        self.assertEqual(session.state, SessionState.ERROR)
                        self.assertEqual(client.delete_instance.call_count, 3)
                    finally:
                        session.disconnect()


class DeleteWireTests(unittest.TestCase):
    def test_op16_request_independent_layout(self):
        path = 'Example.Name="x"'
        cid = UUID("00112233-4455-6677-8899-aabbccddeeff")
        request = delete_request((5, 7), cid, path)
        prefix = bytes.fromhex("05000700 00000000 00000000 33221100554477668899aabbccddeeff 00000000")
        payload = path.encode("utf-16-le")
        expected = prefix + struct.pack("<IIII", 0x20000, len(payload)//2, len(payload), len(payload)//2) + payload
        expected += bytes(-len(expected) % 4) + bytes(12)
        self.assertEqual(request, expected)

    def test_response_success_provider_error_and_invalid_reference(self):
        decode_delete(bytes(16))
        with self.assertRaises(DcomStatusError):
            decode_delete(bytes(12) + struct.pack("<I", 0x80041003))
        for reply in (bytes(15), bytes(17), bytes(8) + struct.pack("<II", 1, 0)):
            with self.assertRaises(RpcProtocolError):
                decode_delete(reply)
