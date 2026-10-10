import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

from remoteops.commands.templates import prepare_command
from remoteops.core.models import CredentialProfile, Host, SessionState
from remoteops.remoting.session import RemoteSessionError
from remoteops.remoting.wmi import NativeWmiClient, WmiSession, parse_wmi_date


class WmiTests(unittest.TestCase):
    def test_native_prefix_stops_without_consuming_remaining_objects(self):
        from remoteops.commands.wmi_catalog import QuerySpec

        client = NativeWmiClient.__new__(NativeWmiClient)
        def objects():
            yield SimpleNamespace(Properties_=[SimpleNamespace(Name="RecordNumber", Value=12345)])
            raise AssertionError("Prefix query fetched another row")
        client.services = Mock()
        client.services.ExecQuery.return_value = objects()
        spec = QuerySpec("root/cimv2", "Win32_NTLogEvent", {"RecordNumber": 19}, max_rows=1, take=1)
        with patch("remoteops.remoting.wmi.com_timeout", return_value=nullcontext()):
            self.assertEqual(client.query_spec(spec, 10), [{"RecordNumber": 12345}])

    def test_native_provider_failure_keeps_session_for_next_command(self):
        class ProviderError(Exception):
            hresult = -2147352567
            excepinfo = (0, None, "Access denied", None, 0, -2147217405)

        client = Mock()
        with patch.object(WmiSession, "client_factory", return_value=client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            self.addCleanup(session.disconnect)
            client.query_spec.side_effect = ProviderError("denied")
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 1)
            self.assertEqual(session.state, SessionState.CONNECTED)
            client.query_spec.side_effect = None
            client.query_spec.return_value = [{"Name": "Spooler", "State": "Running"}]
            client.invoke.side_effect = ProviderError("denied")
            self.assertEqual(session.run_prepared(prepare_command("restart-service Spooler")).exit_code, 1)
            self.assertEqual(session.state, SessionState.CONNECTED)
            client.invoke.assert_called_once()
            client.query_spec.return_value = []
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 0)
            self.assertIs(session.client, client)

    def test_native_namespaces_are_cached_and_all_closed(self):
        clients = []

        class Client:
            def __init__(self, host, profile, credential, **kwargs):
                self.namespace = host.wmi_namespace
                self.closed = False
                clients.append(self)
            def query(self, *args):
                return []
            def query_spec(self, *args):
                return []
            def invoke(self, *args):
                return {"ReturnValue": 2}
            def close(self):
                self.closed = True

        with patch.object(WmiSession, "client_factory", Client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            for alias in ("ports", "firewall", "tasks", "pending-reboot"):
                result = session.run_prepared(prepare_command(alias))
                self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertEqual(len(clients), 4)
            session.disconnect()
            self.assertTrue(all(client.closed for client in clients))
            self.assertIsNone(session.credential)

    def test_native_local_processing_and_provider_health_errors_keep_session(self):
        class ProviderError(Exception):
            hresult = 0x80041003

        client = Mock()
        client.query_spec.return_value = []
        with patch.object(WmiSession, "client_factory", return_value=client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            self.addCleanup(session.disconnect)
            with patch("remoteops.remoting.wmi.records_text", side_effect=TypeError("local formatting")):
                self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 1)
            client.query.side_effect = ProviderError("query denied")
            session.health_check()
            self.assertEqual(session.state, SessionState.CONNECTED)
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 0)
            client.close.assert_not_called()

    def test_native_access_denied_hresult_does_not_break_connection(self):
        class AccessDenied(Exception):
            hresult = -2147024891

        client = Mock()
        with patch.object(WmiSession, "client_factory", return_value=client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            self.addCleanup(session.disconnect)
            client.query_spec.side_effect = AccessDenied("denied")
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 1)
            self.assertEqual(session.state, SessionState.CONNECTED)
            client.query_spec.side_effect = None
            client.query_spec.return_value = []
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 0)

    def test_native_auxiliary_transport_failure_preserves_primary(self):
        primary, auxiliary = Mock(), Mock()
        primary.query_spec.return_value = []
        auxiliary.query_spec.side_effect = RuntimeError("lost association")
        with patch.object(WmiSession, "client_factory", side_effect=[primary, auxiliary]):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            self.addCleanup(session.disconnect)
            result = session.run_prepared(prepare_command("ports"))
            self.assertEqual(result.exit_code, 1)
            self.assertIn("primary session retained", result.stderr)
            self.assertEqual(session.state, SessionState.CONNECTED)
            self.assertEqual(session.namespace_clients, {"root/cimv2": primary})
            auxiliary.close.assert_called_once()
            primary.close.assert_not_called()
            self.assertEqual(session.run_prepared(prepare_command("services")).exit_code, 0)

    def test_native_method_failure_is_uncertain_and_not_retried(self):
        client = Mock()
        client.query_spec.return_value = [{"Name": "Spooler", "State": "Running"}]
        client.invoke.side_effect = RuntimeError("lost reply")
        with patch.object(WmiSession, "client_factory", return_value=client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            result = session.run_prepared(prepare_command("restart-service Spooler"))
            self.assertEqual(result.exit_code, 1)
            self.assertIn("outcome unconfirmed", result.stderr)
            self.assertIn("StopService", result.command)
            self.assertEqual(session.state, SessionState.ERROR)
            client.invoke.assert_called_once()
            session.disconnect()

    def test_native_uint64_normalization_and_missing_property(self):
        from remoteops.commands.wmi_catalog import make_query

        client = NativeWmiClient.__new__(NativeWmiClient)
        client.query = Mock(return_value=[{"DeviceID": "C:", "VolumeName": None,
                                          "FreeSpace": str(2**63), "Size": str(2**64 - 1)}])
        rows = client.query_spec(make_query("disk", {}), 10)
        self.assertEqual(rows[0]["FreeSpace"], 2**63)
        self.assertEqual(rows[0]["Size"], 2**64 - 1)
        client.query.return_value = [{"DeviceID": "C:"}]
        with self.assertRaisesRegex(RemoteSessionError, "omitted"):
            client.query_spec(make_query("disk", {}), 10)

    def test_native_queries_return_tables_without_process_creation(self):
        class Client:
            queries = []
            def __init__(self, *args):
                pass
            def query(self, query, timeout):
                self.queries.append(query)
                return [{"DeviceID": "C:", "VolumeName": "System", "FreeSpace": 1073741824, "Size": 2147483648}]
            def query_spec(self, spec, timeout):
                return self.query(spec.wql, timeout)
            def close(self):
                pass

        with patch.object(WmiSession, "client_factory", Client):
            session = WmiSession(Host("test", "example"), CredentialProfile("ops", "user"))
            session.connect()
            result = session.run_prepared(prepare_command("disk"))
            self.assertEqual(result.records[0]["FreeGB"], 1)
            self.assertEqual(session.state, SessionState.CONNECTED)
            self.assertTrue(all(query.startswith("SELECT ") for query in Client.queries))
            with self.assertRaises(RemoteSessionError):
                session.run("Get-WinEvent")
            session.disconnect()

    def test_linux_failure_is_explicit_and_does_not_fallback(self):
        with patch("remoteops.remoting.wmi.sys.platform", "linux"):
            with self.assertRaisesRegex(RemoteSessionError, "No SMB or WinRM fallback"):
                NativeWmiClient(Host("test", "example"), CredentialProfile("ops", "user"), None)

    def test_wmi_datetime_preserves_offset(self):
        value = parse_wmi_date("20260918080000.000000-240")
        self.assertEqual(value.utcoffset().total_seconds(), -14400)
        self.assertIsNone(parse_wmi_date("unknown"))
