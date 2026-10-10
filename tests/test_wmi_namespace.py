import io
import struct
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from uuid import UUID

from remoteops.remoting import dcom_probe as probe
from remoteops.remoting import dcom_wire as wire
from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError
from test_dcom import CID, IPID, OXID, REMOTE_UNKNOWN, activation_fixture, aligned, objref_fixture

SERVICES_IPID = UUID("aaaaaaaa-1234-5678-90ab-cdef12345678")


def namespace_fixture(status=0, flags=0, objref=None):
    if status:
        return struct.pack("<IIII", flags, 0, 0, status)
    if objref is None:
        objref = objref_fixture(iid=wire.WBEM_SERVICES_INTERFACE, public_refs=3)
        objref = objref[:48] + SERVICES_IPID.bytes_le + objref[64:]
    prefix = struct.pack("<IIIII", flags, 0, 0x20000, len(objref), len(objref))
    return aligned(prefix + objref) + bytes(4)


class NamespaceWireTests(unittest.TestCase):
    def test_login_request_fixed_ndr_layout(self):
        expected = bytes.fromhex(
            "05000700 00000000 00000000 33221100554477668899aabbccddeeff 00000000"
            "00000200 0b000000 00000000 0b000000"
            "72006f006f0074005c00630069006d00760032000000 0000"
            "00000000 00000000 00000000"
        )
        self.assertEqual(wire.namespace_login_request((5, 7), CID, "root/cimv2"), expected)

    def test_namespace_validation_does_not_allow_host_redirection(self):
        self.assertEqual(wire.normalize_namespace("ROOT/cimv2"), "ROOT\\cimv2")
        for name in ("", "cimv2", "root/../other", "root//cimv2", "\\\\other\\root\\cimv2",
                     "root/cimv2\0", "root/cimv2\n", "root/" + "a" * 256):
            with self.subTest(name=name), self.assertRaises(ValueError):
                wire.normalize_namespace(name)

    def test_services_interface_decoded_with_nonzero_orpc_flags(self):
        result = wire.decode_namespace_login(namespace_fixture(flags=0xFFFFFFFF))
        self.assertEqual(result.iid, wire.WBEM_SERVICES_INTERFACE)
        self.assertEqual(result.ipid, SERVICES_IPID)
        self.assertEqual(result.oxid, OXID)
        self.assertEqual(result.public_refs, 3)

    def test_namespace_failures_preserve_hresult(self):
        for status, message in ((0x80041003, "access denied"),
                                (0x8004100E, "does not exist"),
                                (0x80041008, "invalid WMI parameter")):
            with self.subTest(status=status), self.assertRaises(wire.DcomStatusError) as caught:
                wire.decode_namespace_login(namespace_fixture(status=status))
            self.assertEqual(caught.exception.status, status)
            self.assertIn(message, str(caught.exception))
            self.assertIn(f"0x{status:08x}", str(caught.exception))

    def test_success_requires_correct_services_interface(self):
        with self.assertRaisesRegex(RpcProtocolError, "omitted"):
            wire.decode_namespace_login(bytes(16))
        with self.assertRaisesRegex(RpcProtocolError, "expected"):
            wire.decode_namespace_login(namespace_fixture(objref=objref_fixture()))

    def test_malformed_reply_cannot_be_assumed_to_have_no_reference(self):
        reply = namespace_fixture()
        with self.assertRaisesRegex(RpcProtocolError, "unexpected interface pointer"):
            wire.decode_namespace_login(reply[:-4] + struct.pack("<I", 0x80041003))
        with self.assertRaisesRegex(RpcProtocolError, "conformance"):
            wire.decode_namespace_login(reply[:12] + bytes(4) + reply[16:])
        with self.assertRaisesRegex(RpcProtocolError, "trailing"):
            wire.decode_namespace_login(reply + b"extra")
        for size in range(len(reply)):
            with self.subTest(size=size), self.assertRaises(RpcProtocolError):
                wire.decode_namespace_login(reply[:size])

    def test_release_tracks_both_reference_counts(self):
        activation = wire.decode_activation(activation_fixture())
        services = wire.decode_namespace_login(namespace_fixture())
        result = wire.release_request(activation, CID, (activation.reference, services))
        self.assertEqual(len(result), 88)
        self.assertEqual(struct.unpack_from("<H2xI", result, 32), (2, 2))
        self.assertEqual(result[40:56], IPID.bytes_le)
        self.assertEqual(struct.unpack_from("<II", result, 56), (5, 0))
        self.assertEqual(result[64:80], SERVICES_IPID.bytes_le)
        self.assertEqual(struct.unpack_from("<II", result, 80), (3, 0))
        with self.assertRaises(RpcProtocolError):
            wire.release_request(activation, CID, ())
        other = wire.decode_namespace_login(namespace_fixture(objref=objref_fixture(
            iid=wire.WBEM_SERVICES_INTERFACE, oxid=1234
        )))
        with self.assertRaisesRegex(RpcProtocolError, "activated exporter"):
            wire.release_request(activation, CID, (activation.reference, other))


class NamespaceWorkflowTests(unittest.TestCase):
    def run_probe(self, reply=None, login_error=None, bind_error=None, release_error=None,
                  callback=None):
        self.connections, self.closed, self.channels = [], [], {}

        @contextmanager
        def connect(address, username, password, interface, port, deadline):
            self.connections.append((address, interface, port, deadline))
            if interface == wire.WBEM_LOGIN_INTERFACE and bind_error:
                raise bind_error
            channel = Mock()
            self.channels[interface] = channel
            if interface == wire.ACTIVATION:
                channel.call.return_value = activation_fixture()
            elif interface == wire.WBEM_LOGIN_INTERFACE:
                channel.call.return_value = namespace_fixture() if reply is None else reply
                channel.call.side_effect = login_error
            else:
                channel.call.return_value = bytes(12)
                channel.call.side_effect = release_error
            try:
                yield channel
            finally:
                self.closed.append(interface)

        with patch.object(probe, "discover_version", return_value=(5, 7)):
            with patch.object(rpc, "ntlm_connection", side_effect=connect):
                return probe.probe_dcom("192.0.2.10", "synthetic-user", "synthetic-password",
                                        namespace="root/cimv2", progress=callback)

    def release_count(self):
        request = self.channels[wire.REM_UNKNOWN].call.call_args.args[1]
        return struct.unpack_from("<H", request, 32)[0]

    def test_login_only_then_release_both_without_queries(self):
        self.run_probe()
        self.assertEqual([(ip, iid, port) for ip, iid, port, _ in self.connections], [
            ("192.0.2.10", wire.ACTIVATION, 135),
            ("192.0.2.10", wire.WBEM_LOGIN_INTERFACE, 50000),
            ("192.0.2.10", wire.REM_UNKNOWN, 50000),
        ])
        self.assertEqual(self.connections[0][3], self.connections[1][3])
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.WBEM_LOGIN_INTERFACE, wire.REM_UNKNOWN])
        calls = [channel.call.call_args.args for channel in self.channels.values()]
        self.assertEqual([args[0] for args in calls], [0, 6, 5])
        self.assertEqual(calls[1][3], IPID)
        self.assertEqual(calls[2][3], REMOTE_UNKNOWN)
        self.assertEqual(self.release_count(), 2)
        for channel in self.channels.values():
            channel.call.assert_called_once()

    def test_namespace_denial_releases_login_without_claiming_unknown_namespace_object(self):
        with self.assertRaisesRegex(RpcProbeError, "0x80041003") as caught:
            self.run_probe(reply=namespace_fixture(status=0x80041003))
        self.assertEqual(self.release_count(), 1)
        self.assertIn("release confirmed", str(caught.exception))
        self.assertNotIn("namespace reference cleanup unconfirmed", str(caught.exception))

    def test_malformed_or_lost_reply_reports_unknown_namespace_cleanup(self):
        for args in ({"reply": b"malformed"}, {"login_error": TimeoutError()}):
            with self.subTest(args=args):
                with self.assertRaisesRegex(RpcProbeError, "namespace reference cleanup unconfirmed"):
                    self.run_probe(**args)
                self.assertEqual(self.release_count(), 1)

    def test_failed_login_bind_releases_only_login(self):
        with self.assertRaisesRegex(RpcProbeError, "synthetic bind failure") as caught:
            self.run_probe(bind_error=RpcProbeError("synthetic bind failure"))
        self.assertEqual(self.release_count(), 1)
        self.assertNotIn("namespace reference cleanup unconfirmed", str(caught.exception))

    def test_cross_exporter_reference_is_not_released_on_wrong_exporter(self):
        reply = namespace_fixture(objref=objref_fixture(iid=wire.WBEM_SERVICES_INTERFACE, oxid=1234))
        with self.assertRaisesRegex(RpcProbeError, "cross-exporter resolution") as caught:
            self.run_probe(reply=reply)
        self.assertEqual(self.release_count(), 1)
        self.assertIn("namespace reference cleanup unconfirmed", str(caught.exception))

    def test_cancellation_after_namespace_access_still_releases_both(self):
        def callback(message):
            if "IWbemServices obtained" in message:
                raise KeyboardInterrupt
        with self.assertRaisesRegex(probe.DcomProbeCancelled, "release confirmed"):
            self.run_probe(callback=callback)
        self.assertEqual(self.release_count(), 2)

    def test_release_failure_is_not_a_successful_namespace_probe(self):
        with self.assertRaisesRegex(RpcProbeError, "release could not be confirmed"):
            self.run_probe(release_error=TimeoutError())
        self.assertEqual(self.release_count(), 2)
        self.channels[wire.REM_UNKNOWN].call.assert_called_once()

    @patch.object(probe, "discover_version")
    def test_invalid_namespace_never_connects(self, discovery):
        with self.assertRaises(ValueError):
            probe.probe_dcom("192.0.2.10", "synthetic", "synthetic", namespace="//other/root")
        discovery.assert_not_called()

    @patch.object(probe.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(probe, "probe_dcom")
    def test_cli_namespace_option_and_truthful_success(self, diagnostic, prompt):
        diagnostic.return_value = wire.decode_activation(activation_fixture())
        output = io.StringIO()
        with redirect_stdout(output):
            result = probe.main(["192.0.2.10", "--username", "synthetic", "--namespace", "root/cimv2"])
        self.assertEqual(result, 0)
        self.assertEqual(diagnostic.call_args.kwargs["namespace"], "root\\cimv2")
        self.assertIn("WMI namespace access verified", output.getvalue())
        self.assertIn("Queries, commands, and persistent sessions have NOT been tested", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())

    @patch.object(probe.getpass, "getpass")
    def test_cli_rejects_invalid_namespace_before_password(self, prompt):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            probe.main(["192.0.2.10", "--username", "synthetic", "--namespace", "//other/root"])
        self.assertEqual(caught.exception.code, 2)
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
