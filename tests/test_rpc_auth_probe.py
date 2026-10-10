import io
import os
import socket
import struct
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID

import spnego
from spnego.exceptions import BadMICError, SpnegoError
from spnego.iov import BufferType

from remoteops.remoting import rpc_auth_probe as auth
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError

ACK_BODY = bytes.fromhex(
    "d016d016 78563412 0400 31333500 0000"
    "01000000 00000000 045d888a eb1cc911 9fe808002b104860 02000000"
)
FIXTURE_USER = "TEST\\probe"
FIXTURE_PASSWORD = "test-only-not-a-real-credential"


def packet(kind, body, token, call_id=1, flags=7, auth_level=6):
    padding = -len(body) % 4
    trailer = struct.pack("<BBBBI", 10, auth_level, padding, 0, 1)
    payload = body + bytes(padding) + trailer + token
    return struct.pack("<BBBB4sHHI", 5, 0, kind, flags, b"\x10\0\0\0",
                       16 + len(payload), len(token), call_id) + payload


def split(packet_bytes):
    auth_size = struct.unpack_from("<H", packet_bytes, 10)[0]
    trailer_offset = len(packet_bytes) - auth_size - 8
    return (
        packet_bytes[16:trailer_offset], packet_bytes[trailer_offset:trailer_offset + 8],
        packet_bytes[-auth_size:],
    )


class NtlmFixture(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        credentials = Path(root.name) / "test-credentials"
        credentials.write_text(f"TEST:probe:{FIXTURE_PASSWORD}\n", encoding="utf-8")
        env = patch.dict(os.environ, {"NTLM_USER_FILE": str(credentials), "LM_COMPAT_LEVEL": "3"})
        env.start()
        self.addCleanup(env.stop)

    def contexts(self):
        client = auth._new_context(FIXTURE_USER, FIXTURE_PASSWORD, "192.0.2.1")
        server = spnego.server(protocol="ntlm", options=spnego.NegotiateOptions.use_ntlm)
        token1 = client.step()
        token2 = server.step(token1)
        token3 = client.step(token2)
        server.step(token3)
        self.assertTrue(server.complete)
        return client, server, token2

    def protected_reply(self, server, status=0):
        data = struct.pack("<I", status)
        call_header = struct.pack("<IHBB", len(data), 0, 0, 0)
        prefix = struct.pack("<BBBB4sHHI", 5, 0, 2, 3, b"\x10\0\0\0",
                             24 + len(data) + 24, 16, 2) + call_header
        trailer = struct.pack("<BBBBI", 10, 6, 0, 0, 1)
        wrapped = server.wrap_iov([
            (BufferType.sign_only, prefix), (BufferType.data, data),
            (BufferType.sign_only, trailer), BufferType.header,
        ])
        return prefix + wrapped.buffers[1].data + trailer + wrapped.buffers[3].data

    def check_request(self, server, request):
        self.assertEqual(request[2], 0)
        self.assertEqual(struct.unpack_from("<HH", request, 20), (0, 3))
        data, trailer, signature = split(request)
        result = server.unwrap_iov([
            (BufferType.sign_only, request[:24]), (BufferType.data, data[8:]),
            (BufferType.sign_only, trailer), (BufferType.header, signature),
        ])
        verification = result.buffers[1].data
        self.assertEqual(verification[:8], bytes.fromhex("8ae3137102f43671"))
        self.assertEqual(struct.unpack_from("<HHI", verification, 8), (0x8001, 4, 1))
        self.assertEqual(struct.unpack_from("<HH", verification, 16), (0xC002, 40))
        self.assertEqual(len(verification), 60)


class AuthWireTests(NtlmFixture):
    def test_nonempty_requests_and_object_header_are_sealed_and_signed(self):
        client, server, _ = self.contexts()
        interface = UUID("00000131-0000-0000-c000-000000000046")
        binding = auth.build_bind(interface, 0, 0)
        for call_id, object_id in ((2, None), (3, interface)):
            stub = b"synthetic non-aligned NDR data"
            request = auth._request_packet(client, binding, 5, stub, call_id, object_id)
            prefix_size = 40 if object_id else 24
            self.assertEqual(request[3], 0x83 if object_id else 3)
            self.assertEqual(struct.unpack_from("<I", request, 12)[0], call_id)
            self.assertEqual(struct.unpack_from("<HH", request, 20), (0, 5))
            if object_id:
                self.assertEqual(request[24:40], object_id.bytes_le)
            data, trailer, signature = split(request)
            decrypted = server.unwrap_iov([
                (BufferType.sign_only, request[:prefix_size]),
                (BufferType.data, data[prefix_size - 16:]),
                (BufferType.sign_only, trailer), (BufferType.header, signature),
            ])
            plaintext = decrypted.buffers[1].data
            self.assertTrue(decrypted.encrypted)
            self.assertEqual(plaintext[:len(stub)], stub)
            offset = (len(stub) + 3) & ~3
            self.assertEqual(plaintext[len(stub):offset], bytes(offset - len(stub)))
            self.assertEqual(plaintext[offset:offset + 8], bytes.fromhex("8ae3137102f43671"))
            self.assertEqual(plaintext[offset + 20:], binding[32:72])
            self.assertEqual(struct.unpack_from("<I", request, 16)[0], len(plaintext))

    def test_changed_orpc_object_id_invalidates_signature(self):
        client, server, _ = self.contexts()
        object_id = UUID("00000131-0000-0000-c000-000000000046")
        request = bytearray(auth._request_packet(
            client, auth.build_bind(object_id, 0, 0), 5, bytes(64), 2, object_id
        ))
        request[24] ^= 1
        data, trailer, signature = split(bytes(request))
        with self.assertRaises(SpnegoError):
            server.unwrap_iov([
                (BufferType.sign_only, bytes(request[:40])), (BufferType.data, data[24:]),
                (BufferType.sign_only, trailer), (BufferType.header, signature),
            ])

    def test_real_ntlm_sealing_and_verification(self):
        client, server, challenge = self.contexts()
        ack, decoded = auth._accept_challenge(packet(12, ACK_BODY, challenge))
        self.assertEqual(decoded, challenge)
        self.assertEqual(ack.association_group, 0x12345678)
        self.check_request(server, auth._inquiry_packet(client))
        result = auth._verify_inquiry(self.protected_reply(server), client)
        self.assertEqual(result.status, 0)

    def test_bad_password_is_rejected_by_real_ntlm_provider(self):
        client = auth._new_context(FIXTURE_USER, "wrong-test-password", "192.0.2.1")
        server = spnego.server(protocol="ntlm", options=spnego.NegotiateOptions.use_ntlm)
        challenge = server.step(client.step())
        with self.assertRaises(SpnegoError):
            server.step(client.step(challenge))
        self.assertFalse(server.complete)

    def test_changed_header_ciphertext_signature_and_replay_fail(self):
        for offset in (16, 24, -1):
            client, server, _ = self.contexts()
            reply = bytearray(self.protected_reply(server))
            reply[offset] ^= 1
            with self.subTest(offset=offset), self.assertRaises(SpnegoError):
                auth._verify_inquiry(bytes(reply), client)
        client, server, _ = self.contexts()
        reply = self.protected_reply(server)
        auth._verify_inquiry(reply, client)
        with self.assertRaises(SpnegoError):
            auth._verify_inquiry(reply, client)

    def test_security_downgrades_are_rejected(self):
        _, _, challenge = self.contexts()
        for changes in ({"flags": 3}, {"auth_level": 5}):
            with self.subTest(changes=changes), self.assertRaises(auth.RpcSecurityError):
                auth._accept_challenge(packet(12, ACK_BODY, challenge, **changes))
        for mask in (0x10, 0x20, 0x80000, 0x20000000):
            weakened = bytearray(challenge)
            flags = struct.unpack_from("<I", weakened, 20)[0]
            struct.pack_into("<I", weakened, 20, flags & ~mask)
            with self.subTest(mask=mask), self.assertRaises(auth.RpcSecurityError):
                auth._accept_challenge(packet(12, ACK_BODY, bytes(weakened)))

    def test_unprotected_fault_is_not_credential_confirmation(self):
        reply = bytes.fromhex("05000303 10000000 20000000 02000000")
        reply += struct.pack("<IHBBII", 0, 0, 0, 0, 5, 0)
        with self.assertRaisesRegex(auth.RpcSecurityError, "Unverified RPC fault 0x00000005"):
            auth._verify_inquiry(reply, None)

    def test_truncations_and_bad_security_fields_fail(self):
        client, server, _ = self.contexts()
        reply = self.protected_reply(server)
        for length in range(len(reply)):
            with self.subTest(length=length), self.assertRaises(RpcProbeError):
                auth._verify_inquiry(reply[:length], client)
        for offset in (2, 3, 4, 8, 10, 12, 28, 29, 30, 31, 32):
            altered = bytearray(reply)
            altered[offset] = 255
            with self.subTest(offset=offset), self.assertRaises(RpcProbeError):
                auth._verify_inquiry(bytes(altered), client)

    def test_nonzero_protected_method_status_is_not_success(self):
        client, server, _ = self.contexts()
        with self.assertRaisesRegex(RpcProbeError, "status 0x00000005"):
            auth._verify_inquiry(self.protected_reply(server, status=5), client)

    def test_ntlmv1_environment_is_refused(self):
        for value in ("0", "1", "2", "6", "invalid"):
            with patch.dict(os.environ, {"LM_COMPAT_LEVEL": value}):
                with self.assertRaises(auth.RpcSecurityError):
                    auth._new_context(FIXTURE_USER, FIXTURE_PASSWORD, "192.0.2.1")

    def test_auth3_wire_layout(self):
        result = auth._token_packet(16, bytes(4), b"test-token")
        self.assertEqual(result[2:4], b"\x10\x07")
        self.assertEqual(result[16:20], bytes(4))
        self.assertEqual(result[20:28], bytes.fromhex("0a06000001000000"))
        self.assertEqual(result[28:], b"test-token")
        with self.assertRaises(RpcProtocolError):
            auth._token_packet(16, bytes(4), bytes(4096))


class AuthTransportTests(NtlmFixture):
    def test_real_loopback_authenticated_exchange(self):
        errors, completed = [], []

        def receive(conn):
            data = bytearray()
            size = 16
            while len(data) < size:
                chunk = conn.recv(size - len(data))
                if not chunk:
                    raise AssertionError("Unexpected EOF")
                data.extend(chunk)
                if len(data) == 16:
                    size = struct.unpack_from("<H", data, 8)[0]
            return bytes(data)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)

            def serve():
                try:
                    with listener.accept()[0] as conn:
                        conn.settimeout(3)
                        server = spnego.server(
                            protocol="ntlm", options=spnego.NegotiateOptions.use_ntlm
                        )
                        bind = receive(conn)
                        self.assertEqual(bind[2], 11)
                        challenge = server.step(split(bind)[2])
                        conn.sendall(packet(12, ACK_BODY, challenge))
                        auth3 = receive(conn)
                        self.assertEqual(auth3[2], 16)
                        server.step(split(auth3)[2])
                        self.assertTrue(server.complete)
                        self.check_request(server, receive(conn))
                        reply = self.protected_reply(server)
                        for offset in range(0, len(reply), 3):
                            conn.sendall(reply[offset:offset + 3])
                        self.assertEqual(conn.recv(1), b"")
                        completed.append(True)
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                result = auth.probe_authenticated_rpc(
                    "127.0.0.1", FIXTURE_USER, FIXTURE_PASSWORD,
                    port=listener.getsockname()[1], timeout=2,
                )
                self.assertEqual(result.status, 0)
            finally:
                worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(completed, [True])

    @patch.object(auth.socket, "socket")
    def test_input_and_policy_failure_never_connect(self, factory):
        for values in ({"port": 0}, {"timeout": float("nan")}, {"username": ""},
                       {"password": ""}, {"address": "hostname"}):
            args = dict(address="192.0.2.1", username=FIXTURE_USER, password=FIXTURE_PASSWORD)
            args.update(values)
            with self.subTest(values=values), self.assertRaises(ValueError):
                auth.probe_authenticated_rpc(**args)
        with patch.dict(os.environ, {"LM_COMPAT_LEVEL": "1"}):
            with self.assertRaises(auth.RpcSecurityError):
                auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD)
        factory.assert_not_called()

    @patch.object(auth.socket, "socket")
    def test_timeout_and_rejection_close_without_retry(self, factory):
        for failure in (TimeoutError(), ConnectionResetError()):
            factory.reset_mock(return_value=True)
            sock = factory.return_value.__enter__.return_value
            sock.recv.side_effect = failure
            with self.assertRaises(RpcProbeError):
                auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD)
            sock.connect.assert_called_once()
            sock.sendall.assert_called_once()
            factory.return_value.__exit__.assert_called_once()

    @patch.object(auth.socket, "socket")
    @patch.object(auth, "_new_context")
    def test_provider_error_message_does_not_leak_secrets(self, new_context, factory):
        new_context.side_effect = BadMICError(context_msg=FIXTURE_PASSWORD)
        with self.assertRaisesRegex(auth.RpcSecurityError, "BadMICError") as caught:
            auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD)
        self.assertNotIn(FIXTURE_PASSWORD, str(caught.exception))
        factory.assert_not_called()

    @patch.object(auth.socket, "socket")
    def test_interrupt_closes_socket(self, factory):
        sock = factory.return_value.__enter__.return_value
        sock.recv.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD)
        factory.return_value.__exit__.assert_called_once()

    @patch.object(auth.time, "monotonic")
    @patch.object(auth.socket, "socket")
    @patch.object(auth, "_new_context")
    def test_partial_reads_share_total_deadline(self, new_context, factory, clock):
        new_context.return_value.step.return_value = b"synthetic-negotiate"
        clock.side_effect = [100, 100, 101, 102, 104, 105.1]
        sock = factory.return_value.__enter__.return_value
        sock.recv.side_effect = [b"\x05", b"\0"]
        with self.assertRaisesRegex(RpcProbeError, "timed out.*shared deadline expired"):
            auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD, timeout=5)
        self.assertEqual(sock.recv.call_count, 2)
        factory.return_value.__exit__.assert_called_once()

    @patch.object(auth.socket, "socket")
    @patch.object(auth, "_accept_challenge")
    @patch.object(auth, "_read_packet")
    @patch.object(auth, "_new_context")
    def test_local_context_completion_alone_is_not_success(
        self, new_context, read_packet, accept_challenge, factory
    ):
        context = new_context.return_value
        context.step.side_effect = [b"negotiate", b"authenticate"]
        context.complete = True
        context.context_attr = spnego.ContextReq.integrity | spnego.ContextReq.confidentiality
        context.wrap_iov.return_value.encrypted = True
        context.wrap_iov.return_value.buffers = (
            Mock(), Mock(data=bytes(60)), Mock(), Mock(data=bytes(16)),
        )
        accept_challenge.return_value = (Mock(max_receive_fragment=4096), b"challenge")
        read_packet.side_effect = [b"ack", TimeoutError()]
        with self.assertRaisesRegex(RpcProbeError, "verifying a protected RPC call"):
            auth.probe_authenticated_rpc("192.0.2.1", FIXTURE_USER, FIXTURE_PASSWORD)
        factory.return_value.__exit__.assert_called_once()


class AuthCliTests(unittest.TestCase):
    @patch.object(auth.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(auth, "probe_authenticated_rpc")
    def test_success_wording_and_password_not_printed(self, probe, prompt):
        probe.return_value = auth.AuthProbeResult(0)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            self.assertEqual(auth.main(["192.0.2.1", "--username", "TEST\\probe"]), 0)
        self.assertIn("WMI access has NOT been tested", stdout.getvalue())
        self.assertNotIn("synthetic-secret", stdout.getvalue())

    @patch.object(auth.getpass, "getpass", side_effect=auth.getpass.GetPassWarning)
    @patch.object(auth, "probe_authenticated_rpc")
    def test_no_echoing_password_fallback(self, probe, prompt):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            self.assertEqual(auth.main(["192.0.2.1", "--username", "TEST\\probe"]), 2)
        probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
