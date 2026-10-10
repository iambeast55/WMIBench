import tempfile
import unittest
from pathlib import Path

from remoteops.core.database import Repository
from remoteops.core.models import CommandResult, ConnectionMode, CredentialProfile, Host


class RepositoryTests(unittest.TestCase):
    def test_repository_round_trips_hosts_and_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Repository(Path(temp_dir) / "remoteops.sqlite3")
            profile = repo.save_credential_profile(
                CredentialProfile(name="Admin", username="DOMAIN\\admin")
            )
            host = Host(name="SERVER01", address="server01.example.com", auth_profile_id=profile.id)

            repo.save_host(host)

            self.assertEqual(repo.list_credential_profiles()[0].name, "Admin")
            self.assertEqual(repo.list_hosts()[0].auth_profile_id, profile.id)

    def test_migration_preserves_legacy_hosts_and_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "legacy.db"
            repo = Repository(path)
            host = repo.save_host(Host("original", "server.example"))
            repo.save_command_result(CommandResult(host.id, host.name, "old-script", stdout="retained output"))
            with repo.connect() as conn:
                for column in ("connection_mode", "wmi_namespace", "keepalive_seconds"):
                    conn.execute(f"alter table hosts drop column {column}")
                for column in ("command_line", "connection_mode", "records_json"):
                    conn.execute(f"alter table command_history drop column {column}")
            migrated = Repository(path)
            self.assertEqual(migrated.get_host(host.id).connection_mode, ConnectionMode.WINRM)
            self.assertEqual(migrated.get_host(host.id).keepalive_seconds, 60)
            self.assertEqual(migrated.list_command_history()[0].stdout, "retained output")
            self.assertEqual(migrated.list_command_history()[0].records, [])

    def test_wmi_host_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo = Repository(Path(temp_dir) / "hosts.db")
            host = Host("wmi", "example", connection_mode=ConnectionMode.WMI, keepalive_seconds=120)
            repo.save_host(host)
            loaded = repo.get_host(host.id)
            self.assertEqual(loaded.connection_mode, ConnectionMode.WMI)
            self.assertEqual(loaded.keepalive_seconds, 120)
            self.assertTrue(loaded.endpoint.startswith("wmi://"))
