import struct
import threading
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from spnego.exceptions import BadMICError

from remoteops.remoting import dcom_client as client_module
from remoteops.remoting import dcom_wire as wire
from remoteops.remoting import rpc_auth_probe as rpc
from remoteops.remoting import wmi_query as query
from remoteops.remoting.rpc_probe import RpcProbeError, RpcProtocolError
from test_dcom import IPID, OXID, activation_fixture, objref_fixture
from test_wmi_namespace import SERVICES_IPID, namespace_fixture
from test_wmi_query import ENUM_IPID, EXPECTED, next_fixture, number, object_fixture


def interface_reply(iid, ipid, oid, *, oxid=OXID, flags=0):
    reference = bytearray(objref_fixture(iid=iid, oxid=oxid))
    struct.pack_into("<I", reference, 24, flags)
    struct.pack_into("<Q", reference, 40, oid)
    reference[48:64] = ipid.bytes_le
    return namespace_fixture(objref=bytes(reference))


def ping_reply(set_id=999, status=0):
    return struct.pack("<QH2xI", set_id, 0, status)


class PingWireTests(unittest.TestCase):
    def test_add_and_delete_ndr_alignment(self):
        self.assertEqual(wire.complex_ping_request(0, 1, (456,), ()), bytes.fromhex(
            "0000000000000000 0100 0100 0000 0000"
            "00000200 01000000 c801000000000000 00000000"
        ))
        self.assertEqual(wire.complex_ping_request(999, 2, (), (456,)), bytes.fromhex(
            "e703000000000000 0200 0000 0100 0000"
            "00000000 04000200 01000000 00000000 c801000000000000"
        ))
        self.assertEqual(wire.simple_ping_request(999), struct.pack("<Q", 999))

    def test_ping_responses_and_bounds(self):
        self.assertEqual(wire.decode_complex_ping(ping_reply()), 999)
        wire.decode_simple_ping(bytes(4))
        for reply in (ping_reply(0), ping_reply() + b"extra", ping_reply()[:-1]):
            with self.assertRaises(RpcProtocolError):
                wire.decode_complex_ping(reply)
        with self.assertRaisesRegex(RpcProtocolError, "changed set ID"):
            wire.decode_complex_ping(ping_reply(1234), 999)
        with self.assertRaisesRegex(wire.DcomStatusError, "0x0000077b"):
            wire.decode_simple_ping(number(1915))
        for args in ((0, 0, (1,), ()), (0, 65536, (1,), ()), (0, 1, (), ()),
                     (0, 1, (1, 1), ()), (0, 1, (0,), ())):
            with self.assertRaises(RpcProtocolError):
                wire.complex_ping_request(*args)


class ClientFixture(unittest.TestCase):
    def setUp(self):
        self.opened, self.closed, self.calls, self.channels = [], [], [], {}
        self.overrides = {}
        self.clock = 100.0
        self.query_count = 0
        self.enum_position = 0
        self.service_flags = 0
        self.enum_oid = 789
        self.enum_oxid = OXID
        self.object_padding = b""

        @contextmanager
        def connect(address, username, password, interface, port, deadline):
            self.opened.append((interface, address, port))
            channel = Mock(active=True)

            def call(opnum, stub, deadline, object_id=None):
                self.calls.append((interface, opnum, stub, deadline, object_id))
                override = self.overrides.get((interface, opnum))
                if override is not None:
                    if isinstance(override, BaseException):
                        channel.active = False
                        raise override
                    if callable(override):
                        return override()
                    return override
                if interface == wire.ACTIVATION:
                    return activation_fixture()
                if interface == wire.WBEM_LOGIN_INTERFACE:
                    return interface_reply(wire.WBEM_SERVICES_INTERFACE, SERVICES_IPID,
                                           456, flags=self.service_flags)
                if interface == wire.WBEM_SERVICES_INTERFACE:
                    self.query_count += 1
                    self.enum_position = 0
                    return interface_reply(query.ENUM_INTERFACE, ENUM_IPID,
                                           self.enum_oid, oxid=self.enum_oxid)
                if interface == query.ENUM_INTERFACE:
                    self.enum_position += 1
                    if self.enum_position == 1:
                        return next_fixture(object_fixture(object_padding=self.object_padding))
                    return next_fixture(status=1)
                if interface == rpc.OBJECT_EXPORTER:
                    return ping_reply() if opnum == 2 else bytes(4)
                return bytes(12)

            channel.call.side_effect = call
            self.channels[interface] = channel
            try:
                yield channel
            finally:
                channel.active = False
                self.closed.append(interface)

        patcher = patch.object(rpc, "ntlm_connection", side_effect=connect)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(client_module, "discover_version", return_value=(5, 7))
        self.discovery = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = patch.object(client_module.time, "monotonic", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = client_module.DcomClient()
        self.addCleanup(self.close_client)

    def close_client(self):
        try:
            self.client.close()
        except RpcProbeError:
            pass

    def connect(self):
        self.client.connect("192.0.2.10", "synthetic", "synthetic")

    def releases(self):
        result = []
        for iid, opnum, stub, _, _ in self.calls:
            if iid == wire.REM_UNKNOWN and opnum == 5:
                count = struct.unpack_from("<H", stub, 32)[0]
                result.append([stub[40 + i * 24:56 + i * 24] for i in range(count)])
        return result


class PersistentClientTests(ClientFixture):
    def test_catalog_multirow_and_empty_results_release_enumerator(self):
        from remoteops.commands.wmi_catalog import make_query
        from test_wmi_values import instance_fixture

        self.connect()
        spec = make_query("services", {})
        rows = [{"Name": name, "DisplayName": name, "State": "Running", "StartMode": "Auto"}
                for name in ("Alpha", "Beta", "Gamma")]
        replies = iter([next_fixture(instance_fixture(spec.class_name, spec.properties, row))
                        for row in rows] + [next_fixture(status=1)])
        self.overrides[(query.ENUM_INTERFACE, 4)] = lambda: next(replies)
        self.assertEqual(self.client.query(spec), rows)
        self.assertEqual(self.releases()[-1], [ENUM_IPID.bytes_le])
        self.assertEqual(self.client.ping_oids, {456})
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(status=1)
        self.assertEqual(self.client.query(spec), [])
        self.assertTrue(self.client.ready)

    def test_query_limits_fail_without_partial_success(self):
        from remoteops.commands.wmi_catalog import QuerySpec

        self.connect()
        spec = QuerySpec("root/cimv2", "Win32_OperatingSystem", query.OS_PROPERTIES, max_rows=2)
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(object_fixture())
        with self.assertRaisesRegex(RpcProbeError, "exceeded 2 rows"):
            self.client.query(spec)
        self.assertFalse(self.client.closed)
        self.assertTrue(self.client.ready)
        self.assertEqual(self.releases()[-1], [ENUM_IPID.bytes_le])
        self.assertEqual(self.client.uncertain, [])
        self.overrides.clear()
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_exact_row_limit_and_aggregate_size(self):
        from remoteops.commands.wmi_catalog import QuerySpec

        self.connect()
        spec = QuerySpec("root/cimv2", "Win32_OperatingSystem", query.OS_PROPERTIES, max_rows=1)
        replies = iter([next_fixture(object_fixture()), next_fixture(status=1)])
        self.overrides[(query.ENUM_INTERFACE, 4)] = lambda: next(replies)
        self.assertEqual(self.client.query(spec), [EXPECTED])
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(object_fixture())
        with patch.object(client_module, "MAX_QUERY_BYTES", 8), self.assertRaisesRegex(RpcProbeError, "aggregate size"):
            self.client.query(spec)
        self.assertFalse(self.client.closed)
        self.assertEqual(self.client.uncertain, [])
        self.overrides.clear()
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_secondary_namespace_connect_does_not_query_os(self):
        self.client.connect("192.0.2.10", "synthetic", "synthetic", "root/standardcimv2", probe_os=False)
        self.assertTrue(self.client.ready)
        self.assertEqual(self.query_count, 0)
        self.assertEqual(self.client.namespace, "root\\standardcimv2")
        with self.assertRaisesRegex(ValueError, "require root/cimv2"):
            self.client.invoke('Win32_Service.Name="A"', "StopService", {}, 10)

    def test_method_response_and_lost_reply_are_not_retried(self):
        from test_wmi_methods import output_object, response

        self.connect()
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 24)] = response(output_object(5))
        self.assertEqual(self.client.invoke('Win32_Service.Name="A"', "StopService", {}), {"ReturnValue": 5})
        self.assertTrue(self.client.ready)
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 24)] = TimeoutError()
        before = len([call for call in self.calls if call[:2] == (wire.WBEM_SERVICES_INTERFACE, 24)])
        with self.assertRaisesRegex(RpcProbeError, "outcome unconfirmed; do not automatically retry"):
            self.client.invoke('Win32_Service.Name="A"', "StartService", {})
        after = len([call for call in self.calls if call[:2] == (wire.WBEM_SERVICES_INTERFACE, 24)])
        self.assertEqual(after - before, 1)
        self.assertTrue(self.client.closed)
        self.assertEqual(self.releases()[-1], [SERVICES_IPID.bytes_le])

    def test_method_hresult_preserves_session_without_unknown_outcome(self):
        from test_wmi_methods import response

        self.connect()
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 24)] = response(status=0x80041003)
        with self.assertRaisesRegex(RpcProbeError, "0x80041003") as caught:
            self.client.invoke('Win32_Service.Name="A"', "StopService", {})
        self.assertNotIn("outcome unconfirmed", str(caught.exception))
        self.assertTrue(self.client.ready)
        self.assertFalse(self.client.closed)
        self.assertEqual(self.client.uncertain, [])
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_by_value_method_decode_error_keeps_channel_without_replay(self):
        from test_wmi_methods import output_object, response
        from remoteops.remoting.wmi_query import WmiQueryError

        self.connect()
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 24)] = response(output_object(class_name="Different"))
        with self.assertRaisesRegex(WmiQueryError, "outcome unconfirmed"):
            self.client.invoke('Win32_Service.Name="A"', "StopService", {})
        self.assertEqual(len([call for call in self.calls if call[:2] == (wire.WBEM_SERVICES_INTERFACE, 24)]), 1)
        self.assertTrue(self.client.ready)
        self.assertFalse(self.client.closed)
        self.assertEqual(self.client.uncertain, [])
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_padded_results_connect_repeat_and_release_cleanly(self):
        self.object_padding = bytes(range(32))
        self.connect()
        self.assertTrue(self.client.ready)
        opened = list(self.opened)
        for _ in range(2):
            self.assertEqual(self.client.query_os(), [EXPECTED])
        self.assertEqual(self.opened, opened)
        self.assertEqual(self.client.uncertain, [])
        self.assertEqual(self.releases(), [[IPID.bytes_le], [ENUM_IPID.bytes_le],
                                          [ENUM_IPID.bytes_le], [ENUM_IPID.bytes_le]])
        self.client.close()
        self.assertEqual(self.releases()[-1], [SERVICES_IPID.bytes_le])
        self.assertEqual(self.client.uncertain, [])
        self.assertEqual(len(self.closed), len(self.opened))
        self.assertEqual(self.client.ping_oids, set())

    def test_repeated_queries_reuse_associations_and_services(self):
        self.connect()
        self.assertTrue(self.client.ready)
        self.assertEqual(self.query_count, 1)
        initial_connections = list(self.opened)
        self.assertEqual(self.client.query_os(), [EXPECTED])
        self.assertEqual(self.client.query_os(), [EXPECTED])
        self.assertEqual(self.opened, initial_connections)
        self.assertEqual(len(self.client.channels), 4)
        self.assertEqual(self.client.ping_oids, {456})
        self.assertEqual([ref.ipid for ref in self.client.references], [SERVICES_IPID])
        self.assertEqual(self.releases(), [[IPID.bytes_le], [ENUM_IPID.bytes_le],
                                          [ENUM_IPID.bytes_le], [ENUM_IPID.bytes_le]])
        self.client.close()
        self.assertEqual(self.releases()[-1], [SERVICES_IPID.bytes_le])
        self.assertEqual(len(self.opened), len(self.closed))
        self.assertEqual(self.client.ping_oids, set())
        self.assertFalse(self.client.ready)
        count = len(self.calls)
        self.client.close()
        self.assertEqual(len(self.calls), count)

    def test_idle_ping_has_no_query_or_new_authentication(self):
        self.connect()
        opened, queries, count = len(self.opened), self.query_count, len(self.calls)
        self.clock += 61
        self.client.maintain()
        self.assertEqual((len(self.opened), self.query_count), (opened, queries))
        self.assertEqual([(iid, op) for iid, op, *_ in self.calls[count:]],
                         [(rpc.OBJECT_EXPORTER, 1)])
        self.assertEqual(self.client.next_ping, self.clock + 60)

    def test_same_oid_is_not_deleted_while_another_interface_owns_it(self):
        self.enum_oid = 456
        self.connect()
        self.assertEqual(self.client.ping_oids, {456})
        complex_pings = [call for call in self.calls if call[:2] == (rpc.OBJECT_EXPORTER, 2)]
        self.assertEqual(len(complex_pings), 1)

    def test_noping_service_is_excluded_from_ping_set(self):
        self.service_flags = 0x1000
        self.connect()
        self.assertEqual(self.client.ping_oids, set())
        self.assertEqual(self.client.set_id, 0)
        self.client.query_os()
        creates = [call[2] for call in self.calls
                   if call[:2] == (rpc.OBJECT_EXPORTER, 2)
                   and struct.unpack_from("<Q", call[2])[0] == 0]
        self.assertEqual(len(creates), 2)
        self.assertTrue(all(struct.unpack_from("<H", stub, 8)[0] == 1 for stub in creates))

    def test_bad_configuration_is_local(self):
        for address, namespace in (("hostname", "root/cimv2"),
                                   ("192.0.2.10", "root/other")):
            with self.assertRaises(ValueError):
                self.client.connect(address, "synthetic", "synthetic", namespace)
        self.discovery.assert_not_called()
        self.assertEqual(self.opened, [])

    def test_query_error_loses_session_and_requires_new_client(self):
        self.connect()
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 20)] = TimeoutError()
        with self.assertRaisesRegex(RpcProbeError, "enumerator cleanup unconfirmed"):
            self.client.query_os()
        self.assertTrue(self.client.closed)
        self.assertFalse(self.client.ready)
        count = len(self.calls)
        with self.assertRaisesRegex(RpcProbeError, "reconnect explicitly"):
            self.client.query_os()
        self.assertEqual(len(self.calls), count)
        self.assertEqual(len(self.opened), len(self.closed))

    def test_query_hresult_preserves_session_without_unknown_enumerator(self):
        self.connect()
        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 20)] = bytes(12) + number(0x80041003)
        with self.assertRaisesRegex(RpcProbeError, "0x80041003") as caught:
            self.client.query_os()
        self.assertNotIn("enumerator cleanup unconfirmed", str(caught.exception))
        self.assertTrue(self.client.ready)
        self.assertFalse(self.client.closed)
        self.assertEqual(self.client.uncertain, [])
        self.overrides.clear()
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_prefix_query_stops_without_fetching_extra_row_and_releases(self):
        from remoteops.commands.wmi_catalog import QuerySpec

        self.connect()
        spec = QuerySpec("root/cimv2", "Win32_OperatingSystem", query.OS_PROPERTIES, max_rows=1, take=1)
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(object_fixture())
        before = len(self.calls)
        self.assertEqual(self.client.query(spec), [EXPECTED])
        calls = [call for call in self.calls[before:] if call[:2] == (query.ENUM_INTERFACE, 4)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.releases()[-1], [ENUM_IPID.bytes_le])
        self.assertEqual(self.client.ping_oids, {456})
        self.assertTrue(self.client.ready)

    def test_complete_result_errors_release_only_enumerator_then_allow_next_command(self):
        from test_wmi_values import instance_fixture

        self.connect()
        replies = (next_fixture(instance_fixture("Unrelated", {"Name": 8}, {"Name": "bad"})),
                   next_fixture(object_fixture(bad_type=True)),
                   next_fixture(status=0x80041003), next_fixture(status=0x40004))
        for reply in replies:
            with self.subTest(reply=reply[-12:]):
                self.overrides[(query.ENUM_INTERFACE, 4)] = reply
                with self.assertRaises(query.WmiQueryError) as caught:
                    self.client.query_os()
                self.assertNotIn("cleanup unconfirmed", str(caught.exception))
                self.assertEqual(self.releases()[-1], [ENUM_IPID.bytes_le])
                self.assertEqual(self.client.uncertain, [])
                self.assertTrue(self.client.ready)
                self.overrides.clear()
                self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_recoverable_error_with_failed_cleanup_still_loses_session(self):
        self.connect()
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(object_fixture(bad_type=True))
        self.overrides[(wire.REM_UNKNOWN, 5)] = TimeoutError()
        with self.assertRaisesRegex(RpcProbeError, "release unconfirmed"):
            self.client.query_os()
        self.assertTrue(self.client.closed)

    def test_service_subclass_works_through_persistent_client(self):
        from remoteops.commands.wmi_catalog import make_query
        from test_wmi_values import instance_fixture

        self.connect()
        spec = make_query("services", {})
        row = {"Name": "TermService", "DisplayName": "Remote Desktop", "State": "Running", "StartMode": "Manual"}
        obj = instance_fixture("Win32_TerminalService", spec.properties, row, ancestors=("Win32_Service",))
        replies = iter([next_fixture(obj), next_fixture(status=1)])
        self.overrides[(query.ENUM_INTERFACE, 4)] = lambda: next(replies)
        self.assertEqual(self.client.query(spec), [row])
        self.overrides.clear()
        self.assertEqual(self.client.query_os(), [EXPECTED])

    def test_events_five_from_large_log_through_real_session_and_wire_codecs(self):
        import re
        from remoteops.commands.templates import prepare_command
        from remoteops.commands.wmi_catalog import WMI_CATALOG
        from remoteops.core.models import Host, SessionState
        from remoteops.remoting.dcom_session import DcomWmiSession
        from test_wmi_values import instance_fixture

        self.connect()
        opened = list(self.opened)
        pending = iter(())
        downloaded = []

        def exec_query():
            nonlocal pending
            stub = self.calls[-1][2]
            size = struct.unpack_from("<I", stub, 64)[0]
            wql = stub[72:72 + size].decode("utf-16-le")
            lower, upper = map(int, re.search(r"RecordNumber>=(\d+) AND RecordNumber<=(\d+)$", wql).groups())
            numbers = range(max(100000, lower), min(120049, upper) + 1)
            properties = ({"RecordNumber": 19} if wql.startswith("SELECT RecordNumber FROM")
                          else WMI_CATALOG["events"].properties)

            def replies():
                for number in numbers:
                    values = {name: None for name in properties}
                    values["RecordNumber"] = number
                    downloaded.append((number, len(properties)))
                    yield next_fixture(instance_fixture("Win32_NTLogEvent", properties, values))
                yield next_fixture(status=1)
            pending = replies()
            return interface_reply(query.ENUM_INTERFACE, ENUM_IPID, self.enum_oid)

        self.overrides[(wire.WBEM_SERVICES_INTERFACE, 20)] = exec_query
        self.overrides[(query.ENUM_INTERFACE, 4)] = lambda: next(pending)
        session = DcomWmiSession(Host("fixture", "192.0.2.10"), None)
        session.client = self.client
        session.namespace_clients["root/cimv2"] = self.client
        session.state = SessionState.CONNECTED
        result = session.run_prepared(prepare_command("events System 5"))
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual([row["RecordNumber"] for row in result.records], [120049, 120048, 120047, 120046, 120045])
        self.assertEqual(len([row for row in downloaded if row[1] > 1]), 5)
        self.assertLess(len(downloaded), 40)
        self.assertEqual(self.client.ping_oids, {456})
        self.assertEqual(self.client.uncertain, [])
        self.assertEqual(session.state, SessionState.CONNECTED)
        self.overrides.clear()
        self.assertEqual(session.run_prepared(prepare_command("sysinfo")).exit_code, 0)
        self.assertEqual(self.opened, opened)

    def test_malformed_result_releases_enumerator_and_services(self):
        self.connect()
        self.overrides[(query.ENUM_INTERFACE, 4)] = b"bad"
        with self.assertRaisesRegex(RpcProbeError, "result object cleanup unconfirmed"):
            self.client.query_os()
        self.assertEqual(self.releases()[-1], [SERVICES_IPID.bytes_le, ENUM_IPID.bytes_le])

    def test_cross_exporter_reference_is_not_released_through_wrong_exporter(self):
        self.connect()
        self.enum_oxid = 1234
        with self.assertRaisesRegex(RpcProbeError, "cross-exporter reference cleanup unconfirmed"):
            self.client.query_os()
        self.assertEqual(self.releases()[-1], [SERVICES_IPID.bytes_le])

    def test_failed_release_is_not_repeated(self):
        self.connect()
        self.overrides[(wire.REM_UNKNOWN, 5)] = TimeoutError()
        before = len(self.releases())
        with self.assertRaisesRegex(RpcProbeError, "release unconfirmed"):
            self.client.query_os()
        self.assertEqual(self.releases()[before:], [[ENUM_IPID.bytes_le], [SERVICES_IPID.bytes_le]])
        self.client.close()
        self.assertEqual(len(self.releases()), before + 2)

    def test_idle_failure_closes_without_reconnect_and_redacts_provider_error(self):
        self.connect()
        self.overrides[(rpc.OBJECT_EXPORTER, 1)] = BadMICError(context_msg="synthetic-secret")
        count = len(self.opened)
        with self.assertRaisesRegex(RpcProbeError, "BadMICError") as caught:
            self.client.maintain()
        self.assertNotIn("synthetic-secret", str(caught.exception))
        self.assertEqual(len(self.opened), count)
        self.assertEqual(len(self.closed), count)

    def test_failed_initial_query_never_sets_ready(self):
        self.overrides[(query.ENUM_INTERFACE, 4)] = next_fixture(status=1)
        with self.assertRaisesRegex(RpcProbeError, "no operating-system instance"):
            self.connect()
        self.assertFalse(self.client.ready)
        self.assertTrue(self.client.closed)
        self.assertEqual(len(self.closed), len(self.opened))

    def test_lost_initial_ping_set_is_reported_uncertain(self):
        self.overrides[(rpc.OBJECT_EXPORTER, 2)] = TimeoutError()
        with self.assertRaisesRegex(RpcProbeError, "ping-set update outcome unconfirmed"):
            self.connect()
        self.assertFalse(self.client.ready)
        self.assertEqual(len(self.closed), len(self.opened))

    def test_uncertain_ping_update_consumes_sequence_before_cleanup(self):
        self.connect()
        self.overrides[(rpc.OBJECT_EXPORTER, 2)] = b"malformed"
        before = len(self.calls)
        with self.assertRaisesRegex(RpcProbeError, "ping-set update outcome unconfirmed"):
            self.client.query_os()
        sequences = [struct.unpack_from("<H", stub, 8)[0]
                     for iid, op, stub, *_ in self.calls[before:]
                     if (iid, op) == (rpc.OBJECT_EXPORTER, 2)]
        self.assertEqual(len(sequences), 2)
        self.assertEqual(sequences[1], sequences[0] + 1)

    def test_sequence_exhaustion_fails_explicitly(self):
        self.connect()
        self.client.sequence = 65536
        with self.assertRaisesRegex(RpcProbeError, "sequence exhausted"):
            self.client.query_os()
        self.assertTrue(self.client.closed)

    def test_cross_thread_use_never_dispatches(self):
        self.connect()
        errors = []
        before = len(self.calls)

        def wrong_thread():
            try:
                self.client.query_os()
            except RpcProbeError as exc:
                errors.append(str(exc))

        thread = threading.Thread(target=wrong_thread)
        thread.start()
        thread.join(2)
        self.assertEqual(len(self.calls), before)
        self.assertIn("owning host worker", errors[0])

    def test_query_budget_is_bounded_and_shared(self):
        self.connect()
        before = len(self.calls)
        self.client.query_os(120)
        self.assertEqual({call[3] for call in self.calls[before:]}, {160.0})
