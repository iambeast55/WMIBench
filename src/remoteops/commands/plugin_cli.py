"""Offline validation, scaffolding and tests: python -m remoteops.commands.plugin_cli."""
from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from types import ModuleType

from remoteops.commands.plugins import PLUGIN_NOTE, PluginStore, _read, read_plugin


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("root", type=Path)
    create.add_argument("alias")
    validate = commands.add_parser("validate")
    validate.add_argument("folder", type=Path)
    test = commands.add_parser("test")
    test.add_argument("folder", type=Path)
    test.add_argument("--trust", action="store_true", help="Run the local Python tests")
    args = parser.parse_args(argv)
    try:
        if args.command == "create":
            from remoteops.commands.templates import TEMPLATES_BY_ALIAS
            print(PluginStore(args.root, lambda: set(TEMPLATES_BY_ALIAS)).scaffold(args.alias))
            return 0
        snapshot = read_plugin(args.folder)
        print(f"Valid API v{snapshot.manifest['api_version']} command: {snapshot.manifest['alias']} @ {snapshot.digest[:12]}", flush=True)
        if args.command == "validate":
            return 0
        if not args.trust:
            parser.error(PLUGIN_NOTE + " Use --trust to run the local tests.")
        folder = args.folder.resolve()
        test_file = folder / "test_implementation.py"
        if not test_file.is_file() or test_file.is_symlink():
            raise ValueError("Provide a regular test_implementation.py file")
        # Compile source directly so rapid same-size edits never reuse stale bytecode.
        implementation = ModuleType("implementation")
        implementation.__file__ = str(folder / "implementation.py")
        sys.modules["implementation"] = implementation
        exec(compile(snapshot.source, implementation.__file__, "exec"), implementation.__dict__)
        tests = ModuleType("test_implementation")
        tests.__file__ = str(test_file)
        exec(compile(_read(test_file), str(test_file), "exec"), tests.__dict__)
        suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
        if suite.countTestCases() == 0:
            raise ValueError("No offline tests were discovered")
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
    except (ValueError, OSError, SyntaxError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
