# Adding WMI Commands

There are three extension paths: add a read-only query in the Command Library,
install a [Python command](PYTHON_COMMANDS.md) for custom logic without
rebuilding, or extend the source-defined catalog/backend for a new transport
capability. Custom definitions run through the same Windows/Linux WMI sessions,
tables, history, and error handling as built-in commands. Saving never contacts a host.

## Add A Query Without Rebuilding

1. Open Command Library and choose Add. Set a unique alias, name, and category.
2. Open WMI, enable Custom WMI query, and select BIOS inventory or Service status
   from Preset. Presets replace the query fields and parameter definitions.
3. Leave PowerShell blank for a WMI-only command. A separate PowerShell script is
   optional when the alias should also work on WinRM hosts; there is no fallback.
4. Properties JSON maps exact provider property names to CIM type names. Filters
   JSON contains AND-combined comparisons. Parameters (JSON) retains the existing
   `_schema` and `_positional` conventions.
5. Choose the row/time limits and optional column order. Columns reorder selected
   properties; they do not discard raw fields from the table or history.
6. Validate / Preview checks locally and renders WQL using parameter defaults.
   Required parameters without defaults can be saved; their preview is unavailable
   until defaults are supplied. Validation is not a test of provider availability.
7. Save, then run `ALIAS -h` and the alias in a WMI host's terminal. Running the
   command is the explicit remote test; no editor-side background test is performed.

The library toolbar also provides Duplicate, Import, and Export Selected (hover
for labels). Import accepts a versioned library containing one or more commands.
Alias/ID conflicts reject the entire import; existing commands are never overwritten
by import. Edit the imported alias/ID in its separate JSON file before retrying.
Duplicating a custom query preserves its WMI definition. A built-in workflow with
no explicit query definition does not acquire a new WMI handler by being renamed.

Working import files are included in `examples/wmi/bios.json` and
`examples/wmi/service-status.json`. Automated tests load, compile, and decode
fixtures for both examples. For a quick start, import the BIOS example and run
`bios -h`, then `bios` on your selected WMI host. No rebuild/restart is required.

### Organize And Select Commands

Search matches aliases, names, descriptions, definitions, and categories. Category,
backend, and Favorites filters combine. Check the Favorite column to save a
favorite; favorites sort first, followed by category and alias. Edit Category to
group your own commands. Existing builtins receive default categories without reset.
Open the top-level Commands library to manage commands. Type aliases and arguments
directly into the terminal; use `ALIAS -h` for options and examples. The parser checks
`_schema` types, choices, defaults, required flags and numeric bounds locally.
There is no separate command-arguments form or terminal command-browser button.

Plain-language failures include whether the session is usable. Full diagnostics
appear directly in the terminal and can be selected/copied. Command History keeps
original stderr for earlier failures; no Latest error button is needed.

### Supported Definition Options

The library envelope is `{"version": 2, "commands": [...]}`. Each command has
`name`, `alias`, optional `description`, `parameters`, and at least one of `script`
or `wmi`. Optional `category` is text of at most 80 characters; `favorite` is a JSON
boolean. A PowerShell command may also set `module_path` to a local `.ps1` or
`.psm1` file. WMIBench sends that script source through WinRM and runs it in the
persistent session scope before the command script; it never writes the file to
the remote disk. Leave `script` blank to load the local script without invoking a
second command. The module path belongs to the WMIBench client and is not portable
across clients. These fields survive export/import/duplication. Older builds with strict schemas
cannot read these new metadata fields; keep a library backup before downgrading.
A WMI definition accepts only:

- `operation`: `query`.
- `namespace`: `root/cimv2` (also the default); no parameterized namespaces.
- `class`: a literal CIM identifier.
- `properties`: 1..64 name/type pairs. Supported names are `string`, `boolean`,
  `sint8/16/32/64`, `uint8/16/32/64`, `real32/64`, `datetime`, `reference`, `char16`.
  Use the full type name such as `uint32`, not the slash shorthand.
- `filters`: at most 16 objects with `property`, `operator`, and exactly one of
  `value` or `parameter`. Comparison operators: `eq`, `ne`, `lt`, `le`, `gt`, `ge`.
  Null operators `is_null` and `not_null` take neither value nor parameter.
- `limits`: `rows` 1..10000 (default 1000), `timeout_seconds` 1..60 (default 30).
- `columns`: optional unique selected property names, in presentation order.

Filter properties must be projected, and parameter types must match: `str` for
text/datetime/reference/char16, `int` for integer CIM types, `float` for reals, and
`bool` for booleans. Parameters used in filters need a default or `required: true`.
Integer CIM ranges, finite numbers, text bounds, identifiers, and escaping are
validated before remote dispatch. Boolean comparisons support eq/ne only.

Custom definitions do not accept raw WQL, SELECT *, method calls, association
queries, expressions, arbitrary transforms, array types, or embedded objects.
Built-ins retain their separately implemented arrays, methods, and namespaces.
These are capability limits, not claims that every Windows provider is available.

### Migration And Recovery

The first valid version-1 load creates a byte-for-byte `commands.json.v1.bak`,
then atomically writes version 2. IDs, scripts, parameters, descriptions, and
custom entries are preserved; edited built-in scripts do not gain implicit WMI
support. A conflicting existing backup stops migration rather than overwriting it.

Invalid saves/imports do not replace the active definitions or the last good
file. An invalid external file edit fails reload/dispatch and leaves the last
active in-memory library intact; repair that file before retrying. Duplicate
JSON keys, unsupported versions, and non-finite JSON numbers are rejected.
Do not use Reset Defaults for migration or to add a command.

### Runtime Extension Points

- `commands/wmi_definitions.py`: `CIM_TYPES`, `OPERATORS`, `validate_definition`,
  `compile_definition`, `WmiQueryPlan`, and `execute_query_plan`.
- `commands/templates.py`: versioned `CommandRegistry`, `validate_template`,
  `PreparedCommand.wmi_plan`, typed arguments, help, and atomic persistence.
- `ui/main_window.py`: `CommandTemplateDialog` and `CommandLibraryDialog`.
- `remoting/dcom_session.py` and `remoting/wmi.py`: shared plan execution with
  backend query callbacks; `session_manager.py` rejects missing implementations.

To add an operator, update the named operator/validation branches, define its
operand rules, and add positive/injection/type tests in `test_wmi_definitions.py`.
Do not accept raw query fragments. A new CIM type also needs codec support and
native normalization before registration. Methods remain source-defined workflows,
not an extra JSON operation name. Add tested JSON examples when expanding this schema.

## Coverage And Limits

All 21 existing WinRM aliases have WMI handlers on Windows and Linux. This means
alias coverage, not identical PowerShell output or proven provider compatibility.
Only Linux Connect/sysinfo have been confirmed on the user's live target. New
commands, namespaces, arrays, and method calls have offline fixture coverage.

| Command | WMI behavior |
| --- | --- |
| sysinfo | Seven OS fields; memory in KiB and boot time as CIM text |
| disk | Fixed disks, FreeGB/SizeGB; unknown sizes remain null |
| services / svc NAME | Name, DisplayName, State, StartMode; svc is an exact name |
| restart-service NAME | Stop, wait, start, wait; GUI confirmation; no forced dependency stop |
| admins | Find Administrators by SID; return direct group/member object references |
| pending-reboot | Read two HKLM reboot marker keys via StdRegProv.EnumKey |
| topcpu COUNT | Top processes by cumulative CPU seconds, not utilization percent |
| procs | Process name, ID, CPU seconds, working-set bytes, executable path |
| events LOG COUNT | Classic logs; bounded record-number probes and ranges, newest count first |
| hotfixes | Win32_QuickFixEngineering; InstalledOn stays provider text |
| net | IP-enabled adapters; addresses/subnets/gateways/DNS remain arrays |
| ports | TCP listeners via root/standardcimv2 MSFT_NetTCPConnection |
| shares | Win32_Share fields; no SMB transport or Get-SmbShare-only fields |
| users / groups | Local account/group native fields; no inferred last-logon dates |
| firewall | root/standardcimv2 MSFT_NetFirewallProfile; numeric policy values |
| tasks | root/microsoft/windows/taskscheduler MSFT_ScheduledTask; numeric State |
| startup | Win32_StartupCommand inventory |
| sessions | Interactive security logons, not quser terminals or idle times |
| uptime | Target LocalDateTime minus LastBootUpTime with CIM UTC offsets |

Run `ALIAS -h` for parameters and detailed backend notes. Target provider availability
and namespace permissions vary. No SMB, remote shell, Impacket, or WinRM fallback.

Each command shares a 60-second execution deadline, plus bounded cleanup. Queries
have a 10000-row cap and a 32 MiB aggregate wire/decoded JSON cap. An extra row is
fetched when necessary to distinguish exact-limit completion from truncation.
Limits and timeouts fail explicitly without returning partial rows as success.
An explicit QuerySpec `take` stops enumeration at a requested prefix, without an
extra-row probe. It does not imply ordering: only use it where any matching row
is sufficient, such as an existence probe.

Events finds the highest available RecordNumber using one-row probes (binary
search when necessary), then retrieves bounded descending record-number ranges.
Only the requested event records are downloaded in full, even for logs larger
than 10000 entries. Sparse ranges are handled by seeking the next lower record;
provider order and NumberOfRecords are not treated as latest-record guarantees.
The command retains its shared deadline and 32 MiB result budget. New arrivals
above the selected upper bound are excluded. No transactional snapshot or uint32
record-number-wrap guarantee; detected log changes fail explicitly.

Provider rejections, row/size caps, completed enumeration timeouts, and decoding
errors inside a recognized by-value WMI result release the query enumerator and
retain the session. Unknown result references, broken RPC/authentication, and
unconfirmed cleanup still invalidate the affected connection. Optional namespace
login, operation, and ping failures do not close the primary session. A failed
auxiliary connection is removed from the cache; a later explicit command can open
a replacement. Local result-formatting errors do not close healthy connections.
Known by-value method results that cannot be decoded retain the channel but report
an unknown action outcome. Never treat an unreadable or lost method reply as confirmed
to retry; there is no automatic command replay.

Restart changes the target. An already stopped service is started. Failure after
StopService can leave it stopped; uncertain replies are never retried automatically.
Inspect the real service state before deciding to retry. Registry permission errors
are errors, never treated as absent reboot markers; those markers are not exhaustive.

## Execution Path

1. `commands/templates.py`: `BUILTIN_TEMPLATES`, arguments, and local help.
2. `commands/wmi_catalog.py`: `WMI_CATALOG`, `QuerySpec`, `make_query`,
   `execute_command`, transforms, and `WMI_NOTES`.
3. `remoting/session_manager.py`: preflight and the dedicated host worker.
4. `remoting/dcom_session.py` or `remoting/wmi.py`: backend callbacks, namespace
   caching, results, errors, and cleanup.
5. Linux `remoting/dcom_client.py`: authenticated queries/methods and lifetime pings.
6. Linux `remoting/wmi_query.py`: typed WMIO decoder; `wmi_methods.py`: narrow method
   request/response codec and the EnumKey input writer.

Catalog aliases automatically feed both adapters and help. No extra Linux alias
list needs updating. Implicit built-in WMI eligibility still requires an unchanged
built-in PowerShell script; changing that script does not redefine WMI behavior.
An explicit validated `wmi` definition uses its own query plan instead.

## Source Recipe: Builtin BIOS Inventory

This recipe adds a shipped builtin; use the editor/import path above for a custom
query without source changes. Do not reuse an alias already present in your library.

In `src/remoteops/commands/wmi_catalog.py`, add this inside `WMI_CATALOG`, before
`WMI_ALIASES` is computed:

```python
"bios": QuerySpec(CIMV2, "Win32_BIOS", {
    "Manufacturer": CIM_STRING,
    "SMBIOSBIOSVersion": CIM_STRING,
    "SerialNumber": CIM_STRING,
}),
```

Add a `bios` entry in `WMI_NOTES` describing the output. Verify projected property
CIM types against the provider documentation. Incorrect types fail decoding.

Add this item to `BUILTIN_TEMPLATES` in `commands/templates.py`:

```python
CommandTemplate(
    name="BIOS inventory",
    alias="bios",
    description="Show BIOS manufacturer, version, and serial number.",
    script="Get-CimInstance Win32_BIOS | Select-Object Manufacturer, SMBIOSBIOSVersion, SerialNumber",
),
```

A plain SELECT in a supported namespace needs no extra executor branch. Both WMI
backends use the query; WinRM uses the script.

Add a fixture test in `tests/test_wmi_catalog.py`. Use `instance_fixture` from
`tests/test_wmi_values.py` to check class identity, property types, nulls, and Unicode.
The catalog tests already iterate every simple query and check builtin parity;
update the explicit 21-command count when intentionally adding an alias. Verify
`prepare_command("bios").native_wmi` and `bios -h`.

Existing `commands.json` is deliberately preserved on upgrade. After reinstalling,
use Command Library -> Add with the alias, description, and exact script above,
and Parameters JSON `{}`. Alternatively append the equivalent entry to the existing
`commands` array in the runtime file shown by the library, then Reload File. Do not
replace the entire library or use Reset Defaults to add one command. The script
must match the builtin exactly for WMI eligibility.

Run `bios -h`, then explicitly test `bios` on a lab host. Keep changes in this repo,
not just in the installed wheel. Synthetic fixtures do not prove provider availability.

## Recipe: Parameters, Filters, And Transforms

1. Add the parameter default and optional `_schema` to the builtin's `parameters`.
   `_positional` lists positional names; otherwise use `--parameter-name VALUE`.
2. Add defaults and validation in catalog `_values`. This independently validates
   workflow arguments and rejects names not supported by that WMI command.
3. Compile the filter in `make_query` using `replace(spec, where=...)` and
   `wql_literal` for values. Never interpolate class/property names or raw clauses.
4. Add result processing in `_transform` only when needed. Keep units, nulls, and
   unavailable fields explicit; document differences in `WMI_NOTES`.
5. Test defaults, named/positional forms, invalid ranges, missing values, quotes,
   backslashes, Unicode, control characters, and attempted query fragments.

`svc` is the working reference:

```python
where = "Name=" + wql_literal(parameters["service_name"])
```

WQL escaping differs from PowerShell escaping. QuerySpec's `where` is trusted
Python-authored syntax, not a public raw-WQL field. New operators require a compiler
branch and tests. Result transforms are named Python handlers, never `eval`.

## Recipe: New Types, Methods, Or Namespaces

Scalar CIM types and one-dimensional arrays are decoded by `VALUE_FORMATS` and
`_value` in `wmi_query.py`. Arrays use `base_type | 0x2000`. `decode_instance`
handles nulls and defaults. Embedded objects remain unsupported.

For a new type: check MS-WMIO, implement bounded decoding, update QuerySpec's allowed
types and native normalization, and add independent valid/malformed/truncated
fixtures. Preserve memory limits, class/type checks, and outer-padding rules.
Class validation accepts the requested class or a subclass whose encoded derivation
list names it; the instance must still match its own definition. Do not accept an
unrelated class just because it has properties with the same names.
Do not fix type errors by disabling validation or guessing from property names.

Multi-step commands use `execute_command` and `_Execution`; each query, method,
and poll shares one deadline. Use supplied callbacks to preserve thread ownership,
namespace lifetime, and action history. Existing workflows are `admins`,
`pending-reboot`, `events`, and `restart-service`. Events uses `QuerySpec.take=1`
only for existence probes; its full-record range queries enumerate completely.

`wmi_methods._validate_method` only allows Win32_Service StartService/StopService
with no inputs, and StdRegProv.EnumKey with uint32 hDefKey and string sSubKeyName.
A new method needs validation, an appropriate input writer, an output schema in
`DcomClient.invoke`, native handling if needed, a catalog workflow, and tests.
Adding a name alone is insufficient. Declare mutations in help and add GUI
confirmation for mutating commands. Never replay a mutation after an uncertain reply.

For another namespace, update the module constant `SUPPORTED_NAMESPACES` in
`dcom_client.py`, declare it in QuerySpec, and test failure, reuse, maintenance,
and cleanup. Linux retains one client per used namespace on the same host. Session
credentials stay in memory for on-demand logins and are cleared from session
references at disconnect; they never go in command definitions or history.

## Test And Package

From the repository root on Windows:

```powershell
$env:PYTHONPATH = 'src'
.\.venv\Scripts\python.exe -m unittest discover -s tests -q
.\packaging\linux\build_tarball.ps1 -PackageName remoteops-linux-console-fixes-preview
```

On Ubuntu, close WMIBench and extract outside the install directory:

```bash
tar -xzf remoteops-linux-console-fixes-preview.tar.gz
cd remoteops-linux-console-fixes-preview
bash packaging/linux/install.sh
~/.local/bin/remoteops
```

The archive includes this guide and tests. User data stays in
`~/.local/share/RemoteOps`. The standalone `~/remoteops-diagnostic` is not updated.
Start with read-only queries and idle/reconnect checks. Use only a deliberately
chosen lab service for restart testing.

## References

- [WQL](https://learn.microsoft.com/en-us/windows/win32/wmisdk/wql-sql-for-wmi)
- [Win32_BIOS](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/win32-bios)
- [WMIO values](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/f58dfade-6437-4e8b-a1f1-a97b7a713a21)
- [WMIO arrays](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/2c9f5110-86be-4c21-8a89-97c0fc724774)
- [ExecMethod](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/cbce28cd-fd45-4df1-9ec8-416e6bb33691)
- [Registry provider](https://learn.microsoft.com/en-us/windows/win32/wmisdk/obtaining-registry-data)
