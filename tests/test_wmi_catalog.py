import re
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from remoteops.commands import wmi_catalog as catalog
from remoteops.commands.templates import BUILTIN_TEMPLATES, command_help, prepare_command
from remoteops.remoting.wmi_methods import method_request
from remoteops.remoting.wmi_query import decode_instance
from test_dcom import CID
from test_wmi_values import instance_fixture


def provider_values(spec):
    values = {}
    for name, kind in spec.properties.items():
        if kind & 0x2000:
            value = ["192.0.2.1", "2001:db8::1"]
        elif kind in (8, 102):
            value = "fixture"
        elif kind == 101:
            value = "20260922090000.000000+000"
        elif kind == 11:
            value = False
        else:
            value = 1
        values[name] = value
    return values


class CatalogTests(unittest.TestCase):
    def test_every_builtin_alias_has_handler_and_help(self):
        self.assertEqual(catalog.WMI_ALIASES, {item.alias for item in BUILTIN_TEMPLATES})
        self.assertEqual(len(catalog.WMI_ALIASES), 21)
        for template in BUILTIN_TEMPLATES:
            with self.subTest(alias=template.alias):
                self.assertTrue(prepare_command(template.alias).native_wmi)
                self.assertNotIn("WMI behavior:", command_help(template))
                self.assertIn(template.alias, catalog.WMI_NOTES)

    def test_all_simple_queries_decode_types_and_execute(self):
        for alias, spec in catalog.WMI_CATALOG.items():
            if spec is None or alias == "events":
                continue
            queries = []

            def query(compiled, timeout):
                queries.append(compiled)
                self.assertGreater(timeout, 0)
                values = provider_values(compiled)
                obj = instance_fixture(compiled.class_name, compiled.properties, values)
                return [decode_instance(obj, compiled.class_name, compiled.properties)]

            with self.subTest(alias=alias):
                method = Mock(side_effect=AssertionError("Unexpected method"))
                records, trace = catalog.execute_command(alias, {}, query, method, 10)
                self.assertEqual(len(records), 1)
                self.assertEqual(len(queries), 1)
                self.assertIn(spec.class_name, trace)
                method.assert_not_called()

    def test_wql_literal_escaping_and_exact_filter(self):
        self.assertEqual(catalog.wql_literal("O'Brien\\x"), "'O\\'Brien\\\\x'")
        self.assertEqual(catalog.wql_literal(True), "TRUE")
        self.assertEqual(catalog.wql_literal(42), "42")
        self.assertEqual(catalog.wql_literal(None), "NULL")
        value = "x' OR Name='other"
        spec = catalog.make_query("svc", {"service_name": value})
        self.assertEqual(spec.where, "Name=" + catalog.wql_literal(value))
        for value in ("\0", "a\nb", "\ud800", float("nan"), []):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                catalog.wql_literal(value)

    def test_invalid_definitions_and_values_never_dispatch(self):
        spec = catalog.make_query("services", {})
        for changes in ({"class_name": "Bad;Class"}, {"namespace": "//other/root/cimv2"},
                        {"properties": {}}, {"properties": {"Value": 13}},
                        {"properties": {"A": 8, "a": 8}}, {"max_rows": 0}, {"max_rows": 10001},
                        {"take": 0}, {"take": True}, {"max_rows": 1, "take": 2}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(spec, **changes)
        query, method = Mock(), Mock()
        for alias, values in (("services", {"unknown": "x"}), ("svc", {"service_name": ""}),
                              ("events", {"count": -1}), ("topcpu", {"count": True}),
                              ("not-a-command", {})):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                catalog.execute_command(alias, values, query, method, 10)
        query.assert_not_called()
        method.assert_not_called()

    def test_disk_unknowns_and_cpu_sorting(self):
        query = Mock(return_value=[{"DeviceID": "C:", "FreeSpace": None, "Size": 2**30}])
        rows, _ = catalog.execute_command("disk", {}, query, Mock(), 10)
        self.assertIsNone(rows[0]["FreeGB"])
        self.assertEqual(rows[0]["SizeGB"], 1)
        query.return_value = [
            {"Name": "unknown", "KernelModeTime": None, "UserModeTime": 1},
            {"Name": "low", "KernelModeTime": "10000000", "UserModeTime": "10000000"},
            {"Name": "high", "KernelModeTime": 90000000, "UserModeTime": 10000000},
        ]
        rows, _ = catalog.execute_command("topcpu", {"count": 2}, query, Mock(), 10)
        self.assertEqual([row["Name"] for row in rows], ["high", "low"])
        self.assertEqual([row["CPU"] for row in rows], [10, 2])

    def event_provider(self, numbers):
        def query(spec, timeout):
            self.assertGreater(timeout, 0)
            match = re.search(r"RecordNumber>=(\d+) AND RecordNumber<=(\d+)$", spec.where)
            self.assertIsNotNone(match, spec.wql)
            lower, upper = map(int, match.groups())
            selected = [value for value in numbers if lower <= value <= upper]
            if spec.take is not None:
                selected = selected[:spec.take]
            self.assertLessEqual(len(selected), spec.max_rows)
            rows = []
            for number in selected:
                values = provider_values(spec)
                values["RecordNumber"] = number
                obj = instance_fixture(spec.class_name, spec.properties, values)
                rows.append(decode_instance(obj, spec.class_name, spec.properties))
            return rows
        return Mock(side_effect=query)

    def test_events_large_logs_return_requested_count_without_full_enumeration(self):
        for count in (1, 5, 50):
            for reverse in (False, True):
                with self.subTest(count=count, reverse=reverse):
                    query = self.event_provider(sorted(range(100000, 120050), reverse=reverse))
                    rows, _ = catalog.execute_command("events", {"log": "Application", "count": count},
                                                       query, Mock(), 10)
                    self.assertEqual([row["RecordNumber"] for row in rows],
                                     list(range(120049, 120049 - count, -1)))
                    full_queries = [call.args[0] for call in query.call_args_list if call.args[0].take is None]
                    self.assertEqual(len(full_queries), 1)
                    self.assertEqual(full_queries[0].max_rows, count)
                    self.assertLessEqual(query.call_count, 35)
                    self.assertTrue(all("Logfile='Application' AND " in call.args[0].where
                                        for call in query.call_args_list))
                    if reverse:
                        self.assertEqual(query.call_count, 3)

    def test_events_sparse_unordered_empty_and_fewer_than_requested(self):
        for numbers, count in (([3, 10, 7], 2), ([7, 3, 1000000, 9, 42], 10),
                               ([], 5), ([0], 5), ([0xFFFFFFFE, 0xFFFFFFFF], 1)):
            with self.subTest(numbers=numbers):
                rows, _ = catalog.execute_command("events", {"count": count},
                                                   self.event_provider(numbers), Mock(), 10)
                self.assertEqual([row["RecordNumber"] for row in rows], sorted(numbers, reverse=True)[:count])

    def test_events_log_clear_or_invalid_provider_data_never_returns_partial_success(self):
        for batches, message in (([[{"RecordNumber": 100}], [], []], "changed during retrieval"),
                                 ([[{"RecordNumber": 100}], [{"RecordNumber": 50}]], "outside"),
                                 ([[{"RecordNumber": 2**32}]], "uint32"),
                                 ([[{"RecordNumber": 100}], [], [{"RecordNumber": 100}] * 2], "duplicate")):
            with self.subTest(message=message), self.assertRaisesRegex(catalog.WmiCommandError, message):
                catalog.execute_command("events", {"count": 5}, Mock(side_effect=batches), Mock(), 10)
        query = Mock(side_effect=RuntimeError("provider denied"))
        with self.assertRaisesRegex(RuntimeError, "provider denied"):
            catalog.execute_command("events", {}, query, Mock(), 10)

    def test_events_log_value_is_escaped_on_every_query(self):
        query = self.event_provider([50, 49])
        log = "App' OR Logfile='System"
        catalog.execute_command("events", {"log": log, "count": 1}, query, Mock(), 10)
        for call in query.call_args_list:
            self.assertTrue(call.args[0].where.startswith("Logfile=" + catalog.wql_literal(log) + " AND "))

    def test_events_aggregate_limit_across_ranges(self):
        with patch.object(catalog, "MAX_EVENT_BYTES", 1), self.assertRaisesRegex(catalog.WmiCommandError, "aggregate size"):
            catalog.execute_command("events", {"count": 5}, self.event_provider([100, 1]), Mock(), 10)

    def test_empty_results_and_no_such_service(self):
        self.assertEqual(catalog.execute_command("services", {}, Mock(return_value=[]), Mock(), 10)[0], [])
        with self.assertRaisesRegex(catalog.WmiCommandError, "not found"):
            catalog.execute_command("svc", {}, Mock(return_value=[]), Mock(), 10)

    def test_admins_resolves_localized_group_by_sid(self):
        query = Mock(side_effect=[
            [{"Name": "Administratoren", "Domain": "LAB", "SID": "S-1-5-32-544"}],
            [{"GroupComponent": "group", "PartComponent": "member"}],
        ])
        rows, _ = catalog.execute_command("admins", {}, query, Mock(), 10)
        self.assertIn("S-1-5-32-544", query.call_args_list[0].args[0].where)
        self.assertIn("Administratoren", query.call_args_list[1].args[0].where)
        self.assertEqual(rows[0]["PartComponent"], "member")

    def test_registry_results_do_not_hide_access_denial(self):
        method = Mock(side_effect=[{"ReturnValue": 0}, {"ReturnValue": 2}])
        rows, trace = catalog.execute_command("pending-reboot", {}, Mock(), method, 10)
        self.assertEqual([row["Exists"] for row in rows], [True, False])
        self.assertIn("EnumKey", trace)
        for call in method.call_args_list:
            namespace, path, name, inputs, timeout = call.args
            self.assertEqual(namespace, "root/default")
            self.assertEqual(inputs["hDefKey"], (19, 0x80000002))
            method_request((5, 7), CID, path, name, inputs)
        with self.assertRaisesRegex(catalog.WmiCommandError, "ReturnValue=5"):
            catalog.execute_command("pending-reboot", {}, Mock(), Mock(return_value={"ReturnValue": 5}), 10)

    def test_restart_waits_between_methods_and_never_retries_failure(self):
        rows = [{"Name": "O'Brien", "State": state} for state in
                ("Running", "Stop Pending", "Stopped", "Start Pending", "Running")]
        query = Mock(side_effect=[[row] for row in rows])
        method = Mock(return_value={"ReturnValue": 0})
        with patch.object(catalog.time, "sleep"):
            result, _ = catalog.execute_command("restart-service", {"service_name": "O'Brien"}, query, method, 10)
        self.assertEqual(result[0]["State"], "Running")
        self.assertEqual([call.args[2] for call in method.call_args_list], ["StopService", "StartService"])
        for call in method.call_args_list:
            _, path, name, inputs, _ = call.args
            method_request((5, 7), CID, path, name, inputs)
        for error in (TimeoutError("lost response"), catalog.WmiCommandError("provider denied")):
            method = Mock(side_effect=error)
            with self.assertRaises(type(error)):
                catalog.execute_command("restart-service", {}, Mock(return_value=[{"State": "Running"}]), method, 10)
            method.assert_called_once()

    def test_stopped_service_only_starts_and_provider_failure_is_explicit(self):
        query = Mock(side_effect=[[{"State": "Stopped"}], [{"State": "Running"}]])
        method = Mock(return_value={"ReturnValue": 0})
        catalog.execute_command("restart-service", {}, query, method, 10)
        self.assertEqual(method.call_args.args[2], "StartService")
        method = Mock(return_value={"ReturnValue": 3})
        with self.assertRaisesRegex(catalog.WmiCommandError, "no force"):
            catalog.execute_command("restart-service", {}, Mock(return_value=[{"State": "Running"}]), method, 10)
        method.assert_called_once()

    def test_timeout_bounds_and_no_silent_partial_records(self):
        for timeout in (0, -1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                catalog.execute_command("services", {}, Mock(), Mock(), timeout)
        query = Mock(return_value=[{}] * 10001)
        with self.assertRaisesRegex(catalog.WmiCommandError, "row cap"):
            catalog.execute_command("services", {}, query, Mock(), 10)
        with patch.object(catalog.time, "monotonic", side_effect=[0, 1, 11]):
            with self.assertRaises(TimeoutError):
                catalog.execute_command("services", {}, Mock(return_value=[]), Mock(), 10)
