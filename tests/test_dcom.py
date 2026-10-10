import io
import struct
import time
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from uuid import UUID

from remoteops.remoting import dcom_probe as probe
from remoteops.remoting import dcom_wire as wire
from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError

CID = UUID("00112233-4455-6677-8899-aabbccddeeff")
IPID = UUID("12345678-1234-5678-90ab-cdef12345678")
REMOTE_UNKNOWN = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
OXID = 0x12345678


def aligned(data, alignment=4):
    return data + bytes(-len(data) % alignment)


def binding_fixture(address="fixture[50000]", protocol=7, service=10, conformant=True):
    encoded = address.encode("utf-16-le")
    string_units = struct.unpack("<" + "H" * (len(encoded) // 2), encoded)
    strings = [protocol, *string_units, 0, 0]
    security = [service, 0xFFFF, 0, 0]
    units = strings + security
    result = struct.pack("<HH", len(units), len(strings)) + struct.pack("<" + "H" * len(units), *units)
    return (struct.pack("<I", len(units)) if conformant else b"") + result


def objref_fixture(iid=wire.WBEM_LOGIN_INTERFACE, oxid=OXID, kind=1, public_refs=5):
    return (struct.pack("<II", 0x574F454D, kind) + iid.bytes_le +
            struct.pack("<IIQQ", 0, public_refs, oxid, 123) + IPID.bytes_le +
            binding_fixture("fixture", conformant=False))


def activation_fixture(overall_status=0, interface_status=0, rpc_status=0, objref=None,
                       address="fixture[50000]", auth_service=10):
    objref = objref_fixture() if objref is None else objref
    prefix = bytes(8) + struct.pack("<QI", OXID, 0x20000)
    prefix += binding_fixture(address, service=auth_service)
    prefix = aligned(prefix) + REMOTE_UNKNOWN.bytes_le
    prefix += struct.pack("<IHHI", 6, 5, 7, overall_status)
    interface = struct.pack("<IIII", 1, 0x20004, len(objref), len(objref)) + objref
    return aligned(prefix + interface) + struct.pack("<III", 1, interface_status, rpc_status)


class DcomWireTests(unittest.TestCase):
    def test_activation_request_matches_fixed_wire_layout(self):
        expected = bytes.fromhex(
            "05000700 00000000 00000000 33221100554477668899aabbccddeeff 00000000"
            "5ef0c38b6bd8d011a07500c04fb68820"
            "00000000 00000000 02000000 00000000 01000000 00000200 01000000"
            "18ad09f36ad8d011a07500c04fb68820 01000000 01000000 0700"
        )
        self.assertEqual(wire.activation_request((5, 7), CID), expected)

    def test_activation_response_decodes_identity_and_binding(self):
        result = wire.decode_activation(activation_fixture())
        self.assertEqual(result.oxid, OXID)
        self.assertEqual(result.reference.iid, wire.WBEM_LOGIN_INTERFACE)
        self.assertEqual(result.reference.public_refs, 5)
        self.assertEqual(result.reference.ipid, IPID)
        self.assertEqual(result.remote_unknown, REMOTE_UNKNOWN)
        self.assertEqual(result.bindings.tcp_port(), 50000)
        result.bindings.require_ntlm()

    def test_release_request_exact_interface_and_reference_count(self):
        activation = wire.decode_activation(activation_fixture())
        expected = bytes.fromhex(
            "05000700 00000000 00000000 33221100554477668899aabbccddeeff 00000000"
            "01000000 01000000 785634123412785690abcdef12345678 05000000 00000000"
        )
        self.assertEqual(wire.release_request(activation, CID), expected)
        wire.decode_release(bytes(12))

    def test_server_alive2_version_and_security(self):
        packet = struct.pack("<HHI", 5, 9, 0x20000) + binding_fixture("fixture")
        packet = aligned(packet) + bytes(8)
        self.assertEqual(wire.decode_server_alive2(packet), (5, 7))
        with self.assertRaisesRegex(RpcProtocolError, "Unsupported DCOM version"):
            wire.decode_server_alive2(struct.pack("<HH", 6, 0) + packet[4:])

    def test_binding_lists_and_empty_packet_version(self):
        reader = wire.Reader(bytes.fromhex("040002000000000000000000"))
        result = wire.read_bindings(reader, conformant=False)
        reader.finish()
        self.assertEqual(result.strings, ())
        with self.assertRaises(RpcProtocolError):
            result.tcp_port()
        with self.assertRaises(RpcProtocolError):
            result.require_ntlm()

    def test_binding_bounds_conformance_and_terminators(self):
        fixture = binding_fixture()
        changes = ((0, b"\xff" * 4), (4, b"\xff\xff"), (6, b"\xff\xff"),
                   (len(fixture) - 2, b"\x01\0"))
        for offset, replacement in changes:
            packet = fixture[:offset] + replacement + fixture[offset + len(replacement):]
            with self.subTest(offset=offset), self.assertRaises(RpcProtocolError):
                wire.read_bindings(wire.Reader(packet), conformant=True)

    def test_no_named_pipe_or_unusable_port_fallback(self):
        for address in ("fixture", "fixture[0]", "fixture[65536]", "fixture[abc]"):
            result = wire.decode_activation(activation_fixture(address=address))
            with self.subTest(address=address), self.assertRaises(RpcProtocolError):
                result.bindings.tcp_port()
        result = wire.read_bindings(wire.Reader(binding_fixture(protocol=15)), conformant=True)
        with self.assertRaises(RpcProtocolError):
            result.tcp_port()
        result = wire.read_bindings(wire.Reader(binding_fixture(service=16)), conformant=True)
        with self.assertRaises(RpcProtocolError):
            result.require_ntlm()

    def test_malformed_and_mismatched_object_references(self):
        fixtures = [objref_fixture(iid=CID), objref_fixture(kind=4),
                    objref_fixture(public_refs=0), objref_fixture() + b"extra"]
        for fixture in fixtures:
            with self.subTest(fixture=fixture[:24]), self.assertRaises(RpcProtocolError):
                wire.decode_objref(fixture)
        with self.assertRaisesRegex(RpcProtocolError, "OXIDs disagree"):
            wire.decode_activation(activation_fixture(objref=objref_fixture(oxid=456)))

    def test_all_activation_and_release_truncations_fail_cleanly(self):
        for fixture, decoder in ((activation_fixture(), wire.decode_activation),
                                 (bytes(12), wire.decode_release),
                                 (objref_fixture(), wire.decode_objref)):
            for size in range(len(fixture)):
                with self.subTest(decoder=decoder.__name__, size=size):
                    with self.assertRaises(RpcProtocolError):
                        decoder(fixture[:size])

    def test_full_failure_status_codes_are_preserved(self):
        for fields in ({"overall_status": 0x80070005}, {"interface_status": 0x80004002},
                       {"rpc_status": 1722}):
            with self.subTest(fields=fields), self.assertRaises(wire.DcomStatusError) as caught:
                wire.decode_activation(activation_fixture(**fields))
            self.assertIn(f"0x{next(iter(fields.values())):08x}", str(caught.exception))
        with self.assertRaisesRegex(wire.DcomStatusError, "0x80070005"):
            wire.decode_release(bytes(8) + struct.pack("<I", 0x80070005))

    def test_oversized_and_extended_orpc_data_is_rejected(self):
        with self.assertRaises(RpcProtocolError):
            wire.Reader(bytes(4097))
        with self.assertRaises(RpcProtocolError):
            wire.decode_release(struct.pack("<III", 0, 0x20000, 0))
        with self.assertRaises(RpcProtocolError):
            wire.decode_release(bytes(16))

    def test_arbitrary_orpcthat_flags_are_ignored_without_shifting_fields(self):
        fixture = activation_fixture()
        expected = wire.decode_activation(fixture)
        for flags in (1, 2, 0x80000000, 0xFFFFFFFF):
            with self.subTest(flags=flags):
                reply = struct.pack("<I", flags) + fixture[4:]
                self.assertEqual(wire.decode_activation(reply), expected)
                wire.decode_release(struct.pack("<III", flags, 0, 0))
                with self.assertRaisesRegex(wire.DcomStatusError, "0x80070005"):
                    wire.decode_release(struct.pack("<III", flags, 0, 0x80070005))

    def test_nonzero_flags_do_not_hide_missing_or_unsupported_extensions(self):
        for size in range(4, 8):
            with self.subTest(size=size), self.assertRaisesRegex(RpcProtocolError, "Truncated"):
                wire.decode_release((struct.pack("<I", 1) + bytes(4))[:size])
        with self.assertRaisesRegex(
            RpcProtocolError, "Unsupported ORPCTHAT extensions.*flags=0x00000001.*0x00020000"
        ):
            wire.decode_release(struct.pack("<III", 1, 0x20000, 0))


class DcomWorkflowTests(unittest.TestCase):
    def run_probe(self, activation_reply=None, release_reply=bytes(12), callback=None,
                  release_failure=None):
        calls, closed = [], []

        @contextmanager
        def connect(address, username, password, interface, port, deadline):
            calls.append((address, interface, port))
            channel = Mock()
            if interface == wire.ACTIVATION:
                channel.call.return_value = (
                    activation_fixture() if activation_reply is None else activation_reply
                )
            else:
                channel.call.return_value = release_reply
                channel.call.side_effect = release_failure
            try:
                yield channel
            finally:
                closed.append(interface)
                calls.append(channel.call.call_args)

        self.calls, self.closed = calls, closed
        with patch.object(probe, "discover_version", return_value=(5, 7)):
            with patch.object(rpc, "ntlm_connection", side_effect=connect):
                return probe.probe_dcom("192.0.2.10", "synthetic-user", "synthetic-password",
                                         progress=callback)

    def test_activation_and_release_only_on_authorized_host(self):
        result = self.run_probe(activation_reply=activation_fixture(address="other-host[50000]"))
        self.assertEqual(result.reference.ipid, IPID)
        self.assertEqual(self.calls[0], ("192.0.2.10", wire.ACTIVATION, 135))
        self.assertEqual(self.calls[2], ("192.0.2.10", wire.REM_UNKNOWN, 50000))
        self.assertEqual(self.calls[1].args[0], 0)
        self.assertEqual(self.calls[3].args[0], 5)
        self.assertEqual(self.calls[3].args[3], REMOTE_UNKNOWN)
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])

    def test_nonzero_response_flags_do_not_prevent_reference_cleanup(self):
        reply = struct.pack("<I", 0xFFFFFFFF) + activation_fixture()[4:]
        self.run_probe(activation_reply=reply, release_reply=struct.pack("<III", 1, 0, 0))
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])
        self.assertEqual(self.calls[3].args[0], 5)

    def test_release_failure_is_not_success_or_retried(self):
        with self.assertRaisesRegex(RpcProbeError, "release could not be confirmed"):
            self.run_probe(release_failure=TimeoutError("synthetic timeout"))
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])

    def test_activation_error_does_not_dispatch_release(self):
        with self.assertRaisesRegex(RpcProbeError, "0x80070005"):
            self.run_probe(activation_reply=activation_fixture(overall_status=0x80070005))
        self.assertEqual(self.closed, [wire.ACTIVATION])

    def test_malformed_activation_reports_unknown_cleanup(self):
        with self.assertRaisesRegex(RpcProbeError, "cleanup unconfirmed"):
            self.run_probe(activation_reply=b"malformed")
        self.assertEqual(self.closed, [wire.ACTIVATION])

    def test_cancel_after_activation_still_releases_reference(self):
        def callback(message):
            if message.startswith("WMI login interface obtained"):
                raise KeyboardInterrupt
        with self.assertRaisesRegex(probe.DcomProbeCancelled, "release confirmed"):
            self.run_probe(callback=callback)
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])

    def test_bad_bindings_report_cleanup_unconfirmed(self):
        with self.assertRaisesRegex(RpcProbeError, "no supported TCP endpoint"):
            self.run_probe(activation_reply=activation_fixture(address="fixture"))
        self.assertEqual(self.closed, [wire.ACTIVATION])

    def test_progress_failure_during_cleanup_does_not_skip_release(self):
        for prefix in ("Connecting to the same host", "Releasing the WMI"):
            def callback(message):
                if message.startswith(prefix):
                    raise OSError("synthetic output sink failure")
            with self.subTest(prefix=prefix):
                with self.assertRaisesRegex(RpcProbeError, "release confirmed"):
                    self.run_probe(callback=callback)
                self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])
                self.assertEqual(self.calls[3].args[0], 5)

    def test_cancel_during_cleanup_still_releases_reference(self):
        def callback(message):
            if message.startswith("Releasing the WMI"):
                raise KeyboardInterrupt
        with self.assertRaisesRegex(probe.DcomProbeCancelled, "release confirmed"):
            self.run_probe(callback=callback)
        self.assertEqual(self.closed, [wire.ACTIVATION, wire.REM_UNKNOWN])

    @patch.object(probe, "discover_version", side_effect=TimeoutError())
    @patch.object(rpc, "ntlm_connection")
    def test_discovery_failure_never_authenticates(self, connect, discovery):
        with self.assertRaisesRegex(RpcProbeError, "discovering DCOM version"):
            probe.probe_dcom("192.0.2.10", "synthetic-user", "synthetic-password")
        connect.assert_not_called()

    @patch.object(probe.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(probe, "probe_dcom")
    def test_cli_truthful_scope_and_hidden_password(self, diagnostic, prompt):
        diagnostic.return_value = wire.decode_activation(activation_fixture())
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(probe.main(["192.0.2.10", "--username", "synthetic"]), 0)
        self.assertIn("have NOT been tested", stdout.getvalue())
        self.assertNotIn("synthetic-secret", stdout.getvalue())

    @patch.object(probe.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(probe, "probe_dcom", side_effect=probe.DcomProbeCancelled("cancelled"))
    def test_cli_cancellation_exit_code(self, diagnostic, prompt):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main(["192.0.2.10", "--username", "synthetic"]), 130)

    def test_status_has_utc_timestamp_and_preserves_full_message(self):
        output = io.StringIO()
        with patch.object(probe, "datetime") as clock:
            clock.now.return_value.isoformat.return_value = "2026-09-22T00:23:41+00:00"
            probe._print_status("complete diagnostic message", file=output)
            clock.now.assert_called_once_with(probe.timezone.utc)
        self.assertEqual(
            output.getvalue(),
            "[2026-09-22T00:23:41+00:00] complete diagnostic message\n",
        )

    @patch.object(probe.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(probe, "probe_dcom", side_effect=RpcProbeError("access denied 0x00000005"))
    def test_cli_failure_is_timestamped_without_password(self, diagnostic, prompt):
        output = io.StringIO()
        with redirect_stderr(output):
            self.assertEqual(probe.main(["192.0.2.10", "--username", "synthetic"]), 1)
        self.assertRegex(output.getvalue(), r"\[\d{4}-\d{2}-\d{2}T.*\+00:00\] access denied 0x00000005")
        self.assertNotIn("synthetic-secret", output.getvalue())


class SharedRpcConnectionTests(unittest.TestCase):
    @patch.object(rpc, "_read_packet", return_value=b"\x05\x00\x02\x03")
    @patch.object(rpc, "_verify_response", return_value=b"stub")
    @patch.object(rpc, "_request_packet", return_value=b"request")
    def test_reuses_context_and_increments_call_ids(self, request, verify, read):
        sock, context = Mock(), Mock()
        channel = rpc.NtlmRpcConnection(sock, context, b"binding", 4096)
        self.assertFalse(channel.peer_verified)
        deadline = time.monotonic() + 10
        self.assertEqual(channel.call(0, b"first", deadline), b"stub")
        self.assertEqual(channel.call(5, b"second", deadline, IPID), b"stub")
        self.assertTrue(channel.peer_verified)
        self.assertEqual([call.args[4] for call in request.call_args_list], [2, 3])
        self.assertIs(request.call_args_list[0].args[0], context)
        self.assertIs(request.call_args_list[1].args[0], context)
        self.assertEqual(request.call_args_list[1].args[5], IPID)

    @patch.object(rpc, "_read_packet", side_effect=TimeoutError())
    @patch.object(rpc, "_request_packet", return_value=b"request")
    def test_failed_connection_cannot_be_reused(self, request, read):
        sock = Mock()
        channel = rpc.NtlmRpcConnection(sock, Mock(), b"binding", 4096)
        with self.assertRaises(TimeoutError):
            channel.call(0, b"", time.monotonic() + 10)
        with self.assertRaisesRegex(RpcProbeError, "unusable"):
            channel.call(0, b"", time.monotonic() + 10)
        sock.close.assert_called_once()
        sock.sendall.assert_called_once()
        self.assertFalse(channel.peer_verified)


if __name__ == "__main__":
    unittest.main()
