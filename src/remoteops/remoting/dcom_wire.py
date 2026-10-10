"""Bounded NDR32 codecs for WMI activation, namespace login, and release.

Only the fixed structures needed by this milestone are implemented, not a generic
NDR engine. Custom/handler OBJREFs and ORPC extensions fail explicitly.
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from uuid import UUID

from remoteops.remoting.rpc_probe import MAX_FRAGMENT, RpcProbeError, RpcProtocolError

ACTIVATION = UUID("4d9f4ab8-7d1c-11cf-861e-0020af6e7c57")
WBEM_LOGIN_CLASS = UUID("8bc3f05e-d86b-11d0-a075-00c04fb68820")
WBEM_LOGIN_INTERFACE = UUID("f309ad18-d86a-11d0-a075-00c04fb68820")
WBEM_SERVICES_INTERFACE = UUID("9556dc99-828c-11cf-a37e-00aa003240c7")
REM_UNKNOWN = UUID("00000131-0000-0000-c000-000000000046")
TCP = 7
CLIENT_VERSION = (5, 7)


class DcomStatusError(RpcProbeError):
    def __init__(self, operation: str, status: int):
        descriptions = {
            0x80070005: "access denied; check DCOM activation/access permissions",
            0x80040154: "class not registered",
            0x80004002: "requested interface is unavailable",
            0x800706BA: "RPC server unavailable",
            5: "access denied",
            0x80041003: "WMI namespace access denied; check Enable Account and Remote Enable rights",
            0x80041008: "invalid WMI parameter",
            0x8004100E: "WMI namespace does not exist",
        }
        self.status = status
        super().__init__(f"{operation}: {descriptions.get(status, 'server reported failure')} "
                         f"(0x{status:08x})")


class Reader:
    def __init__(self, data: bytes, *, limit: int = MAX_FRAGMENT):
        if len(data) > limit:
            raise RpcProtocolError("DCOM data exceeds this milestone's size limit")
        self.data = data
        self.offset = 0

    def take(self, size: int, alignment: int = 1) -> bytes:
        start = (self.offset + alignment - 1) & ~(alignment - 1)
        if size < 0 or start + size > len(self.data):
            raise RpcProtocolError(f"Truncated DCOM data at byte {self.offset}")
        self.offset = start + size
        return self.data[start:self.offset]

    def u16(self):
        return struct.unpack("<H", self.take(2, 2))[0]

    def u32(self):
        return struct.unpack("<I", self.take(4, 4))[0]

    def u64(self):
        return struct.unpack("<Q", self.take(8, 8))[0]

    def guid(self):
        return UUID(bytes_le=self.take(16, 4))

    def finish(self):
        if self.offset != len(self.data):
            raise RpcProtocolError(f"Unexpected trailing DCOM data at byte {self.offset}")


@dataclass(frozen=True)
class Bindings:
    strings: tuple[tuple[int, str], ...]
    security: tuple[tuple[int, int, str], ...]

    def tcp_port(self) -> int:
        for protocol, address in self.strings:
            if protocol != TCP:
                continue
            match = re.fullmatch(r".+\[([0-9]{1,5})\]", address)
            if match and 1 <= int(match[1]) <= 65535:
                # Never follow the returned hostname/IP. The caller keeps its approved IP.
                return int(match[1])
        raise RpcProtocolError("DCOM returned no supported TCP endpoint; no fallback attempted")

    def require_ntlm(self):
        if not any(service == 10 for service, _, _ in self.security):
            raise RpcProtocolError("DCOM did not advertise NTLM; no authentication fallback attempted")


def _binding_string(units: tuple[int, ...], start: int, end: int):
    try:
        stop = units.index(0, start, end)
    except ValueError as exc:
        raise RpcProtocolError("Unterminated DCOM binding string") from exc
    try:
        text = struct.pack("<" + "H" * (stop - start), *units[start:stop]).decode("utf-16-le")
    except UnicodeError as exc:
        raise RpcProtocolError("Invalid UTF-16 DCOM binding string") from exc
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        raise RpcProtocolError("Control characters in DCOM binding string")
    return text, stop + 1


def read_bindings(reader: Reader, *, conformant: bool) -> Bindings:
    maximum = reader.u32() if conformant else None
    count, security_offset = reader.u16(), reader.u16()
    if not 4 <= count <= MAX_FRAGMENT // 2 or not 2 <= security_offset <= count - 2:
        raise RpcProtocolError("Invalid DCOM binding array bounds")
    if maximum is not None and maximum != count:
        raise RpcProtocolError("DCOM binding conformance count mismatch")
    units = struct.unpack("<" + "H" * count, reader.take(count * 2, 2))
    strings, security = [], []
    position = 0
    while position < security_offset and units[position]:
        protocol = units[position]
        value, position = _binding_string(units, position + 1, security_offset)
        strings.append((protocol, value))
    if position == security_offset or any(units[position:security_offset]):
        raise RpcProtocolError("Missing DCOM string-binding terminator")
    position = security_offset
    while position < count and units[position]:
        if position + 2 >= count:
            raise RpcProtocolError("Truncated DCOM security binding")
        service, authorization = units[position:position + 2]
        value, position = _binding_string(units, position + 2, count)
        security.append((service, authorization, value))
    if position == count or any(units[position:]):
        raise RpcProtocolError("Missing DCOM security-binding terminator")
    return Bindings(tuple(strings), tuple(security))


def _status(operation: str, value: int):
    if value:
        raise DcomStatusError(operation, value)


def _version(major: int, minor: int):
    if major != 5 or minor < 1:
        raise RpcProtocolError(f"Unsupported DCOM version {major}.{minor}")
    return major, min(minor, CLIENT_VERSION[1])


def decode_server_alive2(data: bytes):
    reader = Reader(data)
    version = _version(reader.u16(), reader.u16())
    pointer = reader.u32()
    bindings = read_bindings(reader, conformant=True) if pointer else None
    reader.u32()  # Reserved; ignored on receipt per MS-DCOM.
    _status("ServerAlive2", reader.u32())
    reader.finish()
    if bindings is None:
        raise RpcProtocolError("ServerAlive2 omitted its bindings")
    bindings.require_ntlm()
    return version


def orpcthis(version: tuple[int, int], causality: UUID) -> bytes:
    return struct.pack("<HHII", *version, 0, 0) + causality.bytes_le + bytes(4)


def activation_request(version: tuple[int, int], causality: UUID) -> bytes:
    # Top-level ref pointers are inline; the unique IID array includes referent and conformance.
    data = orpcthis(version, causality) + WBEM_LOGIN_CLASS.bytes_le
    data += struct.pack("<IIIIIII", 0, 0, 2, 0, 1, 0x20000, 1)
    data += WBEM_LOGIN_INTERFACE.bytes_le
    data += struct.pack("<H2xIH", 1, 1, TCP)
    return data


def _orpcthat(reader: Reader):
    # MS-DCOM 2.2.13.4 allows arbitrary flags and requires ignoring them on receipt.
    flags, extensions = reader.u32(), reader.u32()
    if extensions:
        raise RpcProtocolError(
            "Unsupported ORPCTHAT extensions "
            f"(flags=0x{flags:08x}, extension referent=0x{extensions:08x}); "
            "this diagnostic requires a null extension pointer"
        )


@dataclass(frozen=True)
class ObjectReference:
    iid: UUID
    flags: int
    public_refs: int
    oxid: int
    oid: int
    ipid: UUID
    resolver_bindings: Bindings


def decode_objref(data: bytes, expected_iid: UUID = WBEM_LOGIN_INTERFACE) -> ObjectReference:
    reader = Reader(data)
    if reader.u32() != 0x574F454D or reader.u32() != 1:
        raise RpcProtocolError("Only standard DCOM OBJREFs are supported")
    iid = reader.guid()
    flags, public_refs, oxid, oid = reader.u32(), reader.u32(), reader.u64(), reader.u64()
    ipid = reader.guid()
    bindings = read_bindings(reader, conformant=False)
    reader.finish()
    if flags & ~0x1000 or not public_refs or not oxid or not oid or not ipid.int:
        raise RpcProtocolError("Invalid DCOM standard object reference")
    if iid != expected_iid:
        raise RpcProtocolError(f"DCOM returned interface {iid}; expected {expected_iid}")
    return ObjectReference(iid, flags, public_refs, oxid, oid, ipid, bindings)


def _interface_pointer(reader: Reader) -> bytes | None:
    if not reader.u32():
        return None
    maximum, size = reader.u32(), reader.u32()
    if maximum != size:
        raise RpcProtocolError("DCOM interface byte-array conformance mismatch")
    return reader.take(size)


@dataclass(frozen=True)
class ActivationResult:
    oxid: int
    bindings: Bindings
    remote_unknown: UUID
    auth_hint: int
    version: tuple[int, int]
    reference: ObjectReference


def decode_activation(data: bytes) -> ActivationResult:
    reader = Reader(data)
    _orpcthat(reader)
    oxid = reader.u64()
    pointer = reader.u32()
    bindings = read_bindings(reader, conformant=True) if pointer else None
    remote_unknown, auth_hint = reader.guid(), reader.u32()
    major, minor = reader.u16(), reader.u16()
    overall_status = reader.u32()
    _status("RemoteActivation", overall_status)
    if reader.u32() != 1:
        raise RpcProtocolError("Activation must return exactly one interface result")
    objref_data = _interface_pointer(reader)
    if reader.u32() != 1:
        raise RpcProtocolError("Activation must return exactly one interface status")
    interface_status, rpc_status = reader.u32(), reader.u32()
    reader.finish()
    _status("RemoteActivation RPC status", rpc_status)
    _status("IWbemLevel1Login activation", interface_status)
    if bindings is None or objref_data is None or not remote_unknown.int:
        raise RpcProtocolError("Successful activation omitted required interface/binding data")
    reference = decode_objref(objref_data)
    if reference.oxid != oxid:
        raise RpcProtocolError("Activation and object-reference OXIDs disagree")
    return ActivationResult(oxid, bindings, remote_unknown, auth_hint,
                            _version(major, minor), reference)


def normalize_namespace(namespace: str) -> str:
    namespace = namespace.replace("/", "\\")
    if len(namespace) > 256 or not re.fullmatch(
        r"root(?:\\[A-Za-z_][A-Za-z_0-9]*)*", namespace, flags=re.IGNORECASE
    ):
        raise ValueError("Namespace must be a root-relative path such as root/cimv2 (max 256 chars)")
    return namespace


def namespace_login_request(version: tuple[int, int], causality: UUID, namespace: str) -> bytes:
    encoded = (normalize_namespace(namespace) + "\0").encode("utf-16-le")
    count = len(encoded) // 2
    # Unique LPWSTR: referent, conformant/varying counts, terminated UTF-16.
    data = orpcthis(version, causality) + struct.pack("<IIII", 0x20000, count, 0, count)
    data += encoded
    # Default locale, flags=0, null IWbemContext. Authentication stays in RPC.
    return data + bytes(-len(data) % 4) + bytes(12)


def decode_namespace_login(data: bytes) -> ObjectReference:
    reader = Reader(data)
    _orpcthat(reader)
    objref_data = _interface_pointer(reader)
    status = reader.u32()
    reader.finish()
    if status:
        if objref_data is not None:
            raise RpcProtocolError(
                f"NTLMLogin failure 0x{status:08x} included an unexpected interface pointer"
            )
        _status("IWbemLevel1Login.NTLMLogin", status)
    if objref_data is None:
        raise RpcProtocolError("Successful NTLMLogin omitted the IWbemServices interface")
    return decode_objref(objref_data, WBEM_SERVICES_INTERFACE)


def release_request(result: ActivationResult, causality: UUID,
                    references: tuple[ObjectReference, ...] | None = None) -> bytes:
    references = (result.reference,) if references is None else references
    if not 1 <= len(references) <= 3 or any(ref.oxid != result.oxid for ref in references):
        raise RpcProtocolError("Release requires one to three references from the activated exporter")
    data = orpcthis(result.version, causality) + struct.pack("<H2xI", len(references), len(references))
    for reference in references:
        data += reference.ipid.bytes_le + struct.pack("<II", reference.public_refs, 0)
    return data


def decode_release(data: bytes):
    reader = Reader(data)
    _orpcthat(reader)
    _status("IRemUnknown.RemRelease", reader.u32())
    reader.finish()


def complex_ping_request(set_id: int, sequence: int, added: tuple[int, ...],
                         removed: tuple[int, ...]) -> bytes:
    if not 1 <= sequence <= 65535 or len(added) + len(removed) > 6:
        raise RpcProtocolError("Invalid DCOM ping sequence or object count")
    if not added and not removed:
        raise RpcProtocolError("ComplexPing requires an object-set change")
    data = struct.pack("<QHHH", set_id, sequence, len(added), len(removed))
    for index, objects in enumerate((added, removed)):
        if len(set(objects)) != len(objects) or any(not 0 < oid < 2**64 for oid in objects):
            raise RpcProtocolError("Invalid DCOM ping object list")
        data += bytes(-len(data) % 4)
        data += struct.pack("<I", 0x20000 + index * 4 if objects else 0)
        if objects:
            data += struct.pack("<I", len(objects))
            data += bytes(-len(data) % 8)
            data += struct.pack("<" + "Q" * len(objects), *objects)
    return data


def decode_complex_ping(data: bytes, expected_set: int = 0) -> int:
    reader = Reader(data)
    set_id = reader.u64()
    reader.u16()  # Backoff hint may be ignored per MS-DCOM 3.2.6.1.
    status = reader.u32()
    reader.finish()
    _status("IObjectExporter.ComplexPing", status)
    if not set_id or (expected_set and set_id != expected_set):
        raise RpcProtocolError("ComplexPing returned an invalid or changed set ID")
    return set_id


def simple_ping_request(set_id: int) -> bytes:
    if not 0 < set_id < 2**64:
        raise RpcProtocolError("SimplePing requires an established set ID")
    return struct.pack("<Q", set_id)


def decode_simple_ping(data: bytes):
    reader = Reader(data)
    status = reader.u32()
    reader.finish()
    _status("IObjectExporter.SimplePing", status)
