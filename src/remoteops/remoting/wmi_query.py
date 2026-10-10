"""Bounded query and scalar/array MS-WMI/MS-WMIO codecs.

Only catalog-projected properties are decoded. Embedded objects are unsupported;
class/instance qualifiers are skipped by bounded lengths.
"""
from __future__ import annotations

import struct
import math
from uuid import UUID

from remoteops.remoting.dcom_wire import (
    DcomStatusError, Reader, _interface_pointer, _orpcthat, decode_objref, orpcthis,
)
from remoteops.remoting.rpc_auth_probe import MAX_RESPONSE
from remoteops.remoting.rpc_probe import RpcProtocolError

ENUM_INTERFACE = UUID("027947e1-d731-11ce-a357-000000000001")
CLASS_INTERFACE = UUID("dc12a681-737f-11cf-884d-00aa004b2e24")
CLASS_MARSHALER = UUID("4590f812-1d3a-11d0-891f-00aa004b2e24")
OS_PROPERTIES = {
    "CSName": 8, "Caption": 8, "Version": 8, "BuildNumber": 8,
    "OSArchitecture": 8, "LastBootUpTime": 101, "TotalVisibleMemorySize": 21,
}
OS_QUERY = "SELECT " + ", ".join(OS_PROPERTIES) + " FROM Win32_OperatingSystem"
DICTIONARY = ('"', "key", "", "read", "write", "volatile", "provider", "dynamic",
              "cimwin32", "DWORD", "CIMTYPE")


class WmiQueryError(RpcProtocolError):
    """A command failed after a complete reply with no unknown remote references."""


def _bstr(text: str, referent: int) -> bytes:
    encoded = text.encode("utf-16-le")
    count = len(encoded) // 2
    return struct.pack("<IIII", referent, count, len(encoded), count) + encoded


def os_query_request(version, causality) -> bytes:
    return query_request(version, causality, OS_QUERY)


def query_request(version, causality, wql: str) -> bytes:
    if not wql.startswith("SELECT ") or len(wql) > 65536 or "\0" in wql:
        raise ValueError("Expected a bounded catalog SELECT query")
    data = orpcthis(version, causality) + _bstr("WQL", 0x20000)
    data += bytes(-len(data) % 4) + _bstr(wql, 0x20004)
    # Semisynchronous, forward-only, no context.
    return data + bytes(-len(data) % 4) + struct.pack("<II", 0x30, 0)


def decode_query(data: bytes):
    reader = Reader(data)
    _orpcthat(reader)
    reference = _interface_pointer(reader)
    status = reader.u32()
    reader.finish()
    if status:
        if reference is not None:
            raise RpcProtocolError("Failed ExecQuery returned an unexpected interface pointer")
        raise DcomStatusError("IWbemServices.ExecQuery", status)
    if reference is None:
        raise RpcProtocolError("Successful ExecQuery omitted its enumerator")
    return decode_objref(reference, ENUM_INTERFACE)


def next_request(version, causality, timeout_ms: int) -> bytes:
    if not 1 <= timeout_ms <= 300000:
        raise ValueError("Enumeration timeout must be 1..300000 milliseconds")
    return orpcthis(version, causality) + struct.pack("<iI", timeout_ms, 1)


class PackedReader:
    """WMIO is packed, unlike aligned NDR. All subblocks are independently bounded."""

    def __init__(self, data: bytes):
        if len(data) > MAX_RESPONSE:
            raise RpcProtocolError("WMI object exceeds the size limit")
        self.data, self.offset = data, 0

    def take(self, size):
        if size < 0 or self.offset + size > len(self.data):
            raise RpcProtocolError(f"Truncated WMI object at byte {self.offset}")
        start = self.offset
        self.offset += size
        return self.data[start:self.offset]

    def number(self, fmt):
        return struct.unpack("<" + fmt, self.take(struct.calcsize("<" + fmt)))[0]

    def block(self):
        size = self.number("I")
        if size < 4:
            raise RpcProtocolError("Invalid WMI block length")
        return PackedReader(self.take(size - 4))

    def heap(self):
        size = self.number("I")
        if not size & 0x80000000:
            raise RpcProtocolError("Invalid WMI heap length marker")
        return self.take(size & 0x7FFFFFFF)

    def string(self):
        encoding = self.number("B")
        if encoding not in (0, 1):
            raise RpcProtocolError("Unsupported WMI string encoding")
        width = encoding + 1
        start = self.offset
        while self.take(width) != bytes(width):
            pass
        try:
            return self.data[start:self.offset - width].decode(
                "latin-1" if encoding == 0 else "utf-16-le"
            )
        except UnicodeError as exc:
            raise RpcProtocolError("Invalid WMI Unicode string") from exc

    def finish(self, block_name="object"):
        if self.offset != len(self.data):
            raise RpcProtocolError(
                f"Unexpected trailing WMI data in {block_name} at byte {self.offset} "
                f"({len(self.data) - self.offset} bytes remain)"
            )


def _heap_reader(heap: bytes, offset: int):
    if not 0 <= offset < len(heap):
        raise RpcProtocolError("WMI heap reference is out of bounds")
    return PackedReader(heap[offset:])


def _string(heap: bytes, reference: int):
    if reference & 0x80000000:
        index = reference & 0x7FFFFFFF
        if index >= len(DICTIONARY):
            raise RpcProtocolError("Unknown WMI string dictionary reference")
        return DICTIONARY[index]
    return _heap_reader(heap, reference).string()


def _nd(table: bytes, index: int):
    return (table[index // 4] >> (2 * (index % 4))) & 3


def decode_os_object(data: bytes) -> dict:
    return decode_instance(data, "Win32_OperatingSystem", OS_PROPERTIES)


VALUE_FORMATS = {2: "h", 3: "i", 4: "f", 5: "d", 8: "I", 11: "H",
                 16: "b", 17: "B", 18: "H", 19: "I", 20: "q", 21: "Q",
                 101: "I", 102: "I", 103: "H"}
MAX_ARRAY_ITEMS = 65536
MAX_DECODED_BYTES = 16 * 1024 * 1024


def _value(reader, kind, heap, budget):
    base = kind & ~0x2000
    if base not in VALUE_FORMATS:
        raise RpcProtocolError(f"Unsupported CIM type 0x{kind:04x}")
    if kind & 0x2000:
        array = _heap_reader(heap, reader.number("I"))
        count = array.number("I")
        if count > MAX_ARRAY_ITEMS:
            raise RpcProtocolError("WMI array exceeds the element limit")
        width = struct.calcsize("<" + VALUE_FORMATS[base])
        if count * width > len(array.data) - array.offset:
            raise RpcProtocolError("Truncated WMI array")
        budget[0] -= count * 8
        if budget[0] < 0:
            raise RpcProtocolError("Decoded WMI values exceed the size limit")
        return [_value(array, base, heap, budget) for _ in range(count)]
    value = reader.number(VALUE_FORMATS[base])
    if base in (8, 101, 102):
        value = _string(heap, value)
    elif base == 11:
        if value not in (0, 0xFFFF):
            raise RpcProtocolError("Invalid WMI boolean encoding")
        value = bool(value)
    elif base == 103:
        value = chr(value)
    elif base in (4, 5) and not math.isfinite(value):
        raise RpcProtocolError("Non-finite WMI floating-point value is unsupported")
    budget[0] -= len(value) * 2 if isinstance(value, str) else 16
    if budget[0] < 0:
        raise RpcProtocolError("Decoded WMI values exceed the size limit")
    return value


def _custom_envelope(data: bytes) -> int:
    envelope = Reader(data, limit=MAX_RESPONSE)
    if (envelope.u32(), envelope.u32()) != (0x574F454D, 4):
        raise RpcProtocolError("Result requires a custom-marshaled WMI object")
    if envelope.guid() != CLASS_INTERFACE or envelope.guid() != CLASS_MARSHALER:
        raise RpcProtocolError("Unexpected WMI object interface or custom marshaler")
    envelope.u32()  # cbExtension and reserved are ignored per MS-DCOM 2.2.18.6.
    envelope.u32()
    return envelope.offset


def decode_instance(data: bytes, expected_class: str, properties: dict[str, int]) -> dict:
    reader = PackedReader(data[_custom_envelope(data):])
    if reader.number("I") != 0x12345678:
        raise RpcProtocolError("Invalid WMI EncodingUnit signature")
    size = reader.number("I")
    if size != len(reader.data) - 8:
        raise RpcProtocolError("WMI ObjectEncodingLength mismatch")
    flags = reader.number("B")
    # Windows also marks projected instances with 0x10 (native-marshaler smoke).
    if flags not in (2, 6, 0x12, 0x16, 0x52, 0x56):
        raise RpcProtocolError(f"Result must encode a CIM instance (flags=0x{flags:02x})")
    if flags & 4:
        reader.string()
        reader.string()
    cls = reader.block()
    if cls.number("B"):
        raise RpcProtocolError("Nonzero WMI class reserved byte")
    class_name, table_size = cls.number("I"), cls.number("I")
    derivation = cls.block()
    ancestors = set()
    while derivation.offset < len(derivation.data):
        name = derivation.string()
        # MS-WMIO 2.2.18 counts UTF-16 characters plus encoding flag and terminator.
        if derivation.number("I") != len(name.encode("utf-16-le")) // 2 + 2:
            raise RpcProtocolError("Invalid WMI superclass name length")
        if not name or name.casefold() in ancestors or len(ancestors) >= 1024:
            raise RpcProtocolError("Invalid WMI derivation list")
        ancestors.add(name.casefold())
    cls.block()
    count = cls.number("I")
    if not 1 <= count <= 1024:
        raise RpcProtocolError("WMI property count is outside 1..1024")
    lookup = [(cls.number("I"), cls.number("I")) for _ in range(count)]
    nd_size = (count + 3) // 4
    defaults_nd = cls.take(nd_size)
    defaults = cls.take(table_size - nd_size)
    class_heap = cls.heap()
    cls.finish("class block")
    actual_class = _string(class_heap, class_name)
    if actual_class.casefold() != expected_class.casefold() and expected_class.casefold() not in ancestors:
        raise RpcProtocolError(
            f"Query returned an unexpected CIM class {actual_class!r}; expected {expected_class!r} or a subclass"
        )

    instance = reader.block()
    # MS-WMIO section 3 documents insignificant trailing EncodingUnit octets.
    # Only the outer tail is ignored; its declared size and inner blocks stay bounded.
    if instance.number("B"):
        raise RpcProtocolError("Nonzero WMI instance reserved byte")
    instance_name = instance.number("I")
    nd = instance.take(nd_size)
    values = instance.take(table_size - nd_size)
    instance.block()
    qualifier_flag = instance.number("B")
    if qualifier_flag == 2:
        for _ in range(count):
            instance.block()
    elif qualifier_flag != 1:
        raise RpcProtocolError("Invalid WMI instance qualifier flag")
    heap = instance.heap()
    instance.finish("instance block")
    if _string(heap, instance_name).casefold() != actual_class.casefold():
        raise RpcProtocolError("WMI instance class name does not match its definition")

    result, names, orders = {}, set(), set()
    expected = {name.casefold(): (name, kind) for name, kind in properties.items()}
    budget = [MAX_DECODED_BYTES]
    for name_ref, info_ref in lookup:
        name = _string(class_heap, name_ref).casefold()
        info = _heap_reader(class_heap, info_ref)
        kind, order, offset = info.number("I"), info.number("H"), info.number("I")
        info.number("I")  # Class of origin.
        info.block()
        if name in names or order in orders or order >= count:
            raise RpcProtocolError("Duplicate WMI property name/order or invalid declaration order")
        names.add(name)
        orders.add(order)
        if name not in expected:
            continue
        canonical, required_type = expected[name]
        if kind & 0xBFFF != required_type:
            raise RpcProtocolError(f"Unexpected CIM type for {canonical}: 0x{kind:08x}")
        state = _nd(nd, order)
        source, value_heap = values, heap
        if state == 2:
            state = _nd(defaults_nd, order)
            source, value_heap = defaults, class_heap
            if state == 2:
                raise RpcProtocolError("Unresolved inherited WMI default")
        if state & 1:
            result[canonical] = None
            continue
        base = required_type & ~0x2000
        if base not in VALUE_FORMATS:
            raise RpcProtocolError(f"Unsupported CIM type for {canonical}: 0x{required_type:04x}")
        width = 4 if required_type & 0x2000 else struct.calcsize("<" + VALUE_FORMATS[base])
        if offset + width > len(source):
            raise RpcProtocolError("WMI property value is outside its value table")
        result[canonical] = _value(PackedReader(source[offset:offset + width]), required_type,
                                   value_heap, budget)
    if result.keys() != properties.keys():
        missing = ", ".join(name for name in properties if name not in result)
        raise RpcProtocolError(f"{expected_class} result is missing projected properties: {missing}")
    return {name: result[name] for name in properties}


def decode_next(data: bytes, expected_class="Win32_OperatingSystem",
                properties=None) -> tuple[list[dict], int]:
    reader = Reader(data, limit=MAX_RESPONSE)
    _orpcthat(reader)
    maximum, offset, count = reader.u32(), reader.u32(), reader.u32()
    if maximum != 1 or offset != 0 or count > 1:
        raise RpcProtocolError("Invalid WMI Next array bounds")
    objects = [_interface_pointer(reader) for _ in range(count)]
    returned, status = reader.u32(), reader.u32()
    reader.finish()
    if returned != count or any(obj is None for obj in objects):
        raise RpcProtocolError("WMI Next count/pointer mismatch")
    if status not in (0, 1, 0x40004):
        if count:
            raise RpcProtocolError("Failed WMI Next returned unexpected objects")
        raise DcomStatusError("IEnumWbemClassObject.Next", status)
    if status == 0 and count != 1:
        raise RpcProtocolError("Successful WMI Next omitted its requested object")
    properties = OS_PROPERTIES if properties is None else properties
    # Only this known by-value marshaler can fail decoding without leaving a live
    # result-object reference. Unknown OBJREFs or malformed NDR remain fatal.
    for obj in objects:
        _custom_envelope(obj)
    try:
        return [decode_instance(obj, expected_class, properties) for obj in objects], status
    except RpcProtocolError as exc:
        raise WmiQueryError(str(exc)) from exc
