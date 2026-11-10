# WMIBench Backlog

## Credential Management And Console Layout - October 16, 2026

Credentials now supports editing, password replacement/removal, and confirmed
deletion with host-assignment cleanup. Saved secrets use authenticated encryption
in SQLite and a local key (Windows DPAPI or Linux owner-only permissions), with
automatic reuse and legacy keychain migration. Profile/password writes are atomic;
missing keys or invalid ciphertext fail visibly without overwriting existing data.
Active profiles cannot be edited/deleted until their hosts are disconnected.
The host rail is the sole target navigator, with context-menu connect/disconnect
and edit actions. Horizontal host tabs and duplicate center headings are removed.
Larger text, explicit theme-contrast library icons, a consolidated terminal toolbar,
better history column widths, and full-height inspection tables reclaim workspace.
Service/event details use a separate window; event timestamps display local time
while raw values remain available. History has resizable list/output panes.
The new archive is `wmibench-linux-console-fixes-preview.tar.gz`; earlier previews
are preserved. Local tests/Qt captures only, no live remote or SSH testing.

## Command Forms And Inspection Views - October 16, 2026

Schema-driven argument forms now support builtin and custom commands, typed values,
choices, bounds, required fields, and safe command-line quoting. Single-host forms
filter by backend; batch forms retain read-only eligibility and preflight. Accepting
only inserts the command. Services provides loaded-state filtering, inspection, and
confirmed restart through the existing execution path, followed by a read-only
refresh on success. Events provides bounded log/count fetches and local provider,
ID, level, time, and text filters with full message/raw details. Dedicated views use
unchanged builtin scripts and frozen prepared commands. Failed refreshes retain old
records but disable stale service actions; endpoint/profile changes clear results.
Small-window views scroll without collapsing tables, and light/dark themes are
covered by offscreen Qt captures. No new protocol methods, standalone service
start/stop, server-side event filters, or older-event pagination were added.
The separate package is `wmibench-linux-workspaces-preview.tar.gz`. Verification
is local fixtures and Qt only; no SSH, live target tests, or remote deployment.

## Read-Only Multi-Host Execution - September 27, 2026

The Multi-host dialog adds searchable checkbox selection, backend/read-only
preflight, explicit approval of new connections, UI-thread credential resolution,
and frozen command definitions. A bounded batch runner dispatches 1..12 hosts
(default 4), preserving each host's dedicated worker and existing sessions.
Only unchanged audited builtin scripts and validated WMI SELECT definitions qualify;
bulk service changes and arbitrary custom PowerShell are excluded. Failed or lost
sessions are not silently reconnected and commands are never replayed. Cancellation
skips queued hosts while dispatched work finishes. Closing waits for active work.
Per-host status/output, combined namespaced records, full-batch CSV, and individual
history are available. The combined display caps at 10000 rows; full CSV is uncapped
by that display limit. Previous previews are preserved; this build is packaged as
`wmibench-linux-multihost-preview.tar.gz`. Local fixture and Qt tests only.

## Command Organization And Failure Recovery - September 27, 2026

Command Library now has combined text/category/backend/favorites filters, persisted
categories and favorites, and favorite-first ordering. The host command picker
inserts compatible aliases without executing them. Version-2 definitions accept
optional category/favorite metadata; existing libraries receive display defaults.
Terminal failures show short explanations and actual connection status, with a
copyable latest-diagnostic dialog and original command stderr retained in History.
Worker and local result-processing errors do not imply a disconnected session.
Auxiliary WMI operation/ping failures retire only that namespace; known by-value
method decoding failures retain channels but explicitly warn of unknown outcomes.
Native provider health-check/access errors retain sessions. WinRM timeouts retain
the pool only after confirmed stop and successful pipeline cleanup. Broken primary
transport, unknown remote references, and unconfirmed cleanup still require reconnect.
No mutation replay, live target tests, or SSH deployment. Packaged separately as
`wmibench-linux-usability-preview.tar.gz`.

## Custom WMI Query Authoring - September 24, 2026

Version-2 command libraries support read-only custom WMI SELECT definitions in
root/cimv2, explicit scalar CIM types, typed filter binding, columns, and row/time
limits. The existing editor now has PowerShell/WMI/Parameters tabs, BIOS/service
presets, local validation and WQL preview, backend indicators, duplicate, import,
and selected export. Queries use the existing Windows/Linux sessions and history.
Version-1 migration preserves data and makes a byte-for-byte backup; invalid
definitions or failed writes do not replace the active library. No automatic
remote tests, discovery, or transport fallback. Working examples and the runtime
extension recipe are included. Dedicated filter-row builders, editor-host Test,
response-type inference, and partial-result metadata remain later work.
The authoring preview is packaged separately from the service/events fix archive.

## Service, Event, And Session Error Fixes - September 24, 2026

User testing reported unexpected CIM classes for service commands, whole-log row
limits even for `events System 5`, and forced disconnects after command errors.
The decoder now validates subclasses through their encoded derivation lists.
Events uses bounded existence probes and descending record-number ranges instead
of downloading and sorting an entire log. Query/provider failures clean up the
temporary enumerator and retain the primary session; transport/authentication or
uncertain reference failures still require explicit reconnect. Optional namespace
login failures are isolated from the primary session. Regression coverage includes
service subclasses, logs above 20000 records in different provider orders, sparse
records, clean error recovery, and subsequent sysinfo on the same associations.
No live target or service operation was used to verify these fixes.

## WMI Builtin Catalog Expansion - September 22, 2026

The user confirmed Linux Connect/sysinfo after the padding fix and asked for the
same commands as WinRM. A shared catalog now implements all 21 aliases, with typed
scalar/array decoding, bounded enumeration, namespace client reuse, native Windows
integration, and narrow service/registry method workflows. Restart prompts in the
GUI and never retries an uncertain method outcome. Provider differences and event
scan limits are documented in command help.

`docs/ADDING_WMI_COMMANDS.md` records actual extension points, a BIOS example,
parameter/type/method recipes, and test/package instructions. The planned runtime
JSON WMI editor is not implemented by this expansion. New provider and method
interoperability has offline fixtures only; no live remote testing or SSH.

## User Test Package - September 22, 2026

The user requested packaging the existing work now, with no additional commands.
`dist/wmibench-linux-wmi-preview.tar.gz` contains the current persistent Linux WMI
backend and sysinfo integration. Setup and manual checks are in
`packaging/linux/TESTING-WMI.md`. Packaging does not establish live interoperability.
The prior standard archive and installed Ubuntu diagnostic remain unchanged.

## Persistent Linux WMI And Sysinfo Integration - September 22, 2026

The user requested continued building before further lab tests, superseding the
earlier live-gate prerequisite. Parts 5 and initial 6 are now implemented in source:
retained authenticated RPC associations, namespace reuse, temporary query enumerator
release, DCOM ping sets, and per-host serialized lifetime maintenance. Linux uses
the experimental custom client for sysinfo; Windows retains native COM. Results
reach tables and history, and lost sessions require explicit reconnect.

Only NTLM, numeric IPs, root/cimv2, and sysinfo are supported on the custom backend.
Protocol pings remain active when optional health checks are disabled. Queries are
bounded to 60 seconds; cleanup is bounded and reports uncertainty without retries.
Shutdown attempts all hosts even when one cleanup fails. All 183 local tests pass,
covering protocol/session lifetime and Qt integration. No new live tests, SSH, deployment,
installer rebuild, target policy changes, Impacket, SMB, or fallback transport.

Next development phase: extend the decoder and add read-only commands individually.
Deferred combined lab pass: OS query, repeated sysinfo, idle survival, disconnect,
connection loss/reconnect, and Linux UI responsiveness. Live readiness is unverified.

## OS Query Implemented - September 22, 2026

Part 4b adds `--query-os`: fixed read-only Win32_OperatingSystem SELECT, bounded
enumeration, structured JSON, and verified release of login/services/enumerator
references before success. The application-owned WMIO decoder preserves nulls,
Unicode, class defaults, uint64 values, and CIM datetime text. Protected RPC reply
fragment assembly is bounded to 1 MiB and 512 fragments, with per-fragment verification.

151 local tests pass. A separate read-only Windows native-marshaler smoke check
decoded seven OS properties from 1356 bytes and matched native WMI values exactly.
SSH, Ubuntu deployment, and live query testing were explicitly deferred by the user.
Part 4b's live gate remains pending; the persistent Ubuntu diagnostic still has part 4a.
Next: resume that gate when requested, then part 5 session reuse. GUI integration and
installer rebuild remain later work. No Impacket, SMB, WinRM fallback, or policy changes.

## Namespace Access Passed - September 21, 2026 (EDT)

Part 4a adds `--namespace root/cimv2` to the persistent Ubuntu diagnostic. Protected
NTLMLogin returns IWbemServices, then both login and services references are released.
The live check passed at 2026-09-22 00:43:25 UTC; a nonexistent namespace returned
0x8004100e at 00:43:52 UTC with login cleanup confirmed and exit code 1.
128 local tests and 85 Ubuntu protocol tests pass. No queries, process creation,
GUI integration, installer rebuild, or target policy changes. No Impacket/SMB/WinRM.
Namespace denial is fixture-tested only. Cross-exporter resolution remains unsupported.
Next: part 4b, one read-only OS query and structured result decoding.

## DCOM Gate Passed - September 21, 2026 (EDT)

Fixed the decoder rejecting nonzero ORPCTHAT flags: MS-DCOM requires ignoring them.
Non-null extensions still fail explicitly, with the actual header values reported.
111 local tests and 68 Ubuntu protocol tests pass. Updated the persistent standalone
diagnostic; it is not the full GUI package.

At 2026-09-22 00:34:58 UTC, live Ubuntu-to-Windows activation obtained the WMI login
interface, reached its TCP exporter, and confirmed protected reference release.
Part 3's lab gate is complete. The earlier access-denied blocker below is historical;
its cause was not established and no target settings were changed by the agent.
Next milestone: namespace login and one read-only OS query, not yet implemented.

## DCOM Milestone - September 21, 2026

Part 3 now has a standalone activation/release diagnostic and bounded NDR32 codecs,
sharing the protected RPC association from part 2a. No Impacket, SMB, WinRM fallback,
namespace queries, GUI integration, or installer rebuild. Regression results:
106 tests pass on Windows and 63 protocol-focused tests pass on Ubuntu.

**Live gate remains incomplete:** ServerAlive2 discovery succeeds, but activation
returns unverified RPC access-denied fault 0x00000005. A separate protected ServerAlive
call with the same credentials succeeds. Obtain target DistributedCOM event details
and verify remote activation rights; continue checking client interoperability rather
than assuming a policy problem. No target settings have been changed. Successful
activation decoding, exporter access, and release are fixture-tested only. Complete
that live gate before implementing namespace login/query in part 4.

## RPC Milestones - September 20, 2026

Custom Linux-client parts 1 and 2a are implemented as separate diagnostics:
unauthenticated RPC bind, then NTLMv2 with packet privacy/header signing and a
verified read-only ServerAlive reply. Both have live Ubuntu-to-Windows validation.
No Impacket, SMB, WinRM fallback, GUI changes, or WMI activation. Kerberos, DCOM
object activation, native WMI queries, and session integration remain pending.
See WMI_PLAN.md and README.md for scope and test commands. Do not store lab passwords
in this backlog, scripts, tests, or command-line arguments.

## Implementation Status - September 18, 2026

Implemented in 0.2.0: retained single PSRP runspace, serialized per-host work,
configurable idle checks, searchable stored history and output, registered-command
enforcement with typed arguments and local help, structured result tables, persisted
dark mode, and chunked WinRM file transfers with cancellation and SHA-256 checks.
Saved command-library entries are allowed; raw terminal PowerShell is rejected.

WMI is partially implemented using our native Windows COM adapter, with a read-only
native command subset. User constraints: **no Impacket and no SMB on WMI hosts**.
Direct Linux WMI/DCOM is still outstanding. There is no SMB or WinRM fallback in
WMI mode and no file transfer on WMI-only hosts. See WMI_PLAN.md for current scope.

Validation includes local tests and local Windows COM/PowerShell smoke checks;
remote enterprise WinRM/WMI and Kali validation still require target-environment testing.

The original requirements below remain the reference for further work.

Saved September 13, 2026. Planning only; these features are not implemented by
this document. The user approved the Modern Ops Console design and requested
that these ideas be saved for a later session because usage was running low.

Keep the current compact host sidebar, per-host tabs, and terminal-centered
workflow as the design baseline.

## 1. Session Persistence and Idle Keepalive

- Keep an authenticated remote session alive between commands, including long
  periods of inactivity. Ordinary commands must reuse the same remote runspace.
- Audit the current PSRP implementation first: reusing a client object is not
  sufficient proof that the remote runspace persists across invocations.
- Add configurable idle keepalive and health checks, serialized with command
  execution so they cannot collide with an active operation.
- Reflect lost connections honestly in the UI. If a session expires or a network
  interruption requires reconnecting, make the loss of remote session state clear.
- Explicit disconnect must stop keepalive and release the session.
- Verify with idle-period tests and a remote variable retained between commands.

## 2. Searchable Command and Output History

- Provide a populated history view using persisted command runs and their output,
  available after restarting the application. Inspect the existing SQLite command
  history storage before adding another persistence mechanism.
- Record the entered alias and arguments, host, timestamp, duration, outcome,
  stdout, and stderr. Preserve the executed command definition for traceability.
- Search command text and output; filter by host, date, and outcome.
- Selecting a run opens its complete output. Support find within output, match
  highlighting, and next/previous match navigation.
- Keep this accessible from the host workspace without disrupting its live terminal.

## 3. Registered Commands Only

- Only commands explicitly provided by the application's approved command library
  may execute. Unknown aliases must produce a local error and send nothing remotely.
- Remove the existing fallback that executes unrecognized input as raw PowerShell.
- Validate arguments before dispatch. Reject command chaining and prevent argument
  values from introducing arbitrary PowerShell into command templates.
- Preserve runtime command-library maintenance without rebuilding. Before implementing,
  clarify whether user-added library entries count as approved commands or whether
  execution must be restricted strictly to the shipped built-ins.
- Apply the restriction to history reruns and every other execution entry point.

## 4. Structured Table Output

- Commands should return structured records where possible, with a readable table
  view alongside the terminal representation.
- Prefer structured PSRP objects or explicit structured output over parsing formatted
  PowerShell text. Inspect the transport's output capabilities before choosing a format.
- Support sortable columns, resizing, searching/filtering, and copying selected data.
- Preserve raw output and full errors when output is mixed, nested, or not tabular.
- Retain structured results in history so past runs can also be viewed as tables.

## 5. Dark Mode

- Add a complete dark theme using the approved Modern Ops Console layout.
- Include host list, tabs, credentials, command library, history, tables, dialogs,
  input fields, selection states, and output tools, not only the terminal.
- Provide a light/dark preference and persist it between launches.
- Keep connection status colors and text readable in both themes.

## 6. Upload and Download

- Upload local files to a selected host and download remote files to the client.
- Support Windows and Linux clients without requiring local PowerShell on Linux.
- Expose transfers within the host workspace with source/destination selection,
  progress, cancellation, completion, and full error feedback.
- Handle overwrite choices, large files, interrupted transfers, and remote permissions.
- Coordinate transfers with the persistent session; do not silently execute arbitrary
  user-supplied commands as a transfer mechanism.

## 7. Command Help and Optional Arguments

- Every supported alias accepts `-h` and displays its help locally without connecting
  to a host or executing the command.
- Show purpose, usage, positional arguments, available optional arguments, defaults,
  accepted values, and examples. Identify commands that modify the remote system.
- Keep help, validation, command-library display, and argument handling driven by the
  same command metadata so they stay consistent as commands evolve.
- Example: `events -h` lists the supported event-log name, count, and other options
  actually implemented by that command.

## 8. Optional WMI Connection Mode

- Add a per-host choice of WinRM/PSRP or WMI over DCOM for machines without WinRM.
- Keep the same host workspace and approved command catalog, with backend-specific
  implementations and capability reporting.
- See [WMI_PLAN.md](WMI_PLAN.md) for the cross-platform backend, connection lifecycle,
  native WMI commands, optional SMB output/transfers, limitations, and test plan.
- Establish whether affected hosts also allow SMB before promising full output and
  file-transfer parity. WMI connections are not persistent PowerShell runspaces.

## Suggested Implementation Order

1. Audit remote runspace persistence and implement idle keepalive.
2. Define command argument metadata, registered-command enforcement, and `-h` help.
3. Add structured results and searchable history, including find within output.
4. Add table presentation, dark mode, and file transfers.

This order is a proposed dependency sequence, not a change to the requested scope.
