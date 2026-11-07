"""Public Python command API v3 (v1/v2 compatible). Helpers are bounded, Python is trusted."""
from __future__ import annotations

import json
import math
import time
import traceback
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import ModuleType

from remoteops.commands.wmi_definitions import compile_definition
from remoteops.commands.wmi_invocation import compile_delete, compile_method, object_path

MAX_POWERSHELL_SCRIPT_CHARS = 100_000_000


class PluginError(RuntimeError):
    """Expected command failure; does not imply a broken connection."""


def _powershell_text(value):
    if not isinstance(value, (bytes, bytearray)):
        return value
    raw = bytes(value)
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else (
        "utf-16-le" if b"\x00" in raw else "utf-8-sig"
    )
    try:
        return raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise PluginError("PowerShell script bytes must be UTF-8 or UTF-16 text") from exc


class PluginContext:
    def __init__(self, *, backend, query=None, powershell=None, timeout_seconds=60,
                 cancel=None, progress=None, invoke=None, modifies_remote=False, delete_instance=None):
        self.backend = backend
        self._query = query
        self._powershell = powershell
        self._invoke = invoke
        self._delete_instance = delete_instance
        self._modifies_remote = modifies_remote
        self._method_attempted = False
        self._deadline = time.monotonic() + timeout_seconds
        self._cancel = cancel if cancel is not None else Event()
        self._progress = progress
        self._progress_count = 0

    def check_cancelled(self):
        caution = (" Side effects may already have occurred; inspect the remote state before retrying."
                   if self._method_attempted else "")
        if self._cancel.is_set():
            raise PluginError("Python command cancelled; completed remote operations were not rolled back" + caution)
        if time.monotonic() >= self._deadline:
            raise PluginError("Python command deadline expired" + caution)

    @property
    def remaining_seconds(self):
        self.check_cancelled()
        return self._deadline - time.monotonic()

    def progress(self, message):
        self.check_cancelled()
        if not isinstance(message, str) or len(message) > 1000:
            raise PluginError("Progress must be text of at most 1000 characters")
        if self._progress_count < 100 and self._progress is not None:
            self._progress(message)
        self._progress_count += 1

    def query(self, *, class_name, properties, namespace="root/cimv2", filters=None,
              rows=1000, timeout_seconds=30, take=None):
        self.check_cancelled()
        if self.backend != "wmi_dcom" or self._query is None:
            raise PluginError("ctx.query requires a WMI host; there is no backend fallback")
        definition = {"operation": "query", "namespace": namespace, "class": class_name,
                      "properties": properties, "filters": filters or [],
                      "limits": {"rows": rows, "timeout_seconds": timeout_seconds}}
        plan = compile_definition(definition, {}, {}, extended=True, take=take)
        result = self._query(plan, min(timeout_seconds, self.remaining_seconds))
        self.check_cancelled()
        return result

    object_path = staticmethod(object_path)

    @property
    def capabilities(self):
        common = ("progress", "sleep", "check_cancelled", "require_success", "object_path")
        return common + (("query", "invoke", "invoke_allow_no_output", "delete_instance")
                         if self.backend == "wmi_dcom" else ("powershell", "powershell_file"))

    def delete_instance(self, path, *, namespace="root/cimv2", timeout_seconds=30):
        self.check_cancelled()
        if self.backend != "wmi_dcom" or self._delete_instance is None:
            raise PluginError("ctx.delete_instance requires a WMI host; there is no backend fallback")
        if self._modifies_remote is not True:
            raise PluginError("ctx.delete_instance requires modifies_remote: true in command.json")
        plan = compile_delete(path, namespace, timeout_seconds)
        budget = min(timeout_seconds, self.remaining_seconds)
        self._method_attempted = True
        return self._delete_instance(plan, budget)

    def invoke(self, path, method, *, inputs=None, outputs=None, namespace="root/cimv2", timeout_seconds=30,
               allow_no_output=False):
        self.check_cancelled()
        if self.backend != "wmi_dcom" or self._invoke is None:
            raise PluginError("ctx.invoke requires a WMI host; there is no backend fallback")
        if self._modifies_remote is not True:
            raise PluginError("ctx.invoke requires modifies_remote: true in command.json")
        plan = compile_method(path, method, inputs, outputs, namespace, timeout_seconds,
                              allow_no_output=allow_no_output)
        budget = min(timeout_seconds, self.remaining_seconds)
        self._method_attempted = True
        result = self._invoke(plan, budget)
        # Preserve the confirmed return value if cancellation arrives during the call.
        # The next helper/checkpoint (or the runner) observes cancellation; never replay.
        return result

    @staticmethod
    def require_success(result, success_codes=(0,), *, message="WMI method"):
        if not isinstance(result, dict) or type(result.get("ReturnValue")) is not int:
            raise PluginError(f"{message}: missing integer ReturnValue; inspect the method output schema")
        if not isinstance(success_codes, (tuple, list)) or not success_codes or any(type(code) is not int for code in success_codes):
            raise ValueError("success_codes must contain integer provider return codes")
        if result["ReturnValue"] not in success_codes:
            raise PluginError(f"{message} returned provider code {result['ReturnValue']}; no automatic retry")
        return result

    def sleep(self, seconds):
        if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError("Sleep duration must be a finite nonnegative number")
        self._cancel.wait(min(seconds, self.remaining_seconds))
        self.check_cancelled()

    def powershell(self, script):
        self.check_cancelled()
        if self.backend != "winrm" or self._powershell is None:
            raise PluginError("ctx.powershell requires a WinRM host; there is no backend fallback")
        script = _powershell_text(script)
        if not isinstance(script, str):
            raise PluginError(f"PowerShell script must be text; received {type(script).__name__}")
        if not script.strip():
            raise PluginError("PowerShell script is empty or whitespace only")
        if len(script) > MAX_POWERSHELL_SCRIPT_CHARS:
            raise PluginError(
                f"PowerShell script has {len(script)} characters; maximum is {MAX_POWERSHELL_SCRIPT_CHARS}"
            )
        result = self._powershell(script, self.remaining_seconds)
        self.check_cancelled()
        return result

    def powershell_file(self, path):
        """Run a local PowerShell function script in the persistent WinRM runspace."""
        self.check_cancelled()
        if self.backend != "winrm" or self._powershell is None:
            raise PluginError("ctx.powershell_file requires a WinRM host; there is no backend fallback")
        if not isinstance(path, (str, Path)):
            raise PluginError("Provide a local PowerShell script path")
        local_path = Path(path).expanduser()
        if not local_path.is_file():
            raise PluginError(f"Local PowerShell script was not found: {local_path}")
        if local_path.suffix.lower() not in {".ps1", ".psm1"}:
            raise PluginError("Local PowerShell script must use a .ps1 or .psm1 extension")
        try:
            raw = local_path.read_bytes()
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            source = raw.decode(encoding)
        except UnicodeDecodeError as exc:
            raise PluginError("Local PowerShell script must be UTF-8 or UTF-16 text") from exc
        return self.powershell(source)


def execute_plugin(snapshot, context, values):
    """Fresh globals per run; no sys.path edits or reusable plugin module cache."""
    filename = snapshot.folder + "/implementation.py"
    module = ModuleType("remoteops_plugin_" + snapshot.digest)
    module.__file__ = filename
    try:
        context.check_cancelled()
        exec(compile(snapshot.source, filename, "exec"), module.__dict__)
        rows = module.run(context, deepcopy(values))
        context.check_cancelled()
        if not isinstance(rows, list) or len(rows) > 10000:
            raise PluginError("Return a list of at most 10000 row dictionaries")
        if any(not isinstance(row, dict) or len(row) > 64
               or any(not isinstance(key, str) for key in row) for row in rows):
            raise PluginError("Each result row must be a dictionary with at most 64 text column names")
        encoded = json.dumps(rows, allow_nan=False)
        if len(encoded.encode("utf-8")) > 10 * 1024 * 1024:
            raise PluginError("Python command output exceeds 10 MiB")
        return json.loads(encoded)
    except PluginError as exc:
        # Include call locations without copying script source or local values into history.
        frames = traceback.extract_tb(exc.__traceback__)
        locations = "\n".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}'
            for frame in frames
        )
        raise PluginError(f"{exc}\nCall locations:\n{locations}") from None
    except BaseException as exc:
        # A plugin's SystemExit must not terminate the app or strand its worker.
        detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        raise PluginError(f"Python command failed ({type(exc).__name__}): {exc}\n{detail}") from None


class FakeContext(PluginContext):
    """Offline test double: queued responses, recorded plans, no network access in helpers."""
    def __init__(self, query_results=(), powershell_results=(), backend="wmi_dcom", method_results=(), delete_results=(), **kwargs):
        self.queries = []
        self.scripts = []
        self.messages = []
        self.methods = []
        self.deletions = []
        self._query_results = iter(deepcopy(query_results))
        self._powershell_results = iter(deepcopy(powershell_results))
        self._method_results = iter(deepcopy(method_results))
        self._delete_results = iter(deepcopy(delete_results))
        super().__init__(backend=backend, query=self._fake_query, powershell=self._fake_powershell,
                         invoke=self._fake_invoke, delete_instance=self._fake_delete,
                         progress=self.messages.append, **kwargs)

    def _fake_delete(self, plan, timeout):
        self.deletions.append(plan)
        result = next(self._delete_results)
        if isinstance(result, Exception):
            raise result
        return result

    def _fake_query(self, plan, timeout):
        self.queries.append(plan)
        return next(self._query_results)

    def _fake_powershell(self, script, timeout):
        self.scripts.append(script)
        return next(self._powershell_results)

    def _fake_invoke(self, plan, timeout):
        self.methods.append(plan)
        result = next(self._method_results)
        if isinstance(result, Exception):
            raise result
        return result
