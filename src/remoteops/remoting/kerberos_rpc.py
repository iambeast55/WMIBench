"""Bounded NDR32 RPC associations using Kerberos packet privacy."""
import socket
import struct
from contextlib import contextmanager

from remoteops.remoting import rpc_auth_probe as wire
from remoteops.remoting.kerberos import KerberosContext, KerberosFailure
from remoteops.remoting.rpc_probe import _remaining, build_bind, parse_bind_response


def trailer(padding):
    return struct.pack("<BBBBI", 9, 6, padding, 0, 1)


def split(packet, prefix):
    auth_size = struct.unpack_from("<H", packet, 10)[0]
    start = len(packet) - auth_size - 8
    if not auth_size or start < prefix or start % 4:
        raise KerberosFailure("RPC reply omitted Kerberos packet protection.")
    kind, level, padding, reserved, context = struct.unpack_from("<BBBBI", packet, start)
    if (kind, level, reserved, context) != (9, 6, 0, 1) or padding > start - prefix:
        raise KerberosFailure("RPC security context or privacy level changed.")
    return packet[prefix:start], packet[-auth_size:], padding


def token_packet(kind, body, token):
    padding = -len(body) % 4
    body = body + bytes(padding) + trailer(padding) + token
    return wire._header(kind, len(body), len(token), 1) + body


def bind_reply(packet, expected_type, *, final=False):
    if packet[2] != expected_type:
        raise KerberosFailure("The server rejected the Kerberos RPC binding.")
    if final and struct.unpack_from("<H", packet, 10)[0] == 0:
        body, token, padding = packet[16:], b"", 0
    else:
        body, token, padding = split(packet, 16)
    if padding:
        body = body[:-padding]
    ack = parse_bind_response(wire._header(12, len(body), 0, 1) + body)
    return ack, token


class KerberosRpcConnection:
    def __init__(self, sock, context, binding, maximum):
        self.sock, self.context, self.binding = sock, context, binding
        self.maximum = maximum
        self.call_id = 1
        self.active = True

    def call(self, opnum, stub, deadline, object_id=None):
        if not self.active:
            raise KerberosFailure("The Kerberos RPC connection is closed.")
        try:
            self.call_id += 1
            # Without header signing, the verification trailer binds negotiation and call metadata.
            verify = bytes.fromhex("8ae3137102f43671") + struct.pack("<HHI", 0x8001, 4, 0)
            verify += struct.pack("<HH", 0x8002, 40) + self.binding[32:72]
            verify += struct.pack("<HHBBH4sIHH", 0xC003, 16, 0, 0, 0, b"\x10\0\0\0",
                                  self.call_id, 0, opnum)
            body = stub + bytes(-len(stub) % 4) + verify
            padding = -len(body) % 16
            signature, encrypted = self.context.wrap(body + bytes(padding), rpc=True)
            call = struct.pack("<IHH", len(body), 0, opnum)
            if object_id is not None:
                call += object_id.bytes_le
            payload = call + encrypted + trailer(padding) + signature
            packet = wire._header(0, len(payload), len(signature), self.call_id,
                                  0x83 if object_id else 3) + payload
            if len(packet) > self.maximum:
                raise KerberosFailure("RPC request exceeds the server's fragment limit.")
            self.sock.settimeout(_remaining(deadline))
            self.sock.sendall(packet)
            result = bytearray()
            for index in range(wire.MAX_RESPONSE_FRAGMENTS):
                packet = wire._read_packet(self.sock, deadline, self.call_id, fragmented=True)
                if packet[2] not in (2, 3) or bool(packet[3] & 1) != (index == 0):
                    raise KerberosFailure("Unexpected RPC response fragment.")
                prefix = 32 if packet[2] == 3 else 24
                data, signature, padding = split(packet, prefix)
                data = self.context.unwrap(signature, data, rpc=True)
                if padding:
                    if padding > len(data):
                        raise KerberosFailure("Invalid RPC response padding.")
                    data = data[:-padding]
                if struct.unpack_from("<H", packet, 20)[0] != 0:
                    raise KerberosFailure("RPC presentation context changed.")
                if packet[2] == 3:
                    status = struct.unpack_from("<I", packet, 24)[0]
                    raise KerberosFailure(f"RPC call failed (0x{status:08x}).")
                if len(result) + len(data) > wire.MAX_RESPONSE:
                    raise KerberosFailure("RPC response exceeded the size limit.")
                result.extend(data)
                if packet[3] & 2:
                    return bytes(result)
            raise KerberosFailure("RPC response exceeded the fragment limit.")
        except BaseException:
            self.active = False
            self.sock.close()
            raise


@contextmanager
def connection(provider, interface, port, deadline):
    context = KerberosContext(provider, "host", dce=True, timeout=_remaining(deadline))
    token = context.step()
    binding = build_bind(interface, 0, 0)
    family = socket.AF_INET6 if ":" in provider.address else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(_remaining(deadline))
        sock.connect((provider.address, port))
        sock.settimeout(_remaining(deadline))
        sock.sendall(token_packet(11, binding[16:], token))
        ack, token = bind_reply(wire._read_packet(sock, deadline, 1), 12)
        confirmation = context.step(token)
        sock.settimeout(_remaining(deadline))
        sock.sendall(token_packet(14, binding[16:], confirmation))
        _, final = bind_reply(wire._read_packet(sock, deadline, 1), 15, final=True)
        if final:
            from impacket.spnego import SPNEGO_NegTokenResp
            fields = SPNEGO_NegTokenResp(final).fields
            if fields.get("NegState") != b"\x00" or fields.get("ResponseToken"):
                raise KerberosFailure("Kerberos RPC negotiation did not complete.")
        channel = KerberosRpcConnection(sock, context, binding, min(wire.MAX_FRAGMENT, ack.max_receive_fragment))
        try:
            yield channel
        finally:
            channel.active = False
            context.key = None
