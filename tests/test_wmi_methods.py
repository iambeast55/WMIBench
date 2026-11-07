"""Offline structural fixtures, not captured server or native-marshaler packets."""
import struct
import unittest
from unittest.mock import patch
from uuid import UUID

from remoteops.remoting import wmi_methods as methods
from remoteops.remoting import wmi_query
from remoteops.remoting.dcom_wire import DcomStatusError
from remoteops.remoting.rpc_auth_probe import MAX_RESPONSE
from remoteops.remoting.rpc_probe import RpcProtocolError

CID = UUID("00112233-4455-6677-8899-aabbccddeeff")
INPUTS = {"hDefKey": (19, 0x80000002), "sSubKeyName": (8, "A")}
CUSTOM_HEADER = bytes.fromhex(
    "4d454f57 04000000 81a612dc7f73cf11884d00aa004b2e24"
    "12f890453a1dd011891f00aa004b2e24 00000000 00000000"
)

# Independent layout from MS-WMIO, with manually calculated block sizes and heap refs.
# ClassHeap offsets: class=0, abstract=14, hDefKey=24, in=33, ID=37,
# info0=41, sSubKeyName=83, in=96, ID=100, info1=104; heap length=146.
ENUM_OBJECT = CUSTOM_HEADER + bytes.fromhex(
    "78563412 00010000 02"
    "d3000000 00 00000000 09000000 04000000"
    "0f000000 0e000000 10 0b000000 ffff"
    "02000000 18000000 29000000 53000000 68000000"
    "05 ffffffff ffffffff 92000080"
    "005f5f504152414d455445525300"  # __PARAMETERS
    "00616273747261637400"  # abstract
    "00684465664b657900"  # hDefKey
    "00696e00 00494400"  # in, ID
    "13000000 0000 00000000 00000000"
    "1c000000 21000000 12 0b000000 ffff 25000000 12 03000000 00000000"
    "00735375624b65794e616d6500"  # sSubKeyName
    "00696e00 00494400"
    "08000000 0100 04000000 00000000"
    "1c000000 60000000 12 0b000000 ffff 64000000 12 03000000 01000000"
    "2c000000 00 00000000 00 02000080 0e000000 04000000 01 11000080"
    "005f5f504152414d455445525300 004100"
)


def u32(value):
    return struct.pack("<I", value)


def aligned(data):
    return data + bytes(-len(data) % 4)


def output_object(value=0, *, class_name="__PARAMETERS", kind=19):
    # A separate output fixture, never built by the implementation's writer.
    name = b"\0" + class_name.encode("ascii") + b"\0"
    class_heap = name + b"\0abstract\0" + b"\0ReturnValue\0"
    info = len(class_heap)
    class_heap += struct.pack("<IHIII", kind, 0, 0, 0, 4)
    qualifier = struct.pack("<IBIH", len(name), 0x10, 11, 0xFFFF)
    cls = b"\0" + u32(0) + u32(5) + u32(4) + u32(15) + qualifier
    cls += u32(1) + u32(len(name) + 10) + u32(info) + b"\1" + bytes(4)
    cls += u32(0x80000000 | len(class_heap)) + class_heap
    instance = b"\0" + u32(0) + b"\0" + u32(value) + u32(4) + b"\1"
    instance += u32(0x80000000 | len(name)) + name
    payload = b"\2" + u32(len(cls) + 4) + cls + u32(len(instance) + 4) + instance
    return CUSTOM_HEADER + u32(0x12345678) + u32(len(payload)) + payload


def response(obj=None, *, status=0, outer=0x12345678, inner=0x87654321, callresult=0):
    data = bytes(8) + u32(outer)
    if outer:
        data += u32(inner if obj is not None else 0)
        if obj is not None and inner:
            data += u32(len(obj)) + u32(len(obj)) + obj
    return aligned(data) + u32(callresult) + u32(status)


def read_request(test, request, path, method):
    # Independent NDR cursor verifies fields and returns the input OBJREF.
    test.assertEqual(request[:32], bytes.fromhex(
        "05000700 00000000 00000000 33221100554477668899aabbccddeeff 00000000"
    ))
    offset = 32
    for text, ref in ((path, 0x20000), (method, 0x20004)):
        offset = (offset + 3) & ~3
        referent, maximum, size, count = struct.unpack_from("<IIII", request, offset)
        expected = text.encode("utf-16-le")
        test.assertEqual((referent, maximum, size, count),
                         (ref, len(expected) // 2, len(expected), len(expected) // 2))
        offset += 16
        test.assertEqual(request[offset:offset + size], expected)
        offset += size
    offset = (offset + 3) & ~3
    test.assertEqual(request[offset:offset + 8], bytes(8))
    offset += 8
    pointer = struct.unpack_from("<I", request, offset)[0]
    offset += 4
    obj = None
    if pointer:
        maximum, size = struct.unpack_from("<II", request, offset)
        test.assertEqual(pointer, 0x20008)
        test.assertEqual(maximum, size)
        offset += 8
        obj = request[offset:offset + size]
        offset += size
    offset = (offset + 3) & ~3
    test.assertEqual(request[offset:], bytes.fromhex("0c000200 00000000 00000000"))
    return obj


class MethodRequestTests(unittest.TestCase):
    def test_service_request_independent_complete_bytes(self):
        path = 'Win32_Service.Name="A"'
        for method in ("StopService", "StartService"):
            with self.subTest(method=method):
                request = methods.method_request((5, 7), CID, path, method, {})
                self.assertIsNone(read_request(self, request, path, method))
                self.assertEqual(request[-24:], bytes.fromhex(
                    "00000000 00000000 00000000 0c000200 00000000 00000000"
                ))

    def test_enum_key_matches_independent_object_bytes(self):
        for inputs in (INPUTS, dict(reversed(list(INPUTS.items())))):
            request = methods.method_request((5, 7), CID, "StdRegProv", "EnumKey", inputs)
            obj = read_request(self, request, "StdRegProv", "EnumKey")
            self.assertEqual(len(obj), 312)
            self.assertEqual(obj, ENUM_OBJECT)
            self.assertEqual(wmi_query.decode_instance(obj, "__PARAMETERS",
                                                      {"hDefKey": 19, "sSubKeyName": 8}),
                             {"hDefKey": 0x80000002, "sSubKeyName": "A"})

    def test_strings_uint32_boundaries_and_case(self):
        for text in ("", "ASCII", "\u00e9", "\u03a9", "\U0001f600", "A" * 1024):
            for root in (0, 0xFFFFFFFF):
                with self.subTest(text=text[:12], root=root):
                    inputs = {"sSubKeyName": (8, text), "hDefKey": (19, root)}
                    request = methods.method_request((5, 7), CID, "stdregprov", "enumkey", inputs)
                    obj = read_request(self, request, "stdregprov", "EnumKey")
                    self.assertEqual(wmi_query.decode_instance(
                        obj, "__PARAMETERS", {"hDefKey": 19, "sSubKeyName": 8}),
                        {"hDefKey": root, "sSubKeyName": text})
                    tail = (b"\0" + text.encode("latin-1") + b"\0"
                            if all(ord(c) < 256 for c in text)
                            else b"\1" + text.encode("utf-16-le") + b"\0\0")
                    self.assertTrue(obj.endswith(tail))
        for path in ('win32_service.name="A"', 'Win32_Service.Name="A\\\\B\\\"C"',
                     'Win32_Service.Name="\U0001f600"'):
            request = methods.method_request((5, 7), CID, path, "startservice", {})
            self.assertIsNone(read_request(self, request, path, "StartService"))

    def test_rejects_unsupported_methods_paths_and_inputs(self):
        cases = [
            ("Win32_Process", "Create", {}), ("StdRegProv", "DeleteKey", INPUTS),
            ("Win32_Service", "StartService", {}), ("StdRegProv", "StopService", {}),
            ('Win32_Process.Name="A"', "StopService", {}),
            ('Win32_Service.Name=""', "StopService", {}),
            ('Win32_Service.Name="A",Other="B"', "StopService", {}),
            ('Win32_Service.Name="A" trailing', "StopService", {}),
            ('Win32_Service.Name="A\\x"', "StopService", {}),
            ('Win32_Service.Name="A\nB"', "StopService", {}),
            ('root\\cimv2:Win32_Service.Name="A"', "StartService", {}),
            ("root\\default:StdRegProv", "EnumKey", INPUTS),
            ("StdRegProv", "EnumKey", {}),
            ("StdRegProv", "EnumKey", dict(INPUTS, Other=(19, 1))),
            ("StdRegProv", "EnumKey", dict(INPUTS, hDefKey=(3, 1))),
            ("StdRegProv", "EnumKey", dict(INPUTS, hDefKey=[19, 1])),
            ("StdRegProv", "EnumKey", dict(INPUTS, hDefKey=(19,))),
            ("StdRegProv", "EnumKey", dict(INPUTS, hDefKey=(19.0, 1))),
            ("StdRegProv", "EnumKey", dict(INPUTS, sSubKeyName=(0x2008, []))),
            ("StdRegProv", "EnumKey", None), (None, "EnumKey", INPUTS),
            ("StdRegProv", None, INPUTS), ("StdRegProv\0", "EnumKey", INPUTS),
            ('Win32_Service.Name="A"', "StopService", INPUTS),
        ]
        for path, method, inputs in cases:
            with self.subTest(path=path, method=method, inputs=inputs), self.assertRaises(ValueError):
                methods.method_request((5, 7), CID, path, method, inputs)
        for value in (-1, 2**32, True, 1.0, "1", None):
            with self.subTest(root=value), self.assertRaises(ValueError):
                methods.method_request((5, 7), CID, "StdRegProv", "EnumKey",
                                       dict(INPUTS, hDefKey=(19, value)))
        for value in (None, 1, b"A", "A\0B", "\ud800", "A" * 1025, "\U0001f600" * 513):
            with self.subTest(text=repr(value)[:30]), self.assertRaises(ValueError):
                methods.method_request((5, 7), CID, "StdRegProv", "EnumKey",
                                       dict(INPUTS, sSubKeyName=(8, value)))
        with patch.object(methods, "MAX_REQUEST_BYTES", 100), self.assertRaises(ValueError):
            methods.method_request((5, 7), CID, "StdRegProv", "EnumKey", INPUTS)


class MethodResponseTests(unittest.TestCase):
    def test_enum_output_arrays_and_null_are_decoded(self):
        from test_wmi_values import instance_fixture

        properties = {"ReturnValue": 19, "sNames": 0x2008}
        for values in ({"ReturnValue": 0, "sNames": ["A", "\u03a9"]},
                       {"ReturnValue": 2, "sNames": None}):
            with self.subTest(values=values):
                obj = instance_fixture("__PARAMETERS", properties, values)
                self.assertEqual(methods.decode_method(response(obj), properties), values)

    def test_independent_output_fixture_and_provider_failure(self):
        for value in (0, 5, 0xFFFFFFFF):
            self.assertEqual(methods.decode_method(response(output_object(value)), {"ReturnValue": 19}),
                             {"ReturnValue": value})

    def test_hresult_and_required_failure_pointer_shape(self):
        for status in (0x80041003, 0x80041008, 0x80004005, 1):
            with self.subTest(status=status), self.assertRaises(DcomStatusError) as caught:
                methods.decode_method(response(status=status), {"ReturnValue": 19})
            self.assertEqual(caught.exception.status, status)
        for reply in (response(output_object(), status=0x80041003),
                      response(status=0x80041003, outer=0),
                      response(status=0x80041003, callresult=1)):
            with self.assertRaises(RpcProtocolError):
                methods.decode_method(reply, {"ReturnValue": 19})

    def test_explicit_null_output_requires_complete_success_reply(self):
        self.assertEqual(methods.decode_method(response(), {"ReturnValue": 19}, allow_no_output=True), {})
        with self.assertRaisesRegex(wmi_query.WmiQueryError, "outcome unconfirmed"):
            methods.decode_method(response(), {"ReturnValue": 19})
        with self.assertRaises(DcomStatusError):
            methods.decode_method(response(status=0x80041001), {"ReturnValue": 19}, allow_no_output=True)
        for reply in (response(outer=0), response(callresult=1), response() + b"extra", response()[:-1]):
            with self.subTest(reply=reply), self.assertRaises(RpcProtocolError):
                methods.decode_method(reply, {"ReturnValue": 19}, allow_no_output=True)
        self.assertEqual(methods.decode_method(response(output_object(5)), {"ReturnValue": 19},
                                              allow_no_output=True), {"ReturnValue": 5})

    def test_null_missing_extra_and_single_pointer_shapes(self):
        valid = response(output_object())
        replies = [response(), response(outer=0), response(output_object(), inner=0),
                   response(output_object(), callresult=1), valid + bytes(4),
                   valid[:8] + valid[12:],  # Incorrect single-pointer output shape.
                   valid[:12] + valid[16:],
                   valid[:16] + u32(0xFFFFFFFF) + valid[20:],
                   valid[:20] + u32(0xFFFFFFFF) + valid[24:],
                   valid[:4] + u32(1) + valid[8:]]
        for reply in replies:
            with self.subTest(reply=reply[:24]), self.assertRaises(RpcProtocolError):
                methods.decode_method(reply, {"ReturnValue": 19})

    def test_referent_ids_are_presence_markers_and_padding_is_not_data(self):
        obj = output_object()
        valid = response(obj, outer=1, inner=1)
        pad_start = 24 + len(obj)
        padded = valid[:pad_start] + b"\xcc" * (-pad_start % 4) + valid[(pad_start + 3) & ~3:]
        self.assertEqual(methods.decode_method(padded, {"ReturnValue": 19}), {"ReturnValue": 0})
        self.assertEqual(methods.decode_method(u32(0xFFFFFFFF) + valid[4:], {"ReturnValue": 19}),
                         {"ReturnValue": 0})

    def test_truncated_at_every_byte_for_success_and_failure(self):
        for valid in (response(output_object()), response(status=0x80041003)):
            for length in range(len(valid)):
                with self.subTest(length=length, size=len(valid)), self.assertRaises(RpcProtocolError):
                    methods.decode_method(valid[:length], {"ReturnValue": 19})
        # Repair the outer interface counts for each truncated object too, so this
        # independently exercises the object decoder, not only NDR length checks.
        for length in range(len(ENUM_OBJECT)):
            with self.subTest(object_length=length), self.assertRaises(RpcProtocolError):
                methods.decode_method(response(ENUM_OBJECT[:length]), {"hDefKey": 19})

    def test_class_type_identity_and_response_bound(self):
        for obj in (output_object(class_name="WrongClass"), output_object(kind=3),
                    b"", bytes(48), output_object()[:8] + bytes(16) + output_object()[24:]):
            with self.subTest(obj=obj[:12]), self.assertRaises(RpcProtocolError):
                methods.decode_method(response(obj), {"ReturnValue": 19})
        with self.assertRaises(RpcProtocolError):
            methods.decode_method(bytes(MAX_RESPONSE + 1), {"ReturnValue": 19})

    def test_delegates_exact_schema_and_expected_class(self):
        schema = {"ReturnValue": 19, "sNames": 0x2008}
        expected = {"ReturnValue": 0, "sNames": ["A", "B"]}
        obj = output_object()
        with patch.object(wmi_query, "decode_instance", return_value=expected) as decode:
            self.assertEqual(methods.decode_method(response(obj), schema), expected)
        decode.assert_called_once_with(obj, "__PARAMETERS", schema)


if __name__ == "__main__":
    unittest.main()
