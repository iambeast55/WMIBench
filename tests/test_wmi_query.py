import io
import struct
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch
from uuid import UUID

from remoteops.remoting import dcom_probe as probe
from remoteops.remoting import dcom_wire as wire
from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting import wmi_query as query
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError
from test_dcom import CID, IPID, OXID, activation_fixture, aligned, objref_fixture
from test_wmi_namespace import SERVICES_IPID, namespace_fixture

ENUM_IPID = UUID("bbbbbbbb-1234-5678-90ab-cdef12345678")
EXPECTED = {
    "CSName": "FIXTURE", "Caption": "Windows \u03a9", "Version": "10.0",
    "BuildNumber": "12345", "OSArchitecture": "64-bit",
    "LastBootUpTime": "20260921083000.000000-240", "TotalVisibleMemorySize": 2**40 + 1,
}


def number(value):
    return struct.pack("<I", value)


def block(data):
    return number(len(data) + 4) + data


def heap(data):
    return number(len(data) | 0x80000000) + data


def encoded(text):
    try:
        return b"\0" + text.encode("latin-1") + b"\0"
    except UnicodeError:
        return b"\1" + text.encode("utf-16-le") + b"\0\0"


def object_fixture(values=None, *, default_caption=False, flags=0x12, bad_type=False,
                   duplicate_order=False, bad_value_offset=False, bad_heap_ref=False,
                   missing=False, qualifier_flag=1, object_padding=b"",
                   class_padding=b"", instance_padding=b""):
    values = dict(EXPECTED if values is None else values)
    properties = dict(query.OS_PROPERTIES)
    if missing:
        properties.pop("BuildNumber")
    class_heap = bytearray(encoded("Win32_OperatingSystem"))
    instance_heap = bytearray(encoded("Win32_OperatingSystem"))
    lookup, defaults, data = {}, bytearray(), bytearray()
    nd = bytearray((len(properties) + 3) // 4)
    default_nd = bytearray(len(nd))
    for order, (name, kind) in enumerate(properties.items()):
        name_offset = len(class_heap)
        class_heap.extend(encoded(name))
        info_offset = len(class_heap)
        class_heap.extend(struct.pack(
            "<I H I I I", (3 if bad_type and order == 0 else kind) | 0x4000,
            0 if duplicate_order else order,
            0xFFFFFFFF if bad_value_offset else len(data), 0, 4,
        ))
        lookup[name] = number(name_offset) + number(info_offset)
        width = 8 if kind == 21 else 4
        value = values[name]
        if default_caption and name == "Caption":
            nd[order // 4] |= 2 << (2 * (order % 4))
            defaults.extend(number(len(class_heap)))
            class_heap.extend(encoded(value))
        else:
            default_nd[order // 4] |= 1 << (2 * (order % 4))
            defaults.extend(bytes(width))
        if value is None:
            nd[order // 4] |= 1 << (2 * (order % 4))
            data.extend(b"\xff" * width)
        elif kind == 21:
            data.extend(struct.pack("<Q", value))
        else:
            data.extend(number(0x7FFFFFFF if bad_heap_ref else len(instance_heap)))
            instance_heap.extend(encoded(value))
    cls = b"\0" + number(0) + number(len(nd) + len(data))
    cls += block(b"") + block(b"") + number(len(properties))
    cls += b"".join(lookup[name] for name in sorted(lookup))
    cls += default_nd + defaults + heap(class_heap)
    instance = b"\0" + number(0) + nd + data + block(b"") + bytes([qualifier_flag])
    if qualifier_flag == 2:
        instance += block(b"") * len(properties)
    instance += heap(instance_heap)
    payload = bytes([flags])
    if flags & 4:
        payload += encoded("FIXTURE") + encoded("root\\cimv2")
    payload += block(cls + class_padding) + block(instance + instance_padding) + object_padding
    custom = number(0x574F454D) + number(4) + query.CLASS_INTERFACE.bytes_le
    custom += query.CLASS_MARSHALER.bytes_le + bytes(8)
    return custom + number(0x12345678) + number(len(payload)) + payload


def query_fixture(status=0, oxid=OXID):
    if status:
        return bytes(12) + number(status)
    ref = objref_fixture(iid=query.ENUM_INTERFACE, oxid=oxid, public_refs=7)
    ref = ref[:48] + ENUM_IPID.bytes_le + ref[64:]
    return namespace_fixture(objref=ref)


def next_fixture(obj=None, status=0):
    prefix = bytes(8) + struct.pack("<III", 1, 0, int(obj is not None))
    if obj is not None:
        prefix += struct.pack("<III", 0x20000, len(obj), len(obj)) + obj
    return aligned(prefix) + struct.pack("<II", int(obj is not None), status)


class QueryWireTests(unittest.TestCase):
    def test_fixed_query_request_bstr_counts_and_flags(self):
        request = query.os_query_request((5, 7), CID)
        self.assertEqual(request[:32], wire.orpcthis((5, 7), CID))
        self.assertEqual(request[32:56], bytes.fromhex(
            "00000200 03000000 06000000 03000000 570051004c00 0000"
        ))
        referent, maximum, byte_count, count = struct.unpack_from("<IIII", request, 56)
        self.assertEqual(referent, 0x20004)
        self.assertEqual((maximum, byte_count, count),
                         (len(query.OS_QUERY), len(query.OS_QUERY) * 2, len(query.OS_QUERY)))
        self.assertEqual(request[72:72 + byte_count].decode("utf-16-le"), query.OS_QUERY)
        self.assertEqual(request[-8:], struct.pack("<II", 0x30, 0))

    def test_next_request_has_finite_wait_and_one_object(self):
        self.assertEqual(query.next_request((5, 7), CID, 321)[32:],
                         bytes.fromhex("41010000 01000000"))
        for timeout in (0, -1, 300001):
            with self.assertRaises(ValueError):
                query.next_request((5, 7), CID, timeout)

    def test_query_interface_identity_and_errors(self):
        ref = query.decode_query(query_fixture())
        self.assertEqual((ref.ipid, ref.public_refs), (ENUM_IPID, 7))
        with self.assertRaisesRegex(wire.DcomStatusError, "0x80041017"):
            query.decode_query(query_fixture(status=0x80041017))
        with self.assertRaisesRegex(RpcProtocolError, "omitted"):
            query.decode_query(bytes(16))
        with self.assertRaisesRegex(RpcProtocolError, "expected"):
            query.decode_query(namespace_fixture())
        with self.assertRaisesRegex(RpcProtocolError, "unexpected interface"):
            query.decode_query(query_fixture()[:-4] + number(0x80041003))

    def test_scalar_values_defaults_nulls_and_declaration_order(self):
        for options in ({}, {"flags": 6}, {"default_caption": True}, {"qualifier_flag": 2}):
            with self.subTest(options=options):
                self.assertEqual(query.decode_os_object(object_fixture(**options)), EXPECTED)
        values = dict(EXPECTED, Caption=None, TotalVisibleMemorySize=None, Version="")
        self.assertEqual(query.decode_os_object(object_fixture(values)), values)
        values = dict(EXPECTED, Caption="Windows \u00e9", LastBootUpTime="2026**********.******+***")
        self.assertEqual(query.decode_os_object(object_fixture(values)), values)

    def test_malformed_objects_fail_with_protocol_errors(self):
        for options in ({"bad_type": True}, {"duplicate_order": True},
                        {"bad_value_offset": True}, {"bad_heap_ref": True},
                        {"missing": True}, {"flags": 1}, {"qualifier_flag": 3}):
            with self.subTest(options=options), self.assertRaises(RpcProtocolError):
                query.decode_os_object(object_fixture(**options))
        valid = object_fixture()
        for index in (0, 4, 8, 24, 48, 52, 57, 61, 65):
            damaged = bytearray(valid)
            damaged[index] ^= 0xFF
            with self.subTest(index=index), self.assertRaises(RpcProtocolError):
                query.decode_os_object(bytes(damaged))
        for length in range(len(valid)):
            with self.subTest(length=length), self.assertRaises(RpcProtocolError):
                query.decode_os_object(valid[:length])
        with self.assertRaises(RpcProtocolError):
            query.decode_os_object(valid + b"extra")

    def test_outer_padding_inside_declared_object_length(self):
        for flags in (2, 6, 0x12, 0x16):
            for padding in (b"\0", b"\0" * 16, bytes(range(256))):
                with self.subTest(flags=flags, padding_length=len(padding)):
                    obj = object_fixture(flags=flags, object_padding=padding)
                    self.assertEqual(query.decode_os_object(obj), EXPECTED)
                    self.assertEqual(query.decode_next(next_fixture(obj)), ([EXPECTED], 0))

    def test_outer_padding_reproduces_reported_byte_offset(self):
        # Grow a synthetic ANSI value to place the object end at the reported offset.
        values = dict(EXPECTED, Caption="Windows")
        missing = 1333 - (len(object_fixture(values)) - 48)
        self.assertGreater(missing, 0)
        values["Caption"] += "x" * missing
        self.assertEqual(len(object_fixture(values)) - 48, 1333)
        obj = object_fixture(values, object_padding=b"\xa5" * 11)
        self.assertEqual(query.decode_os_object(obj), values)

    def test_padding_does_not_relax_inner_blocks_or_outer_size(self):
        for options in ({"class_padding": b"\0"}, {"instance_padding": b"\0"},
                        {"bad_heap_ref": True}, {"bad_value_offset": True}):
            with self.subTest(options=options), self.assertRaises(RpcProtocolError):
                query.decode_os_object(object_fixture(object_padding=bytes(16), **options))
        obj = object_fixture(object_padding=bytes(16))
        for damaged in (obj[:-1], obj + b"\0", obj[:52] + number(0xFFFFFFFF) + obj[56:]):
            with self.subTest(length=len(damaged)), self.assertRaises(RpcProtocolError):
                query.decode_os_object(damaged)

    def test_next_status_count_and_object_bounds(self):
        self.assertEqual(query.decode_next(next_fixture(object_fixture())), ([EXPECTED], 0))
        self.assertEqual(query.decode_next(next_fixture(status=1)), ([], 1))
        self.assertEqual(query.decode_next(next_fixture(status=0x40004)), ([], 0x40004))
        with self.assertRaisesRegex(wire.DcomStatusError, "0x80041003"):
            query.decode_next(next_fixture(status=0x80041003))
        for reply in (next_fixture(), next_fixture(object_fixture(), status=0x80041003),
                      bytes(8) + struct.pack("<III", 0xFFFFFFFF, 0, 1),
                      next_fixture(status=1)[:-8] + number(1) + number(1)):
            with self.subTest(reply=reply[:20]), self.assertRaises(RpcProtocolError):
                query.decode_next(reply)
        valid = next_fixture(object_fixture())
        for length in range(len(valid)):
            with self.subTest(length=length), self.assertRaises(RpcProtocolError):
                query.decode_next(valid[:length])

    def test_dictionary_and_string_validation(self):
        self.assertEqual(query._string(b"", 0x80000002), "")
        for heap_data, offset in ((b"", 0), (b"\0text", 0), (b"\2x\0", 0),
                                  (b"\1\0\xd8\0\0", 0), (b"", 0x800000FF)):
            with self.assertRaises(RpcProtocolError):
                query._string(heap_data, offset)


class QueryWorkflowTests(unittest.TestCase):
    def run_probe(self, *, query_reply=None, query_error=None, next_replies=None,
                  release_error=None, bind_error=None, callback=None):
        self.channels, self.connections, self.closed = {}, [], []

        @contextmanager
        def connect(address, username, password, interface, port, deadline):
            self.connections.append((address, interface, port, deadline))
            if interface == bind_error:
                raise RpcProbeError("synthetic bind failure")
            channel = Mock()
            self.channels[interface] = channel
            if interface == wire.ACTIVATION:
                channel.call.return_value = activation_fixture()
            elif interface == wire.WBEM_LOGIN_INTERFACE:
                channel.call.return_value = namespace_fixture()
            elif interface == wire.WBEM_SERVICES_INTERFACE:
                channel.call.return_value = query_fixture() if query_reply is None else query_reply
                channel.call.side_effect = query_error
            elif interface == query.ENUM_INTERFACE:
                channel.call.side_effect = next_replies if next_replies is not None else [
                    next_fixture(object_fixture()), next_fixture(status=1),
                ]
            else:
                channel.call.return_value = bytes(12)
                channel.call.side_effect = release_error
            try:
                yield channel
            finally:
                self.closed.append(interface)

        with patch.object(probe, "discover_version", return_value=(5, 7)), \
                patch.object(rpc, "ntlm_connection", side_effect=connect):
            return probe.probe_dcom("192.0.2.10", "synthetic", "synthetic", query_os=True,
                                    progress=callback)

    def release_count(self):
        request = self.channels[wire.REM_UNKNOWN].call.call_args.args[1]
        return struct.unpack_from("<H", request, 32)[0]

    def test_complete_query_then_release_exact_three_reference_counts(self):
        result = self.run_probe()
        self.assertEqual(result.records, (EXPECTED,))
        self.assertEqual(self.release_count(), 3)
        self.assertEqual([iid for _, iid, _, _ in self.connections], [
            wire.ACTIVATION, wire.WBEM_LOGIN_INTERFACE, wire.WBEM_SERVICES_INTERFACE,
            query.ENUM_INTERFACE, wire.REM_UNKNOWN,
        ])
        self.assertTrue(all(ip == "192.0.2.10" for ip, _, _, _ in self.connections))
        self.assertEqual(len({deadline for _, _, _, deadline in self.connections[:-1]}), 1)
        self.assertEqual(len(self.closed), 5)
        self.assertEqual(self.channels[wire.WBEM_SERVICES_INTERFACE].call.call_args.args[0], 20)
        self.assertEqual(self.channels[wire.WBEM_SERVICES_INTERFACE].call.call_args.args[3],
                         SERVICES_IPID)
        for call in self.channels[query.ENUM_INTERFACE].call.call_args_list:
            self.assertEqual((call.args[0], call.args[3]), (4, ENUM_IPID))
        release = self.channels[wire.REM_UNKNOWN].call.call_args.args[1]
        self.assertEqual(release[40:56], IPID.bytes_le)
        self.assertEqual(release[64:80], SERVICES_IPID.bytes_le)
        self.assertEqual(release[88:104], ENUM_IPID.bytes_le)
        self.assertEqual([struct.unpack_from("<II", release, pos) for pos in (56, 80, 104)],
                         [(5, 0), (3, 0), (7, 0)])

    def test_query_denial_releases_known_refs(self):
        with self.assertRaisesRegex(RpcProbeError, "0x80041003") as caught:
            self.run_probe(query_reply=query_fixture(status=0x80041003))
        self.assertEqual(self.release_count(), 2)
        self.assertNotIn("enumerator cleanup unconfirmed", str(caught.exception))

    def test_uncertain_query_does_not_claim_complete_cleanup(self):
        for options in ({"query_reply": b"bad"}, {"query_error": TimeoutError()},
                        {"query_reply": query_fixture(oxid=1234)}):
            with self.subTest(options=options):
                with self.assertRaisesRegex(RpcProbeError, "query enumerator cleanup unconfirmed"):
                    self.run_probe(**options)
                self.assertEqual(self.release_count(), 2)

    def test_failed_query_and_enum_bind_release_only_acquired_refs(self):
        for interface, count in ((wire.WBEM_SERVICES_INTERFACE, 2), (query.ENUM_INTERFACE, 3)):
            with self.assertRaisesRegex(RpcProbeError, "synthetic bind failure"):
                self.run_probe(bind_error=interface)
            self.assertEqual(self.release_count(), count)

    def test_empty_timeout_duplicate_and_lost_results_are_failures(self):
        for replies in ([next_fixture(status=1)], [next_fixture(status=0x40004)],
                        [next_fixture(object_fixture()), next_fixture(object_fixture())],
                        [TimeoutError()], [b"malformed"],
                        [next_fixture(status=0x80041003)]):
            with self.subTest(replies=len(replies)), self.assertRaises(RpcProbeError):
                self.run_probe(next_replies=replies)
            self.assertEqual(self.release_count(), 3)

    def test_cancellation_or_output_failure_still_releases_all(self):
        for failure in (KeyboardInterrupt, BrokenPipeError):
            def callback(message):
                if "OS query decoded" in message:
                    raise failure()
            with self.assertRaisesRegex(RpcProbeError, "release confirmed"):
                self.run_probe(callback=callback)
            self.assertEqual(self.release_count(), 3)

    def test_release_failure_prevents_success_without_retry(self):
        with self.assertRaisesRegex(RpcProbeError, "release could not be confirmed"):
            self.run_probe(release_error=TimeoutError())
        self.channels[wire.REM_UNKNOWN].call.assert_called_once()

    @patch.object(probe, "discover_version")
    def test_invalid_query_namespace_never_connects(self, discovery):
        with self.assertRaises(ValueError):
            probe.probe_dcom("192.0.2.10", "synthetic", "synthetic", query_os=True,
                             namespace="root/other")
        discovery.assert_not_called()

    @patch.object(probe.getpass, "getpass", return_value="synthetic-secret")
    @patch.object(probe, "probe_dcom")
    def test_cli_outputs_structured_result_only_on_success(self, diagnostic, prompt):
        diagnostic.return_value = probe.OsQueryResult(
            wire.decode_activation(activation_fixture()), (EXPECTED,)
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(probe.main(["192.0.2.10", "--username", "synthetic", "--query-os"]), 0)
        self.assertTrue(diagnostic.call_args.kwargs["query_os"])
        self.assertIn('"TotalVisibleMemorySize": 1099511627777', output.getvalue())
        self.assertIn("OS query and reference release verified", output.getvalue())
        self.assertNotIn("synthetic-secret", output.getvalue())
        diagnostic.side_effect = RpcProbeError("release failed")
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            self.assertEqual(probe.main(["192.0.2.10", "--username", "synthetic", "--query-os"]), 1)
        self.assertNotIn("CSName", output.getvalue())

    @patch.object(probe.getpass, "getpass")
    def test_invalid_cli_namespace_rejected_before_password(self, prompt):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            probe.main(["192.0.2.10", "--username", "synthetic", "--query-os",
                        "--namespace", "root/other"])
        prompt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
