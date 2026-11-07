"""Include the installable admin plugins' own offline suites in normal discovery."""
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from remoteops.commands.plugins import read_plugin


def load_tests(loader, suite, pattern):
    root = Path(__file__).resolve().parents[1] / "examples" / "plugins"
    for alias in ("scheduled-task", "registry"):
        folder = root / alias
        snapshot = read_plugin(folder)
        implementation = ModuleType("implementation")
        implementation.__file__ = str(folder / "implementation.py")
        exec(compile(snapshot.source, implementation.__file__, "exec"), implementation.__dict__)
        tests = ModuleType("admin_plugin_tests_" + alias.replace("-", "_"))
        tests.__file__ = str(folder / "test_implementation.py")
        with patch.dict(sys.modules, {"implementation": implementation}):
            exec(compile(Path(tests.__file__).read_text(encoding="utf-8"), tests.__file__, "exec"), tests.__dict__)
        suite.addTests(loader.loadTestsFromModule(tests))
    return suite
