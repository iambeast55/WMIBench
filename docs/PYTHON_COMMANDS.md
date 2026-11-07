# Python Commands (API v3, v1/v2 Compatible)

See [Plugin API Reference](PLUGIN_API.md) for every context helper, parameter schema
field, method input/output format, limits, and action examples. API v2 adds generic
typed WMI invocation, object paths, cancellable waits, array projections, arbitrary
host-relative namespaces, and explicit prefix queries. Existing v1 commands still load.
API v3 adds `ctx.delete_instance()`, larger text inputs and genuinely optional
arguments. The [task and registry plugin pack](ADMIN_PLUGINS.md) uses these additions.

Install this WMIBench update once. After that, add or edit Python command folders
without rebuilding, reinstalling, or restarting the application. Simple read-only
WMI queries can still use the existing JSON Command Library editor instead.

## First Command

1. Open **Command Library**, then the **Python commands** code icon.
2. Choose **Create command** and enter an unused alias such as `my-bios`.
3. Select its row and choose **Open folder**. Edit `implementation.py` in your editor.
4. The command is already available. Run `my-bios -h` in the host terminal, then
   `my-bios`. **Use command** inserts its alias into the terminal without running it.
5. Optionally choose **Run offline tests**. The generated test uses fake responses.
6. Save edits and run again. Changes are picked up on the next command preparation;
   no Validate, Enable, or reload approval is needed. Healthy connections stay usable.

**Install folder** copies a command into the application's plugin directory and makes
it available immediately. Copying a folder there manually also works. Existing folders
are never overwritten. To share a command, share its
folder; Python commands are not exported into the ordinary command-library JSON file.

## Files And Location

Plugins live in `plugins/` beside the application's database and `commands.json`.
Use **Open folder** with no selection to find the exact location; it follows your
configured data directory. This is separate from the application's installation directory.

```text
plugins/
  my-bios/
    command.json
    implementation.py
    test_implementation.py
```

```json
{
  "api_version": 3,
  "alias": "my-bios",
  "name": "My BIOS inventory",
  "description": "Read BIOS manufacturer and version.",
  "category": "Python",
  "parameters": {},
  "backends": ["wmi_dcom"],
  "modifies_remote": false,
  "entry_point": "implementation.py:run"
}
```

`backends` accepts `wmi_dcom`, `winrm`, or both. `modifies_remote` is required;
true prevents read-only batch use. Installed commands run without activation or
per-run trust prompts. Only install Python code you trust; it is not sandboxed.
Aliases are lowercase letters, digits and hyphens, starting with a letter, at most
64 characters. Built-in aliases, saved aliases and other plugin aliases cannot
be shadowed. The API requires one undecorated synchronous `run(ctx, args)` function
in `implementation.py`. Unsupported API versions and unknown manifest fields fail validation.

## Implementation

```python
def run(ctx, args):
    rows = ctx.query(
        class_name="Win32_BIOS",
        properties={"Manufacturer": "string", "SMBIOSBIOSVersion": "string"},
    )
    return [{"Vendor": row.get("Manufacturer"),
             "Version": row.get("SMBIOSBIOSVersion")} for row in rows]
```

Return a list of row dictionaries. Values must be JSON-serializable, with no NaN
or infinity. Limits: 10,000 rows, 64 columns per row, 10 MiB serialized output.
Return `[]` for an empty result. Do not print results: return them so the normal
table, terminal, CSV export, and history can display them. History records the
plugin alias and full source/manifest SHA-256 digest.

### Context API

- `ctx.backend`: `wmi_dcom` or `winrm` for this invocation.
- `ctx.query(class_name=..., properties=..., namespace="root/cimv2", filters=[],
  rows=1000, timeout_seconds=30, take=None)`: returns rows through the existing WMI session.
  Python queries additionally support root-relative namespaces and array projections
  such as `string[]`. No raw WQL or SELECT *. `rows` is an overflow result limit;
  `take=N` explicitly returns at most the first N enumerated rows, without ordering guarantees.
  Per-query limits are 1..10,000 rows and 1..60 seconds, bounded by the remaining
  total command deadline. Filters use literal values, not raw fragments.
- `ctx.object_path(class_name, keys=None, singleton=False)`: builds a quoted,
  namespace-relative instance path. Omit keys for static class methods.
- `ctx.invoke(path, method, inputs=None, outputs=None, namespace="root/cimv2",
  timeout_seconds=30)`: WMI methods with typed inputs and declared outputs. Requires
  `modifies_remote: true`. Returns a dictionary, not a list. Provider return codes
  are data, not transport failures; inspect them or call `ctx.require_success(result)`.
- `ctx.require_success(result, success_codes=(0,), message="WMI method")`: raises
  PluginError when ReturnValue is missing or not one of the accepted integer codes.
  Never retries or disconnects by itself.
- `ctx.sleep(seconds)`: cancellable wait within the shared deadline, useful between
  status queries. It does not repeat an action.
- `ctx.capabilities`: tuple of helper names available for this connection backend;
  does not assert that a specific Windows provider/class/method is installed.
- `ctx.powershell(script)`: WinRM only. Returns `{"records": [...], "stdout": "..."}`.
  A failed pipeline raises an error. The author is responsible for quoting values
  interpolated into a script; do not concatenate unescaped argument text.
- `ctx.progress(message)`: reports up to 100 messages, at most 1,000 characters each.
  Progress appears in the host terminal when that host has an open workspace.
- `ctx.check_cancelled()`: checks cancellation and the shared deadline. Call this
  inside long loops. Query helpers check before and after remote work. Invoke returns
  a confirmed method result even if cancellation arrives in flight; the next checkpoint
  or runner exit handles cancellation without replay. Confirmed method codes remain in History.
- `ctx.remaining_seconds`: remaining shared time budget; raises after cancellation
  or expiry. The total budget is at most 120 seconds, including Python processing.
- Raise `remoteops.commands.plugin_api.PluginError("explanation")` for an expected
  command failure. Other Python errors include source locations in error details.

Helpers never switch backends, open an alternate transport, or retry operations.
All remote calls execute on the existing dedicated host worker. The context does
not expose raw credentials, the host session, or credential-store methods.

### Parameters

Use the existing `_schema` and `_positional` format. For example:

```json
"parameters": {
  "service_name": "Spooler",
  "_positional": ["service_name"],
  "_schema": {"service_name": {"type": "str", "description": "Exact service name"}}
}
```

`args["service_name"]` contains the parsed value. Help, named arguments,
defaults, required values, choices and numeric bounds use the normal command parser.
For WMI filters, pass the value through the helper's structured filters:

```python
filters=[{"property": "Name", "operator": "eq", "value": args["service_name"]}]
```

The filter property must also be selected. The query validator handles literal
escaping and type validation; never construct WQL yourself.

## Offline Tests

The scaffold includes a unittest file. Import `run` from `implementation`, and
use `FakeContext(query_results=[...])`. Each nested list is the response to one
query. Inspect `ctx.queries` (compiled plans), `ctx.scripts`, and `ctx.messages`.
For WinRM, use `FakeContext(backend="winrm", powershell_results=[...])`.
For methods, use `FakeContext(method_results=[{"ReturnValue": 0}], modifies_remote=True)`;
inspect `ctx.methods` for typed method plans and call counts.
Fake helpers perform no network operations, but arbitrary Python in an extension
or test is not prevented from doing so. A fake result does not validate a real provider.

From a source checkout on Windows:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m remoteops.commands.plugin_cli validate examples/plugins/bios-summary
.\.venv\Scripts\python.exe -m remoteops.commands.plugin_cli test examples/plugins/bios-summary --trust
```

From the installed Linux application, use its interpreter (adjust for a custom install):

```bash
~/.local/share/remoteops/.venv/bin/python -m remoteops.commands.plugin_cli validate /path/to/my-bios
~/.local/share/remoteops/.venv/bin/python -m remoteops.commands.plugin_cli test /path/to/my-bios --trust
```

Validation never runs Python code. Testing explicitly executes both the implementation
and test file in a separate process. The UI permits stopping that process and limits
it to 120 seconds. No tests is a failure, not a successful test run.

Seven complete examples live in `examples/plugins/`: **bios-summary** (simple query
and transform), **service-check** (typed argument and escaped filter), and
**machine-summary** (two WMI queries plus an explicit WinRM implementation),
**service-action** (one confirmed service method), **registry-list** (typed inputs
and array output), **scheduled-task** (modern task management), and **registry**
(typed registry reads/writes). Generic invocation conservatively requires the remote-change flag
even for a read method such as EnumKey; the app does not infer method side effects.

## Automatic Loading And Recovery

- Startup, command preparation, and the plugin window's Refresh folders action discover
  added/changed files automatically. Background checks inspect manifest/schema, syntax
  and function signature without importing or executing the implementation.
- Each run captures its source snapshot and uses fresh module globals. Already prepared
  or running commands and batches finish with their original version, even after edits.
- Invalid edits block new runs of that plugin with a diagnostic. Other valid commands
  remain available. Repair the file and run again; stale code is not silently executed.
- Remove a plugin folder to uninstall it. This does not cancel a running invocation.
  Keep backup copies outside the plugin root to avoid duplicate-alias conflicts.
- Legacy `plugin-trust.json` files are ignored and left untouched. No approval state
  is needed, including after restarting the application.
- Python plugins run as local code under your account. They have your account's permissions
  and may read files, access the network or mutate process state. No raw credentials
  in the public API does not prevent plugin code from accessing application data.
- The read-only manifest flag is only a declaration. Multi-host execution requires
  the separate **Run Python plugins for this batch** checkbox and explicit confirmation.
  Commands declaring remote changes are excluded from batches in this version.
- Command exceptions do not disconnect a healthy session. Genuine transport or
  uncertain cleanup failures still require explicit reconnect. Nothing automatically
  replays a failed command or reverses completed changes.
- Running-command cancellation and deadlines are **cooperative**, not preemptive.
  They are checked between helper calls and at explicit checkpoints; they cannot
  safely terminate an infinite loop or an uncooperative blocking Python operation.
  Batch cancellation remains queued-only and does not interrupt running commands.

## API v3 Boundaries

Keep implementation logic in a single file. Runtime loading does not add plugin
folders to `sys.path`; relative/sibling Python modules and vendored dependencies are
not part of this version's reload contract. Standard-library and already-installed
dependencies can be imported, but there is no automatic package installation.
Tests load the reviewed implementation source directly to avoid stale bytecode.
Generic invocation supports scalar and flat-array inputs/outputs, including null
parameters. Explicit `allow_no_output=True` permits a successful synchronous call
with no output object, returning `{}` without inventing a provider code.
Embedded objects, method-schema discovery,
and fragmented oversized method requests are not implemented. The Linux generic
input encoder is fixture-tested but not live-verified. Use provider-declared input
order and exact CIM types. Adding a new transport capability or provider type still
requires an application update. Python methods and private
WMIBench internals are not a supported compatibility contract; target this public API.
