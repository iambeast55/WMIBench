"""Milestone 2a: NTLMv2 RPC privacy and a read-only ServerAlive health call.

Our own RPC framing, using pyspnego only for NTLM and message protection.
No WMI activation, endpoint enumeration, shell execution, retries, or GUI changes.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import math
import os
import socket
import struct
import sys
import time
import warnings
from contextlib import contextmanager
from dataclasses import dataclass
from uuid import UUID

import spnego
from spnego.exceptions import SpnegoError
from spnego.iov import BufferType

from remoteops.remoting.rpc_probe import (
    MAX_FRAGMENT,
    RpcProbeError,
    RpcProtocolError,
    _receive_exact,
    _remaining,
    build_bind,
    parse_bind_response,
)

AUTH_TYPE = 10  # RPC_C_AUTHN_WINNT
AUTH_LEVEL = 6  # RPC_C_AUTHN_LEVEL_PKT_PRIVACY
AUTH_CONTEXT = 1
HEADER_SIGN = 4
SIGNATURE_SIZE = 16
MAX_RESPONSE = 1024 * 1024
MAX_RESPONSE_FRAGMENTS = 512
NTLM_REQUIRED_FLAGS = 0x20080231  # Unicode, sign, seal, NTLM, extended security, 128-bit.
OBJECT_EXPORTER = UUID("99fcfec4-5260-101b-bbcb-00aa0021347a")


class RpcSecurityError(RpcProbeError):
    """Authentication or packet-protection requirements were not met."""


@dataclass(frozen=True)
class AuthProbeResult:
    status: int


def _health_bind() -> bytes:
    return build_bind(OBJECT_EXPORTER, 0, 0)


def _header(kind: int, body_size: int, auth_size: int, call_id: int, flags: int = 3) -> bytes:
    length = 16 + body_size
    if not 16 <= length <= MAX_FRAGMENT:
        raise RpcProtocolError("Authenticated RPC packet exceeds the probe size limit")
    return struct.pack("<BBBB4sHHI", 5, 0, kind, flags, b"\x10\0\0\0",
                       length, auth_size, call_id)


def _trailer(padding: int) -> bytes:
    return struct.pack("<BBBBI", AUTH_TYPE, AUTH_LEVEL, padding, 0, AUTH_CONTEXT)


def _token_packet(kind: int, body: bytes, token: bytes) -> bytes:
    if not token:
        raise RpcSecurityError("NTLM did not produce the required authentication token")
    padding = -len(body) % 4
    payload = body + bytes(padding) + _trailer(padding) + token
    return _header(kind, len(payload), len(token), 1, 3 | HEADER_SIGN) + payload


def _read_packet(sock: socket.socket, deadline: float, call_id: int,
                 *, fragmented: bool = False) -> bytes:
    header = _receive_exact(sock, 16, deadline)
    _validate_header(header, call_id, fragmented=fragmented)
    length = struct.unpack_from("<H", header, 8)[0]
    return header + _receive_exact(sock, length - 16, deadline)


def _validate_header(header: bytes, call_id: int, *, fragmented: bool = False) -> None:
    if len(header) != 16 or header[:2] != b"\x05\0":
        raise RpcProtocolError("Invalid RPC 5.0 header")
    if header[4:8] != b"\x10\0\0\0":
        raise RpcProtocolError("This authenticated probe requires little-endian RPC responses")
    if (not fragmented and header[3] & 3 != 3) or header[3] & 0xE8:
        raise RpcProtocolError("Fragmented or unsupported RPC response flags")
    length, auth_size, received_id = struct.unpack_from("<HHI", header, 8)
    if not 16 <= length <= MAX_FRAGMENT or auth_size > length - 16:
        raise RpcProtocolError("Invalid authenticated RPC fragment/authentication length")
    if received_id != call_id:
        raise RpcProtocolError("Authenticated RPC response call ID mismatch")


def _split_security(packet: bytes, prefix_size: int, call_id: int,
                    *, fragmented: bool = False):
    _validate_header(packet[:16], call_id, fragmented=fragmented)
    length, auth_size = struct.unpack_from("<HH", packet, 8)
    if len(packet) != length:
        raise RpcProtocolError("Authenticated RPC packet length mismatch")
    if not auth_size:
        if packet[2] == 3 and len(packet) >= 32:
            status = struct.unpack_from("<I", packet, 24)[0]
            detail = " (access denied: check credentials or server policy)" if status == 5 else ""
            raise RpcSecurityError(
                f"Unverified RPC fault 0x{status:08x}{detail}; authentication was not confirmed"
            )
        raise RpcSecurityError("Server omitted RPC packet protection; refusing unprotected data")
    trailer_offset = length - auth_size - 8
    if trailer_offset < prefix_size or trailer_offset % 4:
        raise RpcProtocolError("Invalid RPC security trailer position")
    trailer = packet[trailer_offset:trailer_offset + 8]
    auth_type, auth_level, padding, reserved, context = struct.unpack("<BBBBI", trailer)
    if (auth_type, auth_level, context) != (AUTH_TYPE, AUTH_LEVEL, AUTH_CONTEXT):
        raise RpcSecurityError(
            "RPC authentication type, privacy level, or security context changed"
        )
    if reserved or padding > trailer_offset - prefix_size:
        raise RpcProtocolError("Invalid RPC authentication padding or reserved field")
    return packet[prefix_size:trailer_offset], trailer, packet[-auth_size:], padding


def _accept_challenge(packet: bytes):
    if packet[2] == 13:
        # Reuse the bounded, unauthenticated bind-rejection parser from milestone 1.
        parse_bind_response(packet)
    if packet[2] not in (12, 3):
        raise RpcProtocolError("Expected RPC bind acknowledgement during NTLM negotiation")
    body, _, token, padding = _split_security(packet, 16, 1)
    if packet[2] != 12:
        raise RpcSecurityError("Server returned a fault before NTLM authentication completed")
    if not packet[3] & HEADER_SIGN:
        raise RpcSecurityError(
            "Server did not negotiate RPC header signing; no downgrade attempted"
        )
    if padding:
        body = body[:-padding]
    ack = parse_bind_response(_header(12, len(body), 0, 1) + body)
    if len(token) < 48 or token[:12] != b"NTLMSSP\0\x02\0\0\0":
        raise RpcSecurityError("Server did not supply a valid NTLM challenge")
    flags = struct.unpack_from("<I", token, 20)[0]
    if flags & NTLM_REQUIRED_FLAGS != NTLM_REQUIRED_FLAGS:
        raise RpcSecurityError(
            "NTLM challenge does not support required signing and 128-bit sealing"
        )
    return ack, token


def _request_packet(context, binding: bytes, opnum: int, stub: bytes,
                    call_id: int, object_id: UUID | None = None) -> bytes:
    # Signed verification trailer binds the unauthenticated presentation negotiation
    # to each protected request. ORPC also signs the object UUID in the RPC header.
    verification = bytes.fromhex("8ae3137102f43671")
    verification += struct.pack("<HHI", 0x8001, 4, 1)
    verification += struct.pack("<HH", 0xC002, 40) + binding[32:72]
    body = stub + bytes(-len(stub) % 4) + verification
    padding = -len(body) % 4
    data = body + bytes(padding)
    trailer = _trailer(padding)
    call_header = struct.pack("<IHH", len(body), 0, opnum)
    if object_id is not None:
        call_header += object_id.bytes_le
    prefix = _header(
        0, len(call_header) + len(data) + 8 + SIGNATURE_SIZE, SIGNATURE_SIZE,
        call_id, 0x83 if object_id is not None else 3,
    ) + call_header
    wrapped = context.wrap_iov([
        (BufferType.sign_only, prefix), (BufferType.data, data),
        (BufferType.sign_only, trailer), BufferType.header,
    ], encrypt=True)
    encrypted, signature = wrapped.buffers[1].data, wrapped.buffers[3].data
    if not wrapped.encrypted or encrypted is None or signature is None:
        raise RpcSecurityError("NTLM provider did not encrypt the RPC request")
    if len(encrypted) != len(data) or len(signature) != SIGNATURE_SIZE:
        raise RpcSecurityError("Unexpected NTLM protection sizes")
    return prefix + encrypted + trailer + signature


def _inquiry_packet(context) -> bytes:
    return _request_packet(context, _health_bind(), 3, b"", 2)


def _verify_response(packet: bytes, context, call_id: int,
                     *, fragmented: bool = False) -> bytes:
    if len(packet) < 24 or packet[2] not in (2, 3):
        raise RpcProtocolError("Expected an RPC inquiry response or fault")
    prefix_size = 32 if packet[2] == 3 else 24
    data, trailer, signature, padding = _split_security(
        packet, prefix_size, call_id, fragmented=fragmented
    )
    if len(signature) != SIGNATURE_SIZE:
        raise RpcSecurityError("Invalid NTLM response signature length")
    unwrapped = context.unwrap_iov([
        (BufferType.sign_only, packet[:prefix_size]), (BufferType.data, data),
        (BufferType.sign_only, trailer), (BufferType.header, signature),
    ])
    plaintext = unwrapped.buffers[1].data
    if not unwrapped.encrypted or plaintext is None:
        raise RpcSecurityError("NTLM provider did not verify an encrypted response")
    if padding:
        plaintext = plaintext[:-padding]
    if struct.unpack_from("<H", packet, 20)[0] != 0:
        raise RpcProtocolError("RPC response presentation context mismatch")
    if packet[2] == 3:
        status = struct.unpack_from("<I", packet, 24)[0]
        raise RpcSecurityError(f"Protected RPC call failed with fault 0x{status:08x}")
    return plaintext


def _read_response(sock, context, deadline: float, call_id: int) -> bytes:
    """Verify every fragment before assembling; never trust allocation hints."""
    result = bytearray()
    for index in range(MAX_RESPONSE_FRAGMENTS):
        packet = _read_packet(sock, deadline, call_id, fragmented=True)
        data = _verify_response(packet, context, call_id, fragmented=True)
        if bool(packet[3] & 1) != (index == 0):
            raise RpcProtocolError("Invalid RPC first-fragment sequence")
        if len(result) + len(data) > MAX_RESPONSE:
            raise RpcProtocolError("RPC response exceeds the assembled size limit")
        result.extend(data)
        if packet[3] & 2:
            return bytes(result)
        if not data:
            raise RpcProtocolError("Empty non-final RPC fragment")
    raise RpcProtocolError("RPC response exceeds the fragment count limit")


def _verify_inquiry(packet: bytes, context) -> AuthProbeResult:
    plaintext = _verify_response(packet, context, 2)
    if len(plaintext) != 4:
        raise RpcProtocolError("Expected one status in the ServerAlive reply")
    status = struct.unpack("<I", plaintext)[0]
    if status:
        raise RpcProbeError(f"Protected ServerAlive call returned status 0x{status:08x}")
    return AuthProbeResult(status)


def _new_context(username: str, password: str, address: str):
    try:
        compatibility = int(os.environ.get("LM_COMPAT_LEVEL", "3"))
    except ValueError as exc:
        raise RpcSecurityError("Invalid LM_COMPAT_LEVEL; NTLMv2 is required") from exc
    if not 3 <= compatibility <= 5:
        raise RpcSecurityError("LM_COMPAT_LEVEL must be 3..5; NTLMv1/LM are not allowed")
    return spnego.client(
        username=username, password=password, hostname=address, service="host", protocol="ntlm",
        context_req=spnego.ContextReq.integrity | spnego.ContextReq.confidentiality |
                    spnego.ContextReq.replay_detect | spnego.ContextReq.sequence_detect,
        options=spnego.NegotiateOptions.use_ntlm,
    )


def validate_target(address: str, username: str, password: str,
                    port: int, timeout: float):
    ip = ipaddress.ip_address(address)
    if getattr(ip, "scope_id", None):
        raise ValueError("Scoped IPv6 addresses are not supported")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("Port must be an integer between 1 and 65535")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("Timeout must be greater than zero and at most 300 seconds")
    if not username.strip() or not password:
        raise ValueError("An explicit username and nonempty password are required")
    return ip


class NtlmRpcConnection:
    """One serial RPC association. A locally complete NTLM context is not peer proof."""

    def __init__(self, sock, context, binding, max_receive_fragment):
        self.sock = sock
        self.context = context
        self.binding = binding
        self.max_receive_fragment = max_receive_fragment
        self.call_id = 1
        self.active = True
        self.peer_verified = False

    def call(self, opnum: int, stub: bytes, deadline: float,
             object_id: UUID | None = None) -> bytes:
        if not self.active:
            raise RpcProbeError("RPC connection is closed or unusable after an earlier failure")
        try:
            self.call_id += 1
            packet = _request_packet(
                self.context, self.binding, opnum, stub, self.call_id, object_id
            )
            if len(packet) > self.max_receive_fragment:
                raise RpcProtocolError("RPC request exceeds the server's fragment limit")
            self.sock.settimeout(_remaining(deadline))
            self.sock.sendall(packet)
            data = _read_response(self.sock, self.context, deadline, self.call_id)
            self.peer_verified = True
            return data
        except BaseException:
            # A failed/incomplete packet consumes NTLM sequence state. Never reuse it.
            self.active = False
            self.sock.close()
            raise


@contextmanager
def ntlm_connection(address: str, username: str, password: str,
                    interface: UUID, port: int, deadline: float):
    ip = validate_target(address, username, password, port, 300)
    stage = "creating NTLMv2 context"
    try:
        context = _new_context(username, password, str(ip))
        negotiate = context.step()
        binding = build_bind(interface, 0, 0)
        bind = _token_packet(11, binding[16:], negotiate)
        family = socket.AF_INET if ip.version == 4 else socket.AF_INET6
        target = (str(ip), port) if ip.version == 4 else (str(ip), port, 0, 0)
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            stage = "opening TCP connection"
            sock.settimeout(_remaining(deadline))
            sock.connect(target)
            stage = "exchanging NTLM bind/challenge"
            sock.settimeout(_remaining(deadline))
            sock.sendall(bind)
            ack, challenge = _accept_challenge(_read_packet(sock, deadline, 1))
            stage = "creating NTLM authenticate token"
            try:
                authenticate = context.step(challenge)
            except (ValueError, struct.error) as exc:
                raise RpcSecurityError("NTLM provider rejected a malformed challenge") from exc
            required = spnego.ContextReq.integrity | spnego.ContextReq.confidentiality
            if not context.complete or context.context_attr & required != required:
                raise RpcSecurityError("NTLM context did not establish required message protection")
            auth3 = _token_packet(16, bytes(4), authenticate)
            max_receive = min(MAX_FRAGMENT, ack.max_receive_fragment)
            if len(auth3) > max_receive:
                raise RpcProtocolError("Authenticated request exceeds the server's fragment limit")
            stage = "sending NTLM authenticate"
            sock.settimeout(_remaining(deadline))
            sock.sendall(auth3)
            stage = "making or verifying a protected RPC call"
            connection = NtlmRpcConnection(sock, context, binding, max_receive)
            try:
                yield connection
            finally:
                connection.active = False
                connection.context = None
    except SpnegoError as exc:
        # Provider messages/tokens can contain credential material. Expose only type/code.
        raise RpcSecurityError(
            f"NTLM protection failed while {stage}: {type(exc).__name__} "
            f"(code {exc.ERROR_CODE}); no authenticated success was confirmed"
        ) from None
    except TimeoutError as exc:
        raise RpcProbeError(
            f"Authenticated RPC timed out while {stage} (shared deadline expired)"
        ) from exc
    except OSError as exc:
        raise RpcProbeError(f"Network failure while {stage} (OS code {exc.errno})") from exc


def probe_authenticated_rpc(address: str, username: str, password: str,
                            port: int = 135, timeout: float = 10) -> AuthProbeResult:
    validate_target(address, username, password, port, timeout)
    deadline = time.monotonic() + timeout
    with ntlm_connection(address, username, password, OBJECT_EXPORTER, port, deadline) as connection:
        plaintext = connection.call(3, b"", deadline)
        if len(plaintext) != 4:
            raise RpcProtocolError("Expected one status in the ServerAlive reply")
        status = struct.unpack("<I", plaintext)[0]
        if status:
            raise RpcProbeError(f"Protected ServerAlive call returned status 0x{status:08x}")
        return AuthProbeResult(status)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="NTLMv2 RPC privacy probe: one read-only ServerAlive call, then close. "
                    "Does not test WMI permissions. Password is prompted, never a command argument."
    )
    parser.add_argument("address", help="Approved Windows target's numeric IP")
    parser.add_argument(
        "--username", required=True, help=r"Account, preferably COMPUTER\user or DOMAIN\user"
    )
    parser.add_argument("--port", type=int, default=135)
    parser.add_argument("--timeout", type=float, default=10)
    args = parser.parse_args(argv)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass("Windows password: ")
        try:
            result = probe_authenticated_rpc(args.address, args.username, password,
                                             args.port, args.timeout)
        finally:
            password = None
    except ValueError as exc:
        parser.error(str(exc))
    except (getpass.GetPassWarning, EOFError):
        print("A terminal with hidden password input is required.", file=sys.stderr)
        return 2
    except RpcProbeError as exc:
        print(f"Authenticated RPC probe failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Probe cancelled; connection closed.", file=sys.stderr)
        return 130
    print("Authenticated RPC verified: NTLMv2, packet privacy, and header signing.")
    print(f"Protected ServerAlive call succeeded; status {result.status}.")
    print("Connection closed. WMI access has NOT been tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
