import io
import socket
import struct
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from remoteops.remoting.rpc_probe import (
    MAX_FRAGMENT,
    RpcBindRejected,
    RpcProbeError,
    RpcProtocolError,
    build_bind,
    main,
    parse_bind_response,
    probe_rpc,
)

# Independent wire fixtures: endpoint mapper v3 / NDR v2, C706 12.6.4.3-4.
BIND_PACKET = bytes.fromhex(
    "05000b03 10000000 48000000 01000000"
    "00100010 00000000 01000000 00000100"
    "0883afe1 1f5dc911 91a408002b14a0fa 03000000"
    "045d888a eb1cc911 9fe808002b104860 02000000"
)
ACK_PACKET = bytes.fromhex(
    "05000c03 10000000 3c000000 01000000"
    "d016d016 78563412 0400 31333500 0000"
    "01000000 00000000 045d888a eb1cc911 9fe808002b104860 02000000"
)
BIG_ENDIAN_ACK = bytes.fromhex(
    "05000c03 00000000 003c0000 00000001"
    "16d016d0 12345678 0004 31333500 0000"
    "01000000 00000000 8a885d04 1ceb11c9 9fe808002b104860 00000002"
)


def changed(packet, offset, value):
    return packet[:offset] + value + packet[offset + len(value):]


def response(packet_type, body):
    return struct.pack("<BBBB4sHHI", 5, 0, packet_type, 3, b"\x10\0\0\0",
                       16 + len(body), 0, 1) + body


class RpcWireTests(unittest.TestCase):
    def test_bind_matches_independent_wire_fixture(self):
        self.assertEqual(build_bind(), BIND_PACKET)

    def test_ack_accepts_both_byte_orders(self):
        for packet in (ACK_PACKET, BIG_ENDIAN_ACK):
            with self.subTest(packet=packet):
                ack = parse_bind_response(packet)
                self.assertEqual(ack.association_group, 0x12345678)
                self.assertEqual(ack.secondary_address, "135")
                self.assertEqual(ack.max_transmit_fragment, 5840)
                self.assertEqual(ack.max_receive_fragment, 5840)

    def test_all_truncation_boundaries_fail_cleanly(self):
        for length in range(len(ACK_PACKET)):
            with self.subTest(length=length):
                packet = ACK_PACKET[:length]
                if length >= 16:
                    packet = changed(packet, 8, struct.pack("<H", length))
                with self.assertRaises(RpcProtocolError):
                    parse_bind_response(packet)

    def test_invalid_headers_and_results_are_rejected(self):
        mutations = {
            "version": (0, b"\x04"), "minor": (1, b"\x01"),
            "packet type": (2, b"\x02"), "fragmentation": (3, b"\x01"),
            "encoding": (4, b"\x11"), "representation": (5, b"\x01"),
            "short fragment": (8, struct.pack("<H", 15)),
            "huge fragment": (8, struct.pack("<H", MAX_FRAGMENT + 1)),
            "authentication": (10, b"\x01\0"), "call ID": (12, b"\x02\0\0\0"),
            "fragment size": (16, b"\0\0"), "address bounds": (24, b"\xff\xff"),
            "address termination": (29, b"x"), "address control char": (26, b"\n"),
            "context count": (32, b"\x02"), "unknown result": (36, b"\x05\0"),
            "syntax UUID": (40, b"\xff"), "syntax version": (56, b"\x03\0\0\0"),
        }
        for name, (offset, value) in mutations.items():
            with self.subTest(name=name), self.assertRaises(RpcProtocolError):
                parse_bind_response(changed(ACK_PACKET, offset, value))
        with self.assertRaises(RpcProtocolError):
            parse_bind_response(ACK_PACKET + b"\0")
        with self.assertRaises(RpcProtocolError):
            parse_bind_response(changed(ACK_PACKET + b"\0", 8, struct.pack("<H", 61)))

    def test_empty_secondary_address_and_unspecified_padding(self):
        body = ACK_PACKET[16:24] + b"\0\0\xfe\xff" + ACK_PACKET[32:]
        self.assertEqual(parse_bind_response(response(12, body)).secondary_address, "")

    def test_bind_nak_and_optional_extension(self):
        for extension in (b"", bytes(32)):
            with self.subTest(extension=extension), self.assertRaisesRegex(
                RpcBindRejected, "authentication type not recognized.*code 8"
            ):
                parse_bind_response(response(13, b"\x08\0\x01\x05\0" + extension))
        with self.assertRaises(RpcProtocolError):
            parse_bind_response(response(13, b"\x08\0\x02\x05\0"))

    def test_context_rejection_reports_numeric_reason(self):
        for result in (1, 2):
            packet = changed(ACK_PACKET, 36, struct.pack("<HH", result, 1))
            with self.assertRaisesRegex(RpcBindRejected, "abstract syntax not supported"):
                parse_bind_response(packet)

    def test_fault_reports_full_status(self):
        body = struct.pack("<IHBBII", 0, 0, 0, 0, 0x1C010003, 0)
        with self.assertRaisesRegex(RpcBindRejected, "0x1c010003"):
            parse_bind_response(response(3, body))
        with self.assertRaises(RpcProtocolError):
            parse_bind_response(response(3, body[:-1]))


class RpcTransportTests(unittest.TestCase):
    def fake_socket(self, factory, chunks=()):
        sock = factory.return_value.__enter__.return_value
        sock.recv.side_effect = chunks
        return sock

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_partial_reads_send_only_one_bind_and_close(self, factory):
        sock = self.fake_socket(factory, [bytes([b]) for b in ACK_PACKET])
        ack = probe_rpc("192.0.2.1")
        self.assertEqual(ack.secondary_address, "135")
        sock.connect.assert_called_once_with(("192.0.2.1", 135))
        sock.sendall.assert_called_once_with(BIND_PACKET)
        self.assertEqual(sock.recv.call_count, len(ACK_PACKET))
        factory.return_value.__exit__.assert_called_once()

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_ipv6_uses_numeric_address_without_dns(self, factory):
        sock = self.fake_socket(factory, [ACK_PACKET[:16], ACK_PACKET[16:]])
        probe_rpc("::1")
        factory.assert_called_once_with(socket.AF_INET6, socket.SOCK_STREAM)
        sock.connect.assert_called_once_with(("::1", 135, 0, 0))

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_bad_input_never_opens_a_socket(self, factory):
        inputs = [
            {"address": "server.example"}, {"address": "fe80::1%3"},
            {"port": 0}, {"port": 65536}, {"port": True}, {"timeout": 0},
            {"timeout": -1}, {"timeout": float("nan")}, {"timeout": float("inf")},
            {"timeout": 301},
        ]
        for values in inputs:
            with self.subTest(values=values), self.assertRaises(ValueError):
                probe_rpc(**({"address": "192.0.2.1"} | values))
        factory.assert_not_called()

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_header_size_is_checked_before_body_read(self, factory):
        sock = self.fake_socket(factory, [changed(ACK_PACKET[:16], 8, b"\xff\xff")])
        with self.assertRaisesRegex(RpcProtocolError, "fragment length"):
            probe_rpc("192.0.2.1")
        sock.recv.assert_called_once_with(16)
        factory.return_value.__exit__.assert_called_once()

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_eof_in_header_or_body_closes_socket(self, factory):
        for chunks in ([b""], [ACK_PACKET[:16], b"abc", b""]):
            factory.reset_mock()
            self.fake_socket(factory, chunks)
            with self.subTest(chunks=chunks), self.assertRaisesRegex(
                RpcProtocolError, "Server closed"
            ):
                probe_rpc("192.0.2.1")
            factory.return_value.__exit__.assert_called_once()

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_network_failures_close_socket_and_name_stage(self, factory):
        failures = (
            ("connect", ConnectionRefusedError(), "Connection refused"),
            ("connect", TimeoutError(), "timed out while opening"),
            ("sendall", TimeoutError(), "timed out while sending"),
            ("recv", TimeoutError(), "timed out while receiving"),
            ("recv", ConnectionResetError("reset"), "Network error while receiving"),
        )
        for method, error, message in failures:
            factory.reset_mock(return_value=True)
            sock = self.fake_socket(factory)
            getattr(sock, method).side_effect = error
            with self.subTest(method=method), self.assertRaisesRegex(RpcProbeError, message):
                probe_rpc("192.0.2.1")
            factory.return_value.__exit__.assert_called_once()

    @patch("remoteops.remoting.rpc_probe.time.monotonic")
    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_slow_partial_reads_share_one_deadline(self, factory, clock):
        sock = self.fake_socket(factory, [b"\x05", b"\0"])
        clock.side_effect = [100, 100, 101, 102, 104, 105.1]
        with self.assertRaisesRegex(RpcProbeError, "timed out.*budget 5s"):
            probe_rpc("192.0.2.1", timeout=5)
        self.assertEqual(sock.recv.call_count, 2)
        self.assertEqual([c.args[0] for c in sock.settimeout.call_args_list], [5, 4, 3, 1])
        factory.return_value.__exit__.assert_called_once()

    @patch("remoteops.remoting.rpc_probe.socket.socket")
    def test_server_rejection_and_interrupt_close_socket(self, factory):
        nak = response(13, b"\x02\0\x01\x05\0")
        self.fake_socket(factory, [nak[:16], nak[16:]])
        with self.assertRaises(RpcBindRejected):
            probe_rpc("192.0.2.1")
        factory.return_value.__exit__.assert_called_once()
        factory.reset_mock(return_value=True)
        self.fake_socket(factory).recv.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            probe_rpc("192.0.2.1")
        factory.return_value.__exit__.assert_called_once()

    def test_real_loopback_exchange_closes_without_sending_a_request(self):
        errors, received, after_bind = [], [], []
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(3)

            def serve():
                try:
                    with listener.accept()[0] as conn:
                        conn.settimeout(3)
                        data = bytearray()
                        while len(data) < len(BIND_PACKET):
                            chunk = conn.recv(len(BIND_PACKET) - len(data))
                            if not chunk:
                                raise AssertionError("Client closed before sending bind")
                            data.extend(chunk)
                        received.append(bytes(data))
                        for i in range(0, len(ACK_PACKET), 3):
                            conn.sendall(ACK_PACKET[i:i + 3])
                        after_bind.append(conn.recv(1))
                except Exception as exc:
                    errors.append(exc)

            worker = threading.Thread(target=serve)
            worker.start()
            try:
                probe_rpc("127.0.0.1", listener.getsockname()[1], timeout=2)
            finally:
                worker.join(4)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(received, [BIND_PACKET])
        self.assertEqual(after_bind, [b""])


class RpcCliTests(unittest.TestCase):
    @patch("remoteops.remoting.rpc_probe.probe_rpc")
    def test_success_is_not_reported_as_wmi_connection(self, probe):
        probe.return_value = parse_bind_response(ACK_PACKET)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(["192.0.2.1", "--timeout", "2"]), 0)
        probe.assert_called_once_with("192.0.2.1", 135, 2)
        self.assertIn("RPC handshake verified", output.getvalue())
        self.assertIn("WMI access and authentication have NOT been tested", output.getvalue())

    @patch("remoteops.remoting.rpc_probe.probe_rpc")
    def test_error_exit_codes_and_no_traceback(self, probe):
        for error, code, message in (
            (RpcProbeError("full error details"), 1, "full error details"),
            (KeyboardInterrupt(), 130, "cancelled"),
        ):
            probe.side_effect = error
            output = io.StringIO()
            with redirect_stderr(output):
                self.assertEqual(main(["192.0.2.1"]), code)
            self.assertIn(message, output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
