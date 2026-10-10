import struct
import unittest
from unittest.mock import patch

from remoteops.remoting import wmi_query as query
from remoteops.remoting.rpc_probe import RpcProtocolError
from test_wmi_query import block, encoded, heap, next_fixture, number


FORMATS = {2: "h", 3: "i", 4: "f", 5: "d", 8: "I", 11: "H", 16: "b", 17: "B",
           18: "H", 19: "I", 20: "q", 21: "Q", 101: "I", 102: "I", 103: "H"}


def instance_fixture(class_name, properties, values, *, flags=0x12, defaults=False,
                     ancestors=(), derivation_bytes=None, instance_class=None):
    class_heap = bytearray(encoded(class_name))
    instance_heap = bytearray(encoded(instance_class or class_name))
    nd = bytearray((len(properties) + 3) // 4)
    default_nd = bytearray(len(nd))
    table, default_table, lookup = bytearray(), bytearray(), {}

    def value_bytes(kind, value, target):
        base = kind & ~0x2000
        if kind & 0x2000:
            start = len(target)
            width = struct.calcsize("<" + FORMATS[base])
            target.extend(number(len(value)) + bytes(width * len(value)))
            for index, entry in enumerate(value):
                data = value_bytes(base, entry, target)
                target[start + 4 + index * width:start + 4 + (index + 1) * width] = data
            return number(start)
        if base in (8, 101, 102):
            offset = len(target)
            target.extend(encoded(value))
            return number(offset)
        if base == 11:
            value = 0xFFFF if value else 0
        if base == 103:
            value = ord(value)
        return struct.pack("<" + FORMATS[base], value)

    for order, (name, kind) in enumerate(properties.items()):
        name_ref = len(class_heap)
        class_heap.extend(encoded(name))
        info_ref = len(class_heap)
        class_heap.extend(struct.pack("<I H I I I", kind | 0x4000, order, len(table), 0, 4))
        lookup[name] = number(name_ref) + number(info_ref)
        width = 4 if kind & 0x2000 else struct.calcsize("<" + FORMATS[kind])
        value = values[name]
        if value is None:
            nd[order // 4] |= 1 << (2 * (order % 4))
            table.extend(b"\xff" * width)
        elif defaults:
            nd[order // 4] |= 2 << (2 * (order % 4))
            table.extend(bytes(width))
        else:
            table.extend(value_bytes(kind, value, instance_heap))
        if defaults and value is not None:
            default_table.extend(value_bytes(kind, value, class_heap))
        else:
            default_nd[order // 4] |= 1 << (2 * (order % 4))
            default_table.extend(b"\xff" * width)
    if derivation_bytes is None:
        derivation_bytes = b"".join(encoded(name) + number(len(name.encode("utf-16-le")) // 2 + 2)
                                    for name in ancestors)
    cls = b"\0" + number(0) + number(len(nd) + len(table)) + block(derivation_bytes) + block(b"")
    cls += number(len(properties)) + b"".join(lookup[name] for name in sorted(lookup))
    cls += default_nd + default_table + heap(class_heap)
    instance = b"\0" + number(0) + nd + table + block(b"") + b"\1" + heap(instance_heap)
    payload = bytes([flags]) + block(cls) + block(instance)
    envelope = number(0x574F454D) + number(4) + query.CLASS_INTERFACE.bytes_le
    envelope += query.CLASS_MARSHALER.bytes_le + bytes(8)
    return envelope + number(0x12345678) + number(len(payload)) + payload


class ValueCodecTests(unittest.TestCase):
    def test_service_subclasses_use_validated_derivation_chain(self):
        properties = {"Name": 8, "State": 8}
        values = {"Name": "TermService", "State": "Running"}
        obj = instance_fixture("Win32_TerminalService", properties, values,
                               ancestors=("Win32_Service", "Win32_BaseService", "CIM_Service"))
        self.assertEqual(query.decode_next(next_fixture(obj), "Win32_Service", properties), ([values], 0))
        for options in ({"ancestors": ("Unrelated",)},
                        {"ancestors": ("Win32_Service",), "instance_class": "Other"},
                        {"derivation_bytes": encoded("Win32_Service") + number(999)},
                        {"derivation_bytes": encoded("Win32_Service")[:-1]},
                        {"ancestors": ("Win32_Service", "win32_service")}):
            with self.subTest(options=options), self.assertRaises(query.WmiQueryError):
                obj = instance_fixture("Win32_TerminalService", properties, values, **options)
                query.decode_next(next_fixture(obj), "Win32_Service", properties)

    def test_all_scalar_types_and_defaults(self):
        cases = {2: -32768, 3: -2147483648, 4: 1.5, 5: -2.25, 8: "Windows \u03a9",
                 11: True, 16: -128, 17: 255, 18: 65535, 19: 2**32 - 1,
                 20: -(2**63), 21: 2**64 - 1, 101: "2026**********.******+***",
                 102: 'Win32_Group.Domain="LAB",Name="Admins"', 103: "\u03a9"}
        properties = {f"P{kind}": kind for kind in cases}
        values = {f"P{kind}": value for kind, value in cases.items()}
        for defaults in (False, True):
            with self.subTest(defaults=defaults):
                obj = instance_fixture("Fixture", properties, values, defaults=defaults)
                self.assertEqual(query.decode_instance(obj, "Fixture", properties), values)

    def test_arrays_empty_arrays_nulls_and_defaults(self):
        properties = {"IPs": 0x2008, "Integers": 0x2013, "Flags": 0x200B,
                      "Empty": 0x2008, "Null": 0x2008}
        values = {"IPs": ["192.0.2.1", "2001:db8::1", "\u03a9"],
                  "Integers": [0, 2**32 - 1], "Flags": [False, True], "Empty": [], "Null": None}
        for defaults in (False, True):
            with self.subTest(defaults=defaults):
                obj = instance_fixture("Fixture", properties, values, defaults=defaults)
                self.assertEqual(query.decode_next(next_fixture(obj), "Fixture", properties), ([values], 0))

    def test_projection_without_keys_and_multiple_null_types(self):
        properties = {"String": 8, "Integer": 19, "Bool": 11}
        values = dict.fromkeys(properties)
        obj = instance_fixture("Fixture", properties, values, flags=0x52)
        self.assertEqual(query.decode_instance(obj, "Fixture", properties), values)

    def test_type_and_class_mismatches_remain_errors(self):
        obj = instance_fixture("Fixture", {"Value": 19}, {"Value": 42})
        for name, properties in (("Other", {"Value": 19}), ("Fixture", {"Value": 8}),
                                  ("Fixture", {"Missing": 19})):
            with self.subTest(name=name, properties=properties), self.assertRaises(RpcProtocolError):
                query.decode_instance(obj, name, properties)

    def test_array_bounds_and_decoded_budget(self):
        obj = instance_fixture("Fixture", {"Values": 0x2008}, {"Values": ["abc", "def"]})
        with patch.object(query, "MAX_ARRAY_ITEMS", 1), self.assertRaisesRegex(RpcProtocolError, "element limit"):
            query.decode_instance(obj, "Fixture", {"Values": 0x2008})
        with patch.object(query, "MAX_DECODED_BYTES", 1), self.assertRaisesRegex(RpcProtocolError, "size limit"):
            query.decode_instance(obj, "Fixture", {"Values": 0x2008})
        for length in range(len(obj)):
            with self.subTest(length=length), self.assertRaises(RpcProtocolError):
                query.decode_instance(obj[:length], "Fixture", {"Values": 0x2008})
        with self.assertRaisesRegex(RpcProtocolError, "Truncated WMI array"):
            query._value(query.PackedReader(number(0)), 0x2013, number(2) + number(1), [1024])

    def test_invalid_boolean_nonfinite_and_embedded_objects(self):
        for kind, data in ((11, b"\x01\x00"), (4, struct.pack("<f", float("inf"))),
                           (13, bytes(4))):
            with self.subTest(kind=kind), self.assertRaises(RpcProtocolError):
                query._value(query.PackedReader(data), kind, b"", [1024])
