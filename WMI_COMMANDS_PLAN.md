# Extensible WMI Commands

Status: custom query authoring v1 implemented; remaining items are a roadmap.

Update (September 24): version-2 libraries, backed-up migration, typed custom
SELECT definitions, parameter binding, shared Windows/Linux execution, editor
tabs/presets/local preview, duplicate/import/export, and tested BIOS/service examples
are implemented. `docs/ADDING_WMI_COMMANDS.md` is the authoritative format: the
shipped `properties` field maps names to explicit CIM type names, unlike the
earlier proposal below. Custom definitions currently require root/cimv2 and scalar
types. The editor uses JSON for properties, filters, and parameters; dedicated row
builders, editor-host Test, dynamic response-type inference, and partial-result
completeness metadata remain future work. No partial rows are returned as success.
The source-defined catalog also covers all 21 existing WinRM aliases. The baseline
and proposed format below are historical design context, not a current schema.

## Objective

Make ordinary WMI query commands data-driven, and leave a documented extension
path for adding capabilities the backend does not yet support. The owner must be
able to add commands and supported options without relying on another agent to
rediscover the implementation.

Deliver both a command editor and a maintainer cookbook with working examples,
registration points, tests, packaging instructions, and explicit limitations.

## Current Baseline

- The command library stores PowerShell templates in `commands.json` beside the
  application database. `CommandTemplate` currently requires a script.
- `commands/templates.py` derives WMI eligibility from built-in alias membership
  and exact PowerShell script equality. Custom WMI definitions are not supported.
- Windows `remoting/wmi.py` maps supported aliases to hard-coded WQL, with some
  alias-specific result processing.
- Linux `remoting/dcom_session.py` permits only `sysinfo`.
- Linux `remoting/wmi_query.py` encodes a fixed OS query and decodes only its
  projected class and property types. `dcom_client.py` expects one OS instance.
- Both backends use the existing host worker, results table, and command history.
- The user has confirmed Linux Connect and sysinfo work after the padding fix.
  Repeated queries, long idle sessions, and broader interoperability still need
  live acceptance tests.

## Two Extension Levels

### Level 1: Add A Command Definition

For a query using existing capabilities, specify the alias, class, selected
properties, typed parameters, filters, and display columns. No Python changes or
application rebuild should be necessary. Save through the editor or import a
validated JSON definition; execution reloads the library as it does today.

The first release targets scalar-property SELECT queries in the connected host's
`root/cimv2` namespace. The command must not silently switch namespaces or transports.
Unknown classes/properties can be authored manually; validate identifiers locally
and let an explicitly requested test report target-specific availability.

### Level 2: Add A Supported Capability

Adding a previously unsupported CIM type, filter operator, result formatter, or
operation requires source changes and tests. Each capability needs a named
implementation, explicit backend support, and a cookbook entry. A JSON flag alone
must never claim that a backend implements a capability.

First release implements queries only. WMI methods such as service start/stop are
a separate milestone requiring method argument/result codecs, target selection,
confirmation, auditing, and a no-automatic-retry policy for uncertain outcomes.
No PowerShell execution, SMB, Impacket, or WinRM fallback through WMI definitions.

## Definition Format

Upgrade the library envelope to version 2. Retain existing identity, alias,
description, and parameter conventions. Add an optional `wmi` implementation
alongside the optional PowerShell `script`, so one alias can support both backends.
At least one implementation is required. Backend selection follows the host;
missing implementations fail locally instead of falling back.

Proposed WMI-only entry, not accepted by the current application:

```json
{
  "name": "Service status",
  "alias": "my-service",
  "description": "Read status for a named service",
  "parameters": {
    "service_name": "Spooler",
    "_positional": ["service_name"],
    "_schema": {
      "service_name": {"type": "str", "description": "Service name"}
    }
  },
  "wmi": {
    "operation": "query",
    "class": "Win32_Service",
    "properties": ["Name", "DisplayName", "State", "StartMode"],
    "filters": [
      {"property": "Name", "operator": "eq", "parameter": "service_name"}
    ],
    "limits": {"rows": 1000, "timeout_seconds": 30},
    "columns": ["Name", "DisplayName", "State", "StartMode"]
  }
}
```

Initial filters are AND-combined comparisons (`eq`, `ne`, `lt`, `le`, `gt`, `ge`)
and explicit null checks. Each comparison takes either a typed literal or a named
parameter, never an interpolated WQL fragment. Exclude raw WQL, arbitrary
expressions, LIKE patterns, associations, event queries, and SELECT * initially.
Document how to extend each supported operator deliberately.

Use one identifier validator and one WQL literal encoder. Do not reuse PowerShell
escaping. WQL has its own escaping and query restrictions; references are below.
Namespace, class, and property names cannot be supplied through value parameters.

## Runtime Design

1. Parse and validate the complete library before replacing active definitions.
   Reject unknown schema versions and unsupported operation names explicitly.
2. Prepare a backend-specific immutable command containing validated arguments,
   query specification, limits, and display options. Replace the `native_wmi`
   boolean and scattered alias allowlists with implementation/capability checks.
3. Compile supported query specifications to WQL through shared code used by both
   native Windows and custom Linux clients.
4. Generalize Linux's decoder by separating the WMIO envelope/class/property
   metadata from value decoding. Determine CIM types from the response, not guesses
   based on property names. Preserve nulls, defaults, Unicode, integer precision,
   datetime strings, and reference strings. Reject unsupported types explicitly.
5. Generalize enumeration to zero, one, or many records. Keep one-object Next calls
   initially to avoid coupling this feature to multi-object NDR array support.
   Loop with a shared deadline, total-byte limit, and row cap; retain the existing
   per-response size bounds. Empty results are a successful query outcome.
6. Always release enumerators and synchronize reference/ping tracking, including
   early limits and decoding failures. Preserve explicit reconnect after session
   loss and existing thread ownership. Do not weaken protocol validation.
7. Represent completeness separately from command success. Persist whether a
   result was complete, row-limited, timed out, or cancelled. Partial rows must be
   visibly marked in the table, terminal, export, and history, never shown as a
   complete successful result. Probe for another row when needed to distinguish
   exact-limit completion from truncation.
8. Keep connect/health checks independent of editable command aliases. Preserve
   the fixed sysinfo wrapper as a regression check while moving onto shared code.

Start with scalar strings, booleans, signed/unsigned integers, floating-point
values, datetime text, and reference text. Defer arrays and embedded objects;
document those as unsupported and provide the decoder extension procedure. Array
support is required before advertising commands such as network address inventory.

## Authoring UI

Extend the existing Command Library rather than building a separate application.

- Add backend availability indicators and Duplicate, Import, and Export actions.
- Use PowerShell and WMI tabs for a command's implementations.
- WMI fields: operation, class, ordered property list, filter rows, typed parameter
  rows, display columns, row limit, and timeout. Advanced JSON edits use the same
  schema and validation as the form.
- Show a generated, non-editable WQL preview with sample parameter values.
- Separate Validate (local only) from Test (explicitly executes on one selected
  host). Test uses the normal session worker and history path; saving never queries.
- Test results show rows, completion state, and actionable errors, including which
  property/type or capability is unsupported. No automatic discovery scans.
- Initial creation paths: Blank query, Duplicate existing command, or starter
  examples for BIOS, services, disks, and processes. A live class browser is later.

## Maintainer Cookbook: Required Deliverable

Create `docs/ADDING_WMI_COMMANDS.md` with these tested recipes. Every recipe must
name the actual files and symbols after implementation, not approximate pseudocode.

| Change | Recipe must cover |
| --- | --- |
| New scalar query | Complete BIOS example JSON, editor steps, import, help, execution, and expected columns |
| New CLI parameter | Default/required value, positional and named use, type validation, safe filter binding, help |
| New filter operator | Schema enum, shared WQL compiler branch, capability declarations, escaping and rejection tests |
| New CIM value type | Wire representation, bounds/null/default handling, decoder registration, native normalization, fixtures |
| New result formatting | Named local formatter, explicit input/output fields, null behavior, no eval, raw-value preservation |
| New operation | Dispatcher and backend implementation, capability registration, lifecycle/cleanup, tests; explain that method calls are not query templates |
| New built-in command | Definition registration, non-destructive library migration, example usage, platform support |
| Package an extension | Local test commands, Linux archive build, reinstall, preserved user data, manual acceptance checks |

Include `examples/wmi/` definitions that are loaded by automated tests so examples
cannot silently drift from the schema. Leave a developer checklist covering model,
compiler, executor, help/editor, documentation, tests, and packaging. A capability
table must distinguish implemented, unsupported, and target-dependent behavior.

For the current code, extension work crosses `core/models.py`,
`commands/templates.py`, `remoting/session_manager.py`, `remoting/wmi.py`,
`remoting/dcom_session.py`, `remoting/dcom_client.py`, and `remoting/wmi_query.py`.
Keep command schema/compiler code in `commands/`; keep protocol decoding and
backend execution in `remoting/`. Add a helper module only for genuinely shared
logic. Do not add a dynamic Python plugin loader for this feature.

## Compatibility And Migration

- Back up the version-1 command library before an atomic version-2 migration.
- Preserve IDs, aliases, descriptions, user scripts, parameters, and custom entries.
- Attach WMI implementations only to recognized, unchanged built-in templates.
  Edited PowerShell scripts must not acquire unrelated WMI behavior automatically.
- Retain existing built-in output contracts where possible. Keep display transforms
  explicit and backend differences documented, particularly the existing sysinfo fields.
- Invalid imports or saves leave both active state and the last good file intact.
  Alias conflicts require a choice; imports never execute and contain no credentials.
- Add backward-compatible result completeness fields in history; old records use
  an unknown/not-recorded state rather than an invented completeness guarantee.

## Delivery Sequence And Acceptance

1. **Schema and migration:** model, compiler, capability checks, version-2 loader,
   fixtures, BIOS definition, and the first cookbook recipes. Existing WinRM and
   sysinfo behavior must remain covered by regression tests.
2. **Generic queries:** scalar decoding and bounded enumeration on Linux, shared
   query execution on Windows, completeness/history support, and reference cleanup.
   Acceptance: zero/multiple rows, repeated requests, malformed responses, timeouts,
   limits, unsupported types, and lost sessions behave deterministically.
3. **Editor and examples:** query form, duplicate/import/export, help, preview, and
   explicit test execution. Acceptance: add BIOS and service-filter commands without
   modifying source, restarting the app, or rebuilding the package.
4. **Extension handoff:** finish and verify every cookbook recipe against actual
   symbols. Exercise adding a comparison operator and a scalar decoder fixture
   through the documented path, without broad unrelated code edits.
5. **Package and user verification:** run the full local suite, rebuild the Linux
   preview, verify archive contents, and provide install/test steps. The user checks
   Connect, repeated queries, idle survival, disconnect/reconnect, parameter errors,
   empty results, and limited results on their lab target. No automatic SSH testing.

Release gate: a user can follow the documentation to add a supported query without
Python edits, and a developer can add a new supported capability using named,
tested extension points. Do not label all WMI classes or operations supported.

## Verification Matrix

- Schema: legacy migration, modified built-ins, duplicate aliases, malformed imports,
  unsupported versions/operations, missing parameters, and atomic failure recovery.
- Compiler: apostrophes, quotes, backslashes, Unicode, control characters, nulls,
  booleans/numbers, invalid identifiers, and attempted query-fragment injection.
- Decoder: every scalar width, null/default/inherited values, overflow, unknown types,
  valid outer padding, malformed inner blocks, and truncation at byte boundaries.
- Session: zero/many objects, exact-limit versus over-limit results, aggregate byte
  bounds, finite deadlines, partial responses, release failures, and persistent reuse.
- UI/history: both backend implementations, unsupported capabilities, local-only
  validation, explicit test dispatch, import rollback, and completeness after reload.
- Documentation: example definitions validate, compile, produce help, and run with
  fixture-backed executors. No test depends on access to the user's live machines.

## Protocol References

- [WQL overview](https://learn.microsoft.com/en-us/windows/win32/wmisdk/wql-sql-for-wmi)
- [WQL operators](https://learn.microsoft.com/en-us/windows/win32/wmisdk/wql-operators)
- [WQL escaping](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.core/about/about_wql)
- [WMIO EncodingUnit](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/5a3c19d1-f85f-4596-a3d3-33ea734aac9d)
