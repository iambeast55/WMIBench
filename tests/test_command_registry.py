import json
import unittest

from remoteops.commands.templates import CommandRegistry
from remoteops.core.models import CommandTemplate


class CommandRegistryTests(unittest.TestCase):
    def test_registry_creates_editable_command_file(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "commands.json"
            registry = CommandRegistry(path)

            self.assertTrue(path.exists())
            self.assertGreater(len(registry.templates), 0)

    def test_registry_round_trips_custom_command(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "commands.json"
            registry = CommandRegistry(path)
            registry.templates = [
                CommandTemplate(name="Whoami", alias="whoami", script="whoami.exe")
            ]
            registry.save()

            loaded = CommandRegistry(path)
            resolved, source = loaded.resolve("whoami")

            self.assertEqual(source, "Whoami")
            self.assertEqual(resolved, "whoami.exe")

    def test_registry_can_load_manually_edited_json(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "commands.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "commands": [
                            {
                                "name": "Hostname",
                                "alias": "hn",
                                "description": "Show hostname",
                                "script": "hostname",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            registry = CommandRegistry(path)
            resolved, source = registry.resolve("hn")

            self.assertEqual(source, "Hostname")
            self.assertEqual(resolved, "hostname")

    def test_active_library_adds_new_builtin(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "commands.json"
            registry = CommandRegistry(path)
            data = json.loads(path.read_text(encoding="utf-8"))
            data["commands"] = [item for item in data["commands"] if item["alias"] != "dir"]
            path.write_text(json.dumps(data), encoding="utf-8")

            loaded = CommandRegistry(path)
            self.assertIn("dir", loaded.aliases())
            self.assertTrue(loaded.prepare("dir C:\\Windows").native_wmi)
