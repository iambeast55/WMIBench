"""Milestone 1: unauthenticated endpoint-mapper bind, not a WMI connection.

Wire layouts follow C706 section 12.6 and Microsoft's MS-RPCE extensions.
This intentionally sends no RPC requests and is independent of the GUI/session code.
"""
from __future__ import annotations

import argparse
import ipaddress
import math
import socket
import struct
import sys
import time
from dataclasses import dataclass
from uuid import UUID

HEADER_SIZE = 16
MAX_FRAGMENT = 4096
CALL_ID = 1
BIND = 11
BIND_ACK = 12
BIND_NAK = 13
FAULT = 3
ENDPOINT_MAPPER = UUID("e1af8308-5d1f-11c9-91a4-08002b14a0fa")
NDR = UUID("8a885d04-1ceb-11c9-9fe8-08002b104860")


class RpcProbeError(Exception):
    """An unsuccessful probe; never indicates anything about credential validity."""


class RpcProtocolError(RpcProbeError):
    """Malformed or unsupported wire response."""


class RpcBindRejected(RpcProbeError):
    """The RPC server explicitly rejected the bind."""


@dataclass(frozen=True)
class RpcHeader:
    packet_type: int
    byte_order: str
    fragment_length: int


@dataclass(frozen=True)
class RpcBindAck:
    association_group: int
    max_transmit_fragment: int
    max_receive_fragment: int
    secondary_address: str


def build_bind(interface: UUID = ENDPOINT_MAPPER, major: int = 3, minor: int = 0) -> bytes:
    """Offer one NDR v2 context; default to endpoint mapper v3.0."""
    body = struct.pack("<HHIB3x", MAX_FRAGMENT, MAX_FRAGMENT, 0, 1)
    body += struct.pack("<HBB", 0, 1, 0)
    body += interface.bytes_le + struct.pack("<HH", major, minor)
    body += NDR.bytes_le + struct.pack("<I", 2)
    header = struct.pack(
        "<BBBB4sHHI", 5, 0, BIND, 3, b"\x10\0\0\0", HEADER_SIZE + len(body), 0, CALL_ID
    )
    return header + body


def parse_header(data: bytes) -> RpcHeader:
    if len(data) != HEADER_SIZE:
        raise RpcProtocolError("RPC header must contain exactly 16 bytes")
    if data[:2] != b"\x05\x00":
        raise RpcProtocolError("Unsupported RPC version; expected 5.0")
    if data[4:8] not in (b"\x10\0\0\0", b"\0\0\0\0"):
        raise RpcProtocolError("Unsupported RPC data representation")
    byte_order = "<" if data[4] == 0x10 else ">"
    length, auth_length, call_id = struct.unpack_from(byte_order + "HHI", data, 8)
    if not HEADER_SIZE <= length <= MAX_FRAGMENT:
        raise RpcProtocolError(f"RPC fragment length {length} is outside 16..{MAX_FRAGMENT}")
    if call_id != CALL_ID:
        raise RpcProtocolError(f"RPC call ID mismatch: expected {CALL_ID}, received {call_id}")
    if data[3] & 3 != 3:
        raise RpcProtocolError("Fragmented bind responses are unsupported by this probe")
    if auth_length:
        raise RpcProtocolError("Unexpected authentication data in an unauthenticated bind response")
    if data[2] not in (BIND_ACK, BIND_NAK, FAULT):
        raise RpcProtocolError(f"Unexpected RPC packet type {data[2]} during bind")
    return RpcHeader(data[2], byte_order, length)


def parse_bind_response(packet: bytes) -> RpcBindAck:
    header = parse_header(packet[:HEADER_SIZE])
    if len(packet) != header.fragment_length:
        raise RpcProtocolError("RPC response length does not match its header")
    body = packet[HEADER_SIZE:]
    order = header.byte_order
    if header.packet_type == BIND_NAK:
        if len(body) < 3 or len(body) < 3 + 2 * body[2]:
            raise RpcProtocolError("Truncated RPC bind rejection")
        reason = struct.unpack_from(order + "H", body)[0]
        reasons = {
            0: "reason not specified", 1: "temporary congestion", 2: "local limit exceeded",
            3: "called address unknown", 4: "protocol version/protocol error",
            5: "default context unsupported", 6: "user data unreadable",
            7: "no presentation service available", 8: "authentication type not recognized",
            9: "invalid checksum",
        }
        # MS-RPCE permits an optional extended-error blob after the version list.
        raise RpcBindRejected(
            f"RPC bind rejected: {reasons.get(reason, 'unknown reason')} (code {reason})"
        )
    if header.packet_type == FAULT:
        if len(body) < 16:
            raise RpcProtocolError("Truncated RPC fault")
        status = struct.unpack_from(order + "I", body, 8)[0]
        raise RpcBindRejected(f"RPC server returned a fault during bind: 0x{status:08x}")
    if len(body) < 10:
        raise RpcProtocolError("Truncated RPC bind acknowledgement")
    max_xmit, max_recv, association, address_length = struct.unpack_from(order + "HHIH", body)
    address_end = 10 + address_length
    result_offset = (address_end + 3) & ~3
    if result_offset + 4 > len(body):
        raise RpcProtocolError("RPC secondary address exceeds the response length")
    address = body[10:address_end]
    if address and (address[-1] != 0 or any(c < 32 or c > 126 for c in address[:-1])):
        raise RpcProtocolError("RPC secondary address is not a terminated printable ASCII string")
    count = body[result_offset]
    if count != 1:
        raise RpcProtocolError(f"Expected one RPC presentation result, received {count}")
    result_offset += 4
    if result_offset + 24 != len(body):
        raise RpcProtocolError("Invalid RPC presentation result length")
    result, reason = struct.unpack_from(order + "HH", body, result_offset)
    if result in (1, 2):
        reasons = {
            0: "reason not specified", 1: "abstract syntax not supported",
            2: "transfer syntax not supported", 3: "local limit exceeded",
        }
        raise RpcBindRejected(
            f"RPC presentation context rejected: {reasons.get(reason, 'unknown reason')} "
            f"(result {result}, reason {reason})"
        )
    if result != 0:
        raise RpcProtocolError(f"Unexpected RPC presentation result {result}")
    syntax_bytes = body[result_offset + 4:result_offset + 20]
    syntax = UUID(bytes_le=syntax_bytes) if order == "<" else UUID(bytes=syntax_bytes)
    version = struct.unpack_from(order + "I", body, result_offset + 20)[0]
    if syntax != NDR or version != 2:
        raise RpcProtocolError("Server accepted an unoffered RPC transfer syntax; expected NDR v2")
    if min(max_xmit, max_recv) < HEADER_SIZE:
        raise RpcProtocolError("Server advertised an invalid RPC fragment size")
    return RpcBindAck(association, max_xmit, max_recv, address[:-1].decode("ascii"))


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("RPC probe deadline expired")
    return remaining


def _receive_exact(sock: socket.socket, size: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < size:
        sock.settimeout(_remaining(deadline))
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RpcProtocolError(
                f"Server closed the connection: received {len(data)} of {size} expected bytes"
            )
        data.extend(chunk)
    return bytes(data)


def probe_rpc(address: str, port: int = 135, timeout: float = 5.0) -> RpcBindAck:
    """Bind once and always close. No DNS, credentials, retries, or RPC calls."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError as exc:
        raise ValueError("Use a numeric IPv4 or IPv6 address; DNS is not part of this probe") from exc
    if getattr(ip, "scope_id", None):
        raise ValueError("Scoped IPv6 addresses are not supported by this initial probe")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port must be an integer between 1 and 65535")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Timeout must be finite and greater than 0, at most 300 seconds")
    family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
    target = (str(ip), port) if ip.version == 4 else (str(ip), port, 0, 0)
    deadline = time.monotonic() + timeout
    stage = "opening TCP connection"
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.connect(target)
            stage = "sending RPC bind"
            sock.settimeout(_remaining(deadline))
            sock.sendall(build_bind())
            stage = "receiving RPC bind response"
            raw_header = _receive_exact(sock, HEADER_SIZE, deadline)
            header = parse_header(raw_header)
            body = _receive_exact(sock, header.fragment_length - HEADER_SIZE, deadline)
            return parse_bind_response(raw_header + body)
    except TimeoutError as exc:
        raise RpcProbeError(f"RPC probe timed out while {stage} (budget {timeout:g}s)") from exc
    except ConnectionRefusedError as exc:
        raise RpcProbeError(f"Connection refused by {address}:{port} while {stage}") from exc
    except OSError as exc:
        raise RpcProbeError(f"Network error while {stage}: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify an unauthenticated RPC endpoint-mapper bind, then disconnect. "
        "This does not test WMI readiness or credentials."
    )
    parser.add_argument("address", help="Approved target's numeric IPv4 or IPv6 address")
    parser.add_argument("--port", type=int, default=135, help="RPC TCP port (default: 135)")
    parser.add_argument("--timeout", type=float, default=5, help="Total timeout in seconds (default: 5)")
    args = parser.parse_args(argv)
    try:
        result = probe_rpc(args.address, args.port, args.timeout)
    except ValueError as exc:
        parser.error(str(exc))
    except RpcProbeError as exc:
        print(f"RPC probe failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("RPC probe cancelled; connection closed.", file=sys.stderr)
        return 130
    print(f"RPC handshake verified: {args.address}:{args.port}")
    print(f"Endpoint mapper v3.0 accepted NDR v2.0; association {result.association_group}.")
    print("Connection closed. WMI access and authentication have NOT been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
