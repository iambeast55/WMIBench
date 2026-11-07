# Plugin API Reference

API v3 extends v1/v2; existing commands remain compatible. Put `"api_version": 3`
in commands requiring instance deletion, larger text inputs or omitted optional arguments, so older builds reject
their manifest before execution. See [Python Commands](PYTHON_COMMANDS.md) for installation,
automatic loading and offline testing. Python is local code, not a sandbox.

## Entry Point And Parameters

```python
def run(ctx, args):
    return [{"Example": args["count"]}]
```

`ctx` provides the helpers below. `args` is a dictionary of parsed command-line
arguments. Return a list of dictionaries; results flow to the normal terminal,
table and History. Exceptions fail this command; they do not themselves break a
healthy connection. No helper automatically retries an action.

### Parameter Schema

The `parameters` object lives in `command.json`. `_schema` describes each argument;
`_positional` is a sibling list of names in positional argument order. It is not a
field inside an individual argument. Named arguments work whether or not listed there.

| Field inside each `_schema` entry | Values | Behavior |
| --- | --- | --- |
| `type` | `str`, `int`, `float`, `bool` | Parsed Python type. Defaults to `str`. |
| `description` | String | Help text and argument-form label. |
| `default` | Value matching the declared type | Used when omitted. |
| `required` | Boolean | Omission is rejected if there is no default. |
| `choices` | Array of values matching the type | Allowed values; produces a dropdown. |
| `min` | Number | Inclusive lower numeric bound. |
| `max` | Number | Inclusive upper numeric bound. |

This is WMIBench' own argument format, not JSON Schema. `enum`, `minimum`,
`maximum`, `pattern`, `items` and nested-object schemas are not supported. Extra
schema keywords currently have no effect; do not rely on them for validation.
Use explicit types and defaults in `_schema` for new plugins instead of inferred
top-level defaults. In API v3, omitted optional arguments without defaults are absent
from `args`; an explicitly supplied empty string remains present. Argument forms
have include checkboxes for these fields. Use `args.get(name)` or provide a default.
V1/v2 keep their historical empty-string substitution (which can fail numeric/choice
validation); use explicit defaults there. `required` primarily means supplied,
not nonempty: validate nonblank strings in your code when needed.

```json
"parameters": {
  "_positional": ["service_name", "action"],
  "_schema": {
    "service_name": {"type": "str", "required": true, "description": "Exact service name"},
    "action": {"type": "str", "default": "StartService", "choices": ["StartService", "StopService"]},
    "count": {"type": "int", "default": 5, "min": 1, "max": 100}
  }
}
```

Run `alias Spooler StopService --count 5` or
`alias --service-name Spooler --action StopService --count 5`.
Python uses `args["service_name"]`, with underscores. Boolean options require an
explicit `true` or `false`, for example `--enabled true`; they are not bare switches.
JSON booleans are unquoted `true`/`false`; Python booleans are `True`/`False`.
Integer parsing defaults to signed 64-bit bounds unless overridden. Floats must be finite.

## Query

```python
rows = ctx.query(
    class_name="Win32_NetworkAdapterConfiguration",
    properties={"Description": "string", "IPAddress": "string[]", "IPEnabled": "boolean"},
    filters=[{"property": "IPEnabled", "operator": "eq", "value": True}],
    namespace="root/cimv2",
    rows=1000,
    take=5,
    timeout_seconds=30,
)
```

- WMI backend only. Returns `list[dict]`; no matches returns `[]`.
- Namespace must be relative to the connected host: `root`, `root/cimv2`,
  `root/default`, `root/standardcimv2`, or a provider-specific child namespace.
  No host-qualified paths or redirection. Auxiliary namespaces are cached on the
  same host worker, up to 16 per session; permissions/provider availability may differ.
- `properties` selects 1..64 exact provider fields with their CIM type names.
  Scalars and flat array projections are supported. No SELECT *, raw WQL or expressions.
- `filters` accepts up to 16 AND-combined comparisons. Operators: `eq`, `ne`, `lt`,
  `le`, `gt`, `ge`, `is_null`, `not_null`. Use `value`, not `parameter`, in Python
  helper filters. Null checks omit `value`. Filter properties must also be selected.
  Boolean fields support eq/ne; arrays support null checks only. Escaping is automatic.
- `rows` is a result limit from 1 to 10,000, default 1,000. Without `take`, exceeding
  it fails the query instead of silently dropping results.
- `take=N` explicitly stops after at most N rows; N must be between 1 and `rows`.
  Enumeration order is provider-defined: first N does not mean newest or highest N.
- `timeout_seconds` is an integer from 1 to 60, default 30, capped by the shared
  command deadline. There are no partial results on errors.
- JSON-only custom queries retain their previous stricter namespace/type rules.

## Object Paths

```python
service = ctx.object_path("Win32_Service", {"Name": args["service_name"]})
# Win32_Service.Name="Spooler"

process = ctx.object_path("Win32_Process", {"Handle": "1234"})
static_class = ctx.object_path("StdRegProv")
singleton = ctx.object_path("ExampleSingleton", singleton=True)
```

Supply up to 16 named keys with string, boolean or 64-bit integer values. Strings
are escaped for WMI paths. Omit keys for a class/static method. For singleton
instances use `singleton=True`, without keys. You may also pass a validated
namespace-relative path directly to invoke. Namespaces belong in the separate
`namespace` argument; paths cannot select another host.

## Invoke

`ctx.invoke(..., allow_no_output=True)` is an explicit opt-in for providers that
complete a synchronous call successfully without returning an output object, such
as observed Task Scheduler start/stop calls. It returns `{}` in that case, not a
fabricated `ReturnValue: 0`. A successful request does not prove the task/program
finished. Check `"invoke_allow_no_output" in ctx.capabilities` on older runtimes.
The default remains strict. Returned output objects are still checked against the
declared schema; HRESULT errors, malformed replies, and nonzero provider codes are
not suppressed. `ctx.require_success({})` still fails because there is no return code.

Set `"modifies_remote": true` in the manifest and save it. This is
required for **all** generic method calls, even methods intended only to read data.
The app cannot classify arbitrary provider methods. Installed commands run without
activation or a repeated mutation popup and commands declaring changes remain excluded from
read-only batches. This flag is not a sandbox.

```python
def run(ctx, args):
    path = ctx.object_path("Win32_Service", {"Name": args["service_name"]})
    result = ctx.invoke(path, "StartService")
    ctx.require_success(result, message="StartService")
    return [{"Service": args["service_name"], **result}]
```

Full signature:

```python
result = ctx.invoke(
    "StdRegProv", "EnumKey",
    namespace="root/default",
    inputs={
        "hDefKey": ("uint32", 0x80000002),
        "sSubKeyName": ("string", args["subkey"]),
    },
    outputs={"ReturnValue": "uint32", "sNames": "string[]"},
    timeout_seconds=30,
)
```

| Argument | Contract |
| --- | --- |
| `path` | Required relative class or instance path. |
| `method` | Required provider method name, e.g. `StartService`. |
| `inputs` | Ordered dictionary of name to `(CIM type name, Python value)` pairs. Default `{}`. |
| `outputs` | Dictionary of expected output names to CIM type names. Default `{"ReturnValue": "uint32"}`. |
| `namespace` | Host-relative namespace; defaults to `root/cimv2`. |
| `timeout_seconds` | Integer 1..60, default 30, capped by remaining command time. |

Returns **one dictionary**, not a list. It includes the declared outputs, with their
declared spelling. A nonzero `ReturnValue` is provider data, not a transport exception;
interpret it according to the method documentation. `ctx.require_success()` is an
optional convenience, not an automatic rule imposed on every provider.

### Input Order And Types

For Linux DCOM, supply the complete supported input signature in the provider's
parameter ID order, starting at zero. Python dictionaries preserve insertion order.
Include nullable optional parameters as `("type", None)` when the provider permits
NULL; NULL is not the same as an omitted argument or an embedded object. The client
does not discover or repair method signatures. Wrong declarations fail locally or
at the provider and are never automatically retried.

Supported CIM names for queries and method inputs/outputs:

```text
string, boolean, sint8, uint8, sint16, uint16, sint32, uint32,
sint64, uint64, real32, real64, datetime, reference, char16
```

Append `[]` for flat arrays, e.g. `string[]`, `uint32[]`. Method input arrays accept
lists or tuples of up to 1,024 non-null elements. A whole parameter may be `None`.
String/datetime/reference input values are at most 65,536 UTF-16 code units in the
API v3 application (older builds cap them at 1,024) and cannot
contain NUL. `char16` is one non-surrogate UTF-16 character. Datetime/reference values
remain text; numeric ranges and finite floating values are checked before dispatch.
There are at most 64 input parameters and 64 projected output properties.

Linux requests must fit the current single-fragment codec limit (3,968 bytes for
the method request); metadata and strings count too. Larger requests fail before
dispatch. Embedded objects, nested arrays, method-signature discovery and arbitrary
COM access are not supported. Declare at
least one expected output field; it need not be ReturnValue if the method has others.
The explicit no-output opt-in above applies only when the entire output object is
absent from a complete successful reply, not to missing fields inside an object.

**Generic Linux input encoding is experimental and has not been verified against
a live Windows provider.** Tests include independent wire-layout checks and the
existing EnumKey fixture; they cannot establish provider interoperability. Test
your chosen method in a lab before relying on it. Windows uses native COM and
checks supplied types against the provider's input definitions before dispatch.

### Return Codes, Failures And History

```python
ctx.require_success(result)                    # Accept integer ReturnValue 0.
ctx.require_success(result, (0, 1), message="Provider method")
```

Only accept additional codes when the provider documents them as acceptable.
A returned code does not necessarily mean an asynchronous state transition has
finished; query state afterward when necessary. Do not retry the action while polling.
Provider failures and local decoding/type errors retain a healthy session when
cleanup is confirmed. A broken transport or uncertain reference cleanup still
requires explicit reconnect. If the reply is lost or cannot be decoded, the action
may already have happened: inspect remote state before deciding what to do next.

History retains the plugin digest plus attempted method paths and confirmed return
codes, even if later Python processing or cancellation fails. It does not log method
input values through this audit. Paths can contain identifiers, so avoid secrets in keys.

## Workflow Helpers

### Delete An Instance (v3)

```python
path = ctx.object_path("MSFT_ScheduledTask", {"TaskPath": "\\", "TaskName": "Lab"})
result = ctx.delete_instance(path, namespace="root/Microsoft/Windows/TaskScheduler",
                             timeout_seconds=30)
# {"Deleted": True, "Path": path}
```

This calls WMI's synchronous instance deletion operation, not a provider method
named Delete. It requires a keyed or singleton instance path, a WMI backend and
`modifies_remote: true`. Class deletion and remote-host redirection are rejected.
It returns only after successful completion; failures raise, and no action is retried.
There is no provider ReturnValue to pass to `require_success`. Session reuse,
deadlines, cancellation and uncertain outcomes follow invoke's rules.
History records the attempted path and successful HRESULT when confirmed.

Offline tests use `FakeContext(delete_results=[{"Deleted": True}], modifies_remote=True)`;
`ctx.deletions` records plans. Exceptions in the fixture queue are raised.
The Linux DeleteInstance codec is protocol-fixture-tested, not live-verified.

### Progress And Cancellation

```python
ctx.progress("Waiting for the service state")
ctx.sleep(0.5)
ctx.check_cancelled()
remaining = ctx.remaining_seconds
backend = ctx.backend
helpers = ctx.capabilities
```

- `progress`: up to 100 messages per run, each at most 1,000 characters.
- `sleep`: finite nonnegative seconds; wakes for cancellation and respects the
  remaining command deadline. Prefer it over `time.sleep()` in polling loops.
- `check_cancelled` / `remaining_seconds`: raise PluginError on cancellation or
  deadline expiry. Cancellation is cooperative, not a way to terminate arbitrary Python.
- `backend`: `wmi_dcom` or `winrm`. No silent backend fallback.
- `capabilities`: helper-name tuple for that backend, not remote provider discovery.
- `powershell(script)`: WinRM only; returns `{"records": [...], "stdout": "..."}`.
  It shares the deadline and existing runspace. Script arguments still require
  author-controlled PowerShell quoting; do not concatenate unescaped input. A
  `str` or UTF-8/UTF-16 script `bytes` value is accepted. Scripts may contain up
  to 100,000,000 characters (about 100 MB for ASCII source).
- `powershell_file(path)`: WinRM only. Reads a local `.ps1` or `.psm1` function
  script and runs its source in the existing persistent runspace. It is not uploaded
  or written to the remote filesystem. A later `ctx.powershell("Read-Me -Arg 1")`
  call can use functions defined by the file until the session is disconnected.

The shared command budget is at most 120 seconds. Methods are never replayed.
Cancellation arriving during a method does not cancel that in-flight remote action:
invoke first delivers a confirmed result if available, then the next checkpoint or
runner exit observes cancellation. History retains the confirmed return code.

## Testing

```python
from remoteops.commands.plugin_api import FakeContext

ctx = FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)
result = ctx.invoke(ctx.object_path("Win32_Service", {"Name": "Spooler"}), "StartService")
assert len(ctx.methods) == 1
assert ctx.methods[0].method == "StartService"
```

Queue one `method_results` dictionary per call; an Exception in that queue raises
at the corresponding call. `ctx.methods` records plans with `namespace`, `path`,
`method`, numeric typed `inputs`, `outputs` and `timeout_seconds`. Query fixtures
use `query_results`, and compiled queries are in `ctx.queries`. Fake helpers do not
contact hosts, but arbitrary code in your tests is still trusted local Python.

Complete action examples: `examples/plugins/service-action` and
`examples/plugins/registry-list`, plus the v3 `scheduled-task` and `registry`
plugins described in [Task And Registry Plugins](ADMIN_PLUGINS.md).
The original query examples remain compatible.

## Protocol References

Method behavior and signature ordering follow Microsoft's
[ExecMethod documentation](https://learn.microsoft.com/en-us/windows/win32/wmisdk/swbemservices-execmethod)
and [method parameter ID rules](https://learn.microsoft.com/en-us/windows/win32/wmisdk/creating-a-method).
The bounded Linux encoder uses the MS-WMIO
[value table](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/96e2c1c0-7efd-4846-92d8-514baa60f921),
[null/default table](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/65bcd0c2-b3f3-49a7-b4aa-c972cdc0774b)
and [sorted property lookup table](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/06ec93f3-b4df-4f7e-b2ba-090cd435becc).
Instance deletion follows [MS-WMI DeleteInstance op16](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/dc4aee01-7b9f-4c1a-8671-658fa334b8a4).
