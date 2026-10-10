import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from remoteops.commands.templates import (
    BUILTIN_TEMPLATES, CommandRegistry, CommandValidationError, _template_to_dict,
    argument_schema, command_backends, prepare_command, validate_template,
)
from remoteops.commands.wmi_definitions import compile_definition
from remoteops.core.credentials import ResolvedCredential
from remoteops.core.database import Repository
from remoteops.core.models import AuthMethod, CommandTemplate, ConnectionMode, CredentialProfile, Host, SessionState
from remoteops.remoting.dcom_session import DcomWmiSession
from remoteops.remoting.session_manager import SessionManager
from remoteops.remoting.wmi import WmiSession
from remoteops.remoting.wmi_query import decode_instance
from test_dcom_session import MemoryDcomClient
from test_wmi_values import instance_fixture


EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "wmi"


def bios_template():
    return CommandRegistry._parse((EXAMPLES / "bios.json").read_bytes())[1][0]


class DefinitionTests(unittest.TestCase):
    def test_examples_compile_decode_and_describe_real_backend(self):
        for path in EXAMPLES.glob("*.json"):
            for template in CommandRegistry._parse(path.read_bytes())[1]:
                with self.subTest(path=path.name):
                    prepared = prepare_command(template.alias, [template])
                    self.assertTrue(prepared.native_wmi)
                    self.assertEqual(command_backends(template), ["WMI/DCOM"])
                    spec = prepared.wmi_plan.query
                    row = dict.fromkeys(spec.properties, "fixture")
                    self.assertEqual(decode_instance(instance_fixture(spec.class_name, spec.properties, row),
                                                     spec.class_name, spec.properties), row)
                    help_text = prepare_command(template.alias + " -h", [template]).help_text
                    self.assertIn("Backends: WMI/DCOM", help_text)
                    self.assertIn("WMI limits:", help_text)

    def test_filter_binding_escapes_values_and_keeps_query_structure(self):
        template = CommandRegistry._parse((EXAMPLES / "service-status.json").read_bytes())[1][0]
        prepared = prepare_command('my-service "Bob\'s \\service"', [template])
        self.assertEqual(prepared.wmi_plan.query.where, "Name='Bob\\'s \\\\service'")
        self.assertEqual(prepared.values, {"service_name": "Bob's \\service"})

    def test_typed_filters_and_null_operators(self):
        template = bios_template()
        template.parameters = {"flag": False, "number": 5, "fraction": 1.5}
        template.wmi.update(properties={"Flag": "boolean", "Number": "uint32", "Fraction": "real64", "Text": "string"},
                            columns=["Number", "Flag"], filters=[
                                {"property": "Flag", "operator": "eq", "parameter": "flag"},
                                {"property": "Number", "operator": "ge", "parameter": "number"},
                                {"property": "Fraction", "operator": "lt", "parameter": "fraction"},
                                {"property": "Text", "operator": "is_null"},
                            ])
        prepared = prepare_command("bios --flag true --number 9", [template])
        self.assertEqual(prepared.wmi_plan.query.where, "Flag=TRUE AND Number>=9 AND Fraction<1.5 AND Text IS NULL")
        self.assertEqual(prepared.wmi_plan.columns, ("Number", "Flag"))
        for args in ("--number -1", "--number 4294967296", "--fraction nan", "--flag maybe"):
            with self.subTest(args=args), self.assertRaises(ValueError):
                prepare_command("bios " + args, [template])

    def test_all_comparison_operators_are_compiled_not_interpolated(self):
        from remoteops.commands.wmi_definitions import OPERATORS
        for name, operator in OPERATORS.items():
            template = bios_template()
            template.wmi["filters"] = [{"property": "Manufacturer", "operator": name, "value": "A"}]
            self.assertEqual(prepare_command("bios", [template]).wmi_plan.query.where, f"Manufacturer{operator}'A'")

    def test_unsupported_or_malformed_definitions_fail_locally(self):
        changes = [{"operation": "method"}, {"operation": "query;DELETE"}, {"namespace": "root/default"},
                   {"namespace": None}, {"class": "Win32_BIOS; DELETE"}, {"properties": ["Manufacturer"]},
                   {"properties": {"Name": "object"}}, {"properties": {"Name": "string[]"}},
                   {"properties": {"Name": "string", "name": "string"}}, {"limits": {"rows": True}},
                   {"limits": {"rows": 10001}}, {"limits": {"timeout_seconds": 61}}, {"columns": ["Unknown"]},
                   {"wql": "SELECT * FROM Win32_BIOS"}, {"filters": [{"property": "Manufacturer", "operator": "LIKE", "value": "%"}]},
                   {"filters": [{"property": "Manufacturer", "operator": "eq", "parameter": "missing"}]},
                   {"filters": [{"property": "Manufacturer", "operator": "eq", "value": None}]},
                   {"filters": [{"property": "Manufacturer", "operator": "is_null", "value": None}]}]
        for change in changes:
            template = bios_template()
            template.wmi.update(change)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_template(template)

    def test_prepared_plan_is_detached_from_definition_and_builtin_override_is_explicit(self):
        template = bios_template()
        prepared = prepare_command("bios", [template])
        template.wmi["properties"].clear()
        self.assertEqual(len(prepared.wmi_plan.query.properties), 3)
        builtin = deepcopy(BUILTIN_TEMPLATES[0])
        builtin.script += "\n# custom"
        self.assertFalse(prepare_command("sysinfo", [builtin]).native_wmi)
        builtin.wmi = bios_template().wmi
        self.assertEqual(prepare_command("sysinfo", [builtin]).wmi_plan.query.class_name, "Win32_BIOS")

    def test_invalid_defaults_and_required_parameter_preview(self):
        template = CommandRegistry._parse((EXAMPLES / "service-status.json").read_bytes())[1][0]
        template.parameters = {"_schema": {"service_name": {"type": "str", "required": True}}}
        validate_template(template)
        self.assertIn("required", prepare_command("my-service -h", [template]).help_text)
        with self.assertRaisesRegex(ValueError, "Missing required"):
            prepare_command("my-service", [template])
        self.assertEqual(prepare_command("my-service --service-name Spooler", [template]).wmi_plan.query.where, "Name='Spooler'")
        for parameters in ({"service_name": 4}, {"service_name": "a\nb"},
                           {"service_name": "x", "count": "bad"}, {"service_name": "x", "fraction": float("nan")}):
            template.parameters = parameters
            with self.subTest(parameters=parameters), self.assertRaises(ValueError):
                validate_template(template)


class LibraryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "commands.json"

    def test_v1_backup_preserves_all_fields_and_modified_builtin(self):
        template = deepcopy(BUILTIN_TEMPLATES[0])
        template.script += "\n# customized"
        original = json.dumps({"version": 1, "commands": [_template_to_dict(template)]}).encode()
        self.path.write_bytes(original)
        registry = CommandRegistry(self.path)
        self.assertEqual(self.path.with_name("commands.json.v1.bak").read_bytes(), original)
        self.assertEqual(json.loads(self.path.read_bytes())["version"], 2)
        self.assertEqual(registry.templates[0], template)
        self.assertFalse(registry.prepare("sysinfo").native_wmi)
        registry.load()
        self.assertEqual(self.path.with_name("commands.json.v1.bak").read_bytes(), original)

    def test_invalid_load_import_and_failed_save_preserve_active_state(self):
        registry = CommandRegistry(self.path)
        before = self.path.read_bytes()
        aliases = list(registry.aliases())
        for version in (3, True, "2"):
            self.path.write_text(json.dumps({"version": version, "commands": []}))
            with self.assertRaises(ValueError):
                registry.load()
            self.assertEqual(list(registry.aliases()), aliases)
        self.path.write_bytes(before)
        template = bios_template()
        with patch("remoteops.commands.templates.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                registry.upsert(template)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(registry.aliases()), aliases)
        registry.import_file(EXAMPLES / "bios.json")
        saved = self.path.read_bytes()
        with self.assertRaises(ValueError):
            registry.import_file(EXAMPLES / "bios.json")
        self.assertEqual(self.path.read_bytes(), saved)
        self.assertIn("bios", registry.aliases())

    def test_conflicting_migration_backup_does_not_overwrite_anything(self):
        original = json.dumps({"version": 1, "commands": []}).encode()
        self.path.write_bytes(original)
        backup = self.path.with_name("commands.json.v1.bak")
        backup.write_bytes(b"other backup")
        with self.assertRaisesRegex(ValueError, "different data"):
            CommandRegistry(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b"other backup")

    def test_failed_migration_write_leaves_original_and_backup(self):
        original = json.dumps({"version": 1, "commands": [_template_to_dict(t) for t in BUILTIN_TEMPLATES]}).encode()
        self.path.write_bytes(original)
        with patch("remoteops.commands.templates.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                CommandRegistry(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(self.path.with_name("commands.json.v1.bak").read_bytes(), original)
        registry = CommandRegistry(self.path)
        self.assertTrue(registry.prepare("sysinfo").native_wmi)
        self.assertEqual(len(registry.templates), len(BUILTIN_TEMPLATES))

    def test_export_round_trip_and_external_reload(self):
        registry = CommandRegistry(self.path)
        registry.import_file(EXAMPLES / "bios.json")
        template = registry.aliases()["bios"]
        exported = self.path.with_name("export.json")
        registry.export_file(exported, [template])
        self.assertEqual(CommandRegistry(exported).templates, [template])
        template.wmi["limits"]["rows"] = 7
        registry.upsert(template)
        self.assertEqual(registry.prepare("bios").wmi_plan.query.max_rows, 7)
        with self.assertRaises(ValueError):
            registry.export_file(self.path, [template])

    def test_ambiguous_json_and_invalid_bulk_import_are_atomic(self):
        registry = CommandRegistry(self.path)
        before = self.path.read_bytes()
        for content in (b'{"version":2,"version":1,"commands":[]}',
                        b'{"version":2,"commands":[],"value":NaN}'):
            with self.subTest(content=content), self.assertRaises(ValueError):
                registry._parse(content)
        commands = [_template_to_dict(bios_template()), {"name": "Broken", "alias": "broken", "wmi": {"operation": "method"}}]
        imported = self.path.with_name("import.json")
        imported.write_text(json.dumps({"version": 2, "commands": commands}))
        with self.assertRaises(ValueError):
            registry.import_file(imported)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertNotIn("bios", registry.aliases())


class CustomExecutionTests(unittest.TestCase):
    def test_backend_availability_is_checked_before_credentials_or_connect(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = SessionManager(repository=Repository(Path(folder) / "history.db"))
            self.addCleanup(manager.shutdown)
            manager.registry.import_file(EXAMPLES / "bios.json")
            with patch.object(manager, "_resolve_credential") as credentials:
                with self.assertRaisesRegex(CommandValidationError, "no WinRM"):
                    manager.run_command(Host("test", "example"), "bios")
                credentials.assert_not_called()
            help_result = manager.run_command_with_credential(Host("test", "example"), "bios -h")
            self.assertEqual(help_result.exit_code, 0)
            self.assertEqual(manager.states(), {})

    def test_both_backends_use_custom_query_limits_columns_and_no_methods(self):
        template = bios_template()
        template.wmi["columns"] = ["SerialNumber", "Manufacturer"]
        prepared = prepare_command("bios", [template])
        row = {"Manufacturer": "Fixture", "SMBIOSBIOSVersion": "1.0", "SerialNumber": None}
        host = Host("lab", "192.0.2.10", connection_mode=ConnectionMode.WMI)
        profile = CredentialProfile("ops", "synthetic", method=AuthMethod.NTLM)
        for backend in (DcomWmiSession, WmiSession):
            client = Mock(next_ping=None)
            client.query.return_value = [row]
            client.query_spec.return_value = [row]
            with self.subTest(backend=backend.__name__), patch.object(backend, "client_factory", return_value=client):
                session = backend(host, profile)
                session.connect(ResolvedCredential("synthetic", "synthetic"))
                result = session.run_prepared(prepared)
                self.assertEqual(result.exit_code, 0, result.stderr)
                self.assertEqual(list(result.records[0]), ["SerialNumber", "Manufacturer", "SMBIOSBIOSVersion"])
                self.assertIn("SELECT Manufacturer", result.command)
                self.assertEqual(session.state, SessionState.CONNECTED)
                call = (client.query if backend is DcomWmiSession else client.query_spec).call_args
                self.assertEqual(call.args[0].class_name, "Win32_BIOS")
                self.assertLessEqual(call.args[1], 15)
                client.invoke.assert_not_called()
                session.disconnect()

    def test_custom_query_runs_on_worker_and_persists_history(self):
        class Client(MemoryDcomClient):
            def query(self, spec, timeout):
                self.query_os(timeout)
                return [dict.fromkeys(spec.properties, "fixture")]
        with tempfile.TemporaryDirectory() as folder, patch.object(DcomWmiSession, "client_factory", Client):
            repo = Repository(Path(folder) / "history.db")
            manager = SessionManager(repository=repo, session_factory=DcomWmiSession)
            self.addCleanup(manager.shutdown)
            manager.registry.import_file(EXAMPLES / "bios.json")
            host = Host("lab", "192.0.2.10", connection_mode=ConnectionMode.WMI)
            profile = CredentialProfile("ops", "synthetic", method=AuthMethod.NTLM)
            result = manager.run_command_with_credential(host, "bios", profile, ResolvedCredential("synthetic", "synthetic"))
            self.assertEqual(result.exit_code, 0, result.stderr)
            history = repo.list_command_history(host_id=host.id)
            self.assertEqual(history[0].command_line, "bios")
            self.assertIn("Win32_BIOS", history[0].command)
            self.assertEqual(history[0].records, result.records)
            manager.shutdown()

    def test_explicit_query_overrides_builtin_alias_without_using_os_wrapper(self):
        template = bios_template()
        template.alias = "sysinfo"
        prepared = prepare_command("sysinfo", [template])
        client = Mock(next_ping=None)
        client.query.return_value = [dict.fromkeys(prepared.wmi_plan.query.properties, "fixture")]
        host = Host("lab", "192.0.2.10", connection_mode=ConnectionMode.WMI)
        profile = CredentialProfile("ops", "synthetic", method=AuthMethod.NTLM)
        with patch.object(DcomWmiSession, "client_factory", return_value=client):
            session = DcomWmiSession(host, profile)
            session.connect(ResolvedCredential("synthetic", "synthetic"))
            result = session.run_prepared(prepared)
            self.assertEqual(result.exit_code, 0, result.stderr)
            self.assertIn("Win32_BIOS", result.command)
            client.query_os.assert_not_called()
            session.disconnect()
