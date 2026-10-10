import unittest

from remoteops.commands.templates import BUILTIN_TEMPLATES, CommandValidationError, prepare_command, render_template, resolve_command


class TemplateTests(unittest.TestCase):
    def test_restart_service_template_escapes_single_quotes(self) -> None:
        template = next(item for item in BUILTIN_TEMPLATES if item.name == "Restart service")

        rendered = render_template(template, {"service_name": "Bob's Service"})

        self.assertIn("Bob''s Service", rendered)

    def test_sysinfo_alias_resolves_to_system_summary(self) -> None:
        rendered, source = resolve_command("sysinfo")

        self.assertEqual(source, "System summary")
        self.assertIn("Win32_OperatingSystem", rendered)

    def test_unknown_alias_is_rejected(self) -> None:
        for command in ["Get-Process", "sysinfo; hostname", "sysinfo | hostname", "sysinfo\nhostname"]:
            with self.subTest(command=command), self.assertRaises(CommandValidationError):
                resolve_command(command)

    def test_help_is_local_and_describes_optional_arguments(self):
        prepared = prepare_command("events -h")
        self.assertFalse(prepared.script)
        self.assertIn("--count VALUE", prepared.help_text)
        self.assertIn("--log VALUE", prepared.help_text)
        self.assertIn("default: 50", prepared.help_text)

    def test_invalid_or_repeated_options_are_rejected(self):
        for command in ["events --unknown x", "events count=0", "events count=nope", "events count=2 count=3", "events --count", 'events "unclosed']:
            with self.subTest(command=command), self.assertRaises(CommandValidationError):
                prepare_command(command)

    def test_named_options_do_not_overwrite_positional_log(self):
        prepared = prepare_command("events Application --count 5")
        self.assertEqual(prepared.values, {"log": "Application", "count": 5})

    def test_argument_replacement_is_literal_and_single_pass(self):
        from remoteops.core.models import CommandTemplate
        template = CommandTemplate(name="Test", alias="test", script='"$name"; \'$other\'', parameters={"name": "", "other": ""})
        rendered = render_template(template, {"name": "'$other' $(hostname) \\logs", "other": "replacement"})
        self.assertEqual(rendered, "'''$other'' $(hostname) \\logs'; 'replacement'")

    def test_admins_alias_uses_well_known_sid(self) -> None:
        rendered, source = resolve_command("admins")

        self.assertEqual(source, "Local administrators")
        self.assertIn("S-1-5-32-544", rendered)

    def test_alias_supports_positional_and_key_value_options(self) -> None:
        positional, _source = resolve_command("events Application 25")
        keyed, _source = resolve_command("events log=Security count=10")

        self.assertIn("$log = 'Application'", positional)
        self.assertIn("$count = [int]'25'", positional)
        self.assertIn("$log = 'Security'", keyed)
        self.assertIn("$count = [int]'10'", keyed)
