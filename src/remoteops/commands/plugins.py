"""Automatically discovered Python commands. Inspection never executes code."""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from remoteops.core.models import CommandTemplate

API_VERSION = 3
MAX_FILE_BYTES = 1024 * 1024
TRUST_WARNING = (
    "Plugins run locally as your account and use the host connection for remote operations. "
    "They can access local files and credentials; read-only plugins are not sandboxed."
)


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"Expected a regular file of at most 1 MiB: {path}")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class PluginSnapshot:
    folder: str
    manifest_json: str
    source: str
    digest: str

    @property
    def manifest(self):
        return json.loads(self.manifest_json)

    @property
    def backends(self):
        return tuple(self.manifest["backends"])

    @property
    def modifies(self):
        return self.manifest["modifies_remote"]

    def template(self):
        manifest = self.manifest
        return CommandTemplate(
            id="plugin:" + manifest["alias"], alias=manifest["alias"],
            name=manifest["name"], description=manifest.get("description", ""),
            category=manifest.get("category", "Python"),
            parameters=manifest.get("parameters", {}), plugin=self,
        )


def read_plugin(folder):
    from remoteops.commands.templates import parse_json, validate_template
    folder = Path(folder)
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("A plugin must be a regular directory")
    manifest = parse_json(_read(folder / "command.json"))
    allowed = {"api_version", "alias", "name", "description", "category", "parameters",
               "backends", "modifies_remote", "entry_point"}
    if not isinstance(manifest, dict) or set(manifest) - allowed:
        raise ValueError("Unknown plugin manifest fields")
    if type(manifest.get("api_version")) is not int or manifest["api_version"] not in (1, 2, API_VERSION):
        raise ValueError("Unsupported Python command API version; expected 1, 2 or 3")
    if manifest.get("entry_point") != "implementation.py:run":
        raise ValueError("entry_point must be implementation.py:run")
    for key in ("alias", "name", "description", "category"):
        if not isinstance(manifest.get(key, ""), str):
            raise ValueError(f"{key} must be text")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", manifest.get("alias", "")):
        raise ValueError("Alias must be lowercase letters, digits or hyphens, up to 64 characters")
    modes = manifest.get("backends")
    if (not isinstance(modes, list) or not modes or len(modes) > 2
            or any(mode not in ("wmi_dcom", "winrm") for mode in modes)
            or len(set(modes)) != len(modes)):
        raise ValueError("backends must contain wmi_dcom and/or winrm")
    if type(manifest.get("modifies_remote")) is not bool:
        raise ValueError("modifies_remote must explicitly be true or false")
    source = _read(folder / "implementation.py")
    tree = ast.parse(source, filename=str(folder / "implementation.py"))
    runs = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "run"]
    if len(runs) != 1 or not isinstance(runs[0], ast.FunctionDef):
        raise ValueError("Define one synchronous run(ctx, args) function")
    args = runs[0].args
    if (len(args.posonlyargs + args.args) != 2 or args.vararg or args.kwarg
            or args.kwonlyargs or args.defaults or runs[0].decorator_list):
        raise ValueError("Entry point must be an undecorated run(ctx, args) function")
    compile(tree, str(folder / "implementation.py"), "exec")
    canonical = json.dumps(manifest, sort_keys=True, allow_nan=False)
    digest = hashlib.sha256((canonical + "\0" + source).encode("utf-8")).hexdigest()
    snapshot = PluginSnapshot(str(folder.resolve()), canonical, source, digest)
    validate_template(snapshot.template())
    return snapshot


class PluginStore:
    def __init__(self, root, reserved, lock=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.reserved = reserved
        self._lock = lock or RLock()
        self._active = {}
        self.errors = {}

    @property
    def templates(self):
        with self._lock:
            return [snapshot.template() for snapshot in self._active.values()]

    def folders(self):
        return sorted(path for path in self.root.iterdir() if path.is_dir() and not path.is_symlink())

    def active(self, folder):
        with self._lock:
            return self._active.get(Path(folder).name)

    def validate(self, folder):
        folder = Path(folder)
        if folder.resolve().parent != self.root.resolve() or folder.is_symlink():
            raise ValueError("Select a command folder directly inside the plugin directory")
        snapshot = read_plugin(folder)
        alias = snapshot.manifest["alias"]
        if alias in self.reserved():
            raise ValueError(f"Alias '{alias}' conflicts with a saved or built-in command")
        with self._lock:
            if any(s.manifest["alias"] == alias for key, s in self._active.items() if key != folder.name):
                raise ValueError(f"Plugin alias '{alias}' is already installed")
        return snapshot

    def restore(self):
        self.refresh()

    def refresh(self):
        """Publish a complete new snapshot; in-flight commands retain their old one."""
        with self._lock:
            active, errors, aliases = {}, {}, {}
            reserved = set(self.reserved())
            for folder in self.folders():
                try:
                    snapshot = read_plugin(folder)
                    alias = snapshot.manifest["alias"]
                    if alias in reserved:
                        raise ValueError(f"Alias '{alias}' conflicts with a saved or built-in command")
                    if alias in aliases:
                        previous = aliases[alias]
                        active.pop(previous, None)
                        errors[previous] = f"Duplicate plugin alias '{alias}'"
                        raise ValueError(errors[previous])
                    aliases[alias] = folder.name
                    active[folder.name] = snapshot
                except (ValueError, OSError, SyntaxError) as exc:
                    errors[folder.name] = str(exc)
            self._active, self.errors = active, errors

    def enable(self, snapshot):
        """Compatibility helper for existing callers; normal discovery is automatic."""
        with self._lock:
            current = self.validate(Path(snapshot.folder))
            if current.digest != snapshot.digest:
                raise ValueError("Files changed since inspection; read the current version")
            self.refresh()

    def install(self, source):
        snapshot = read_plugin(source)
        alias = snapshot.manifest["alias"]
        if alias in self.reserved():
            raise ValueError(f"Alias '{alias}' is reserved")
        destination = self.root / alias
        test_path = Path(source) / "test_implementation.py"
        test_source = _read(test_path) if test_path.exists() else None
        destination.mkdir()  # Never merge with or overwrite an existing command.
        try:
            (destination / "command.json").write_text(snapshot.manifest_json + "\n", encoding="utf-8")
            (destination / "implementation.py").write_text(snapshot.source, encoding="utf-8")
            if test_source is not None:
                (destination / "test_implementation.py").write_text(test_source, encoding="utf-8")
        except OSError:
            # Preserve incomplete files so they can be repaired; discovery reports errors.
            raise
        self.refresh()
        return destination

    def scaffold(self, alias):
        if not isinstance(alias, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", alias):
            raise ValueError("Use a lowercase alias with letters, digits or hyphens (max 64)")
        if alias in self.reserved():
            raise ValueError("That alias is reserved")
        folder = self.root / alias
        folder.mkdir()
        manifest = {"api_version": API_VERSION, "alias": alias, "name": alias.replace("-", " ").title(),
                    "description": "BIOS inventory", "category": "Python", "parameters": {},
                    "backends": ["wmi_dcom"], "modifies_remote": False,
                    "entry_point": "implementation.py:run"}
        (folder / "command.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (folder / "implementation.py").write_text(STARTER_SOURCE, encoding="utf-8")
        (folder / "test_implementation.py").write_text(STARTER_TEST, encoding="utf-8")
        self.refresh()
        return folder


STARTER_SOURCE = '''def run(ctx, args):
    ctx.progress("Reading BIOS inventory")
    rows = ctx.query(
        class_name="Win32_BIOS",
        properties={"Manufacturer": "string", "SMBIOSBIOSVersion": "string"},
    )
    return [{"Vendor": row["Manufacturer"], "Version": row["SMBIOSBIOSVersion"]}
            for row in rows]
'''

STARTER_TEST = '''import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext


class CommandTests(unittest.TestCase):
    def test_inventory(self):
        ctx = FakeContext(query_results=[[{"Manufacturer": "Example", "SMBIOSBIOSVersion": "1.0"}]])
        self.assertEqual(run(ctx, {}), [{"Vendor": "Example", "Version": "1.0"}])
        self.assertEqual(ctx.queries[0].query.class_name, "Win32_BIOS")
'''
