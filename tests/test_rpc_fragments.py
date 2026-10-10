import struct
import time
from unittest.mock import Mock, patch

from spnego.exceptions import SpnegoError
from spnego.iov import BufferType

from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting.rpc_probe import RpcProtocolError
from test_rpc_auth_probe import NtlmFixture


class RpcFragmentTests(NtlmFixture):
    def fragment(self, server, data, flags, call_id=2):
        padding = -len(data) % 4
        body = data + bytes(padding)
        prefix = rpc._header(2, 8 + len(body) + 24, 16, call_id, flags)
        prefix += struct.pack("<IHBB", 0xFFFFFFFF, 0, 0, 0)
        trailer = rpc._trailer(padding)
        wrapped = server.wrap_iov([
            (BufferType.sign_only, prefix), (BufferType.data, body),
            (BufferType.sign_only, trailer), BufferType.header,
        ])
        return prefix + wrapped.buffers[1].data + trailer + wrapped.buffers[3].data

    def socket_for(self, packets):
        stream = bytearray(b"".join(packets))
        sock = Mock()

        def receive(count):
            # Exercise TCP partial reads independently of RPC fragmentation.
            result = bytes(stream[:min(count, 17)])
            del stream[:len(result)]
            return result

        sock.recv.side_effect = receive
        return sock

    def test_assembles_verified_fragments_without_padding(self):
        client, server, _ = self.contexts()
        parts = [b"one", b"x" * 3001, b"last part"]
        packets = [self.fragment(server, data, flag) for data, flag in zip(parts, (1, 0, 2))]
        self.assertEqual(rpc._read_response(self.socket_for(packets), client,
                                            time.monotonic() + 10, 2), b"".join(parts))

    def test_fragment_sequence_and_call_id_rejected(self):
        for flags in ((2,), (0,), (1, 3), (1, 1)):
            client, server, _ = self.contexts()
            packets = [self.fragment(server, b"data", flag) for flag in flags]
            with self.subTest(flags=flags), self.assertRaises(RpcProtocolError):
                rpc._read_response(self.socket_for(packets), client, time.monotonic() + 10, 2)
        client, server, _ = self.contexts()
        packets = [self.fragment(server, b"first", 1), self.fragment(server, b"last", 2, 3)]
        with self.assertRaisesRegex(RpcProtocolError, "call ID mismatch"):
            rpc._read_response(self.socket_for(packets), client, time.monotonic() + 10, 2)

    def test_tampered_fragment_and_replay_fail_and_invalidate_channel(self):
        for mode in ("ciphertext", "header", "replay"):
            client, server, _ = self.contexts()
            first = self.fragment(server, b"first", 1)
            second = bytearray(self.fragment(server, b"last", 2))
            if mode == "replay":
                second = first
            else:
                second[24 if mode == "ciphertext" else 3] ^= 1
            sock = self.socket_for([first, bytes(second)])
            channel = rpc.NtlmRpcConnection(sock, client, rpc._health_bind(), 4096)
            with self.subTest(mode=mode), self.assertRaises(SpnegoError):
                channel.call(4, b"request", time.monotonic() + 10)
            self.assertFalse(channel.active)
            self.assertFalse(channel.peer_verified)
            sock.close.assert_called_once()

    def test_response_size_and_fragment_count_are_bounded(self):
        for constant, limit in (("MAX_RESPONSE", 7), ("MAX_RESPONSE_FRAGMENTS", 1)):
            client, server, _ = self.contexts()
            packets = [self.fragment(server, b"first", 1), self.fragment(server, b"last", 2)]
            with patch.object(rpc, constant, limit), self.assertRaisesRegex(
                RpcProtocolError, "limit"
            ):
                rpc._read_response(self.socket_for(packets), client, time.monotonic() + 10, 2)

    def test_missing_last_fragment_and_empty_nonfinal_fragment_fail(self):
        for data in (b"first", b""):
            client, server, _ = self.contexts()
            with self.assertRaises(rpc.RpcProbeError):
                rpc._read_response(self.socket_for([self.fragment(server, data, 1)]),
                                   client, time.monotonic() + 10, 2)

    def test_deadline_remains_shared_across_fragments(self):
        client, server, _ = self.contexts()
        packets = [self.fragment(server, b"first", 1), self.fragment(server, b"last", 2)]
        with patch.object(rpc, "_remaining", side_effect=TimeoutError()):
            channel = rpc.NtlmRpcConnection(self.socket_for(packets), client,
                                             rpc._health_bind(), 4096)
            with self.assertRaises(TimeoutError):
                channel.call(4, b"request", time.monotonic() + 10)
            self.assertFalse(channel.active)
