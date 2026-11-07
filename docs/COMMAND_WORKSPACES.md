# Commands And Inspection Views

These views use existing commands and sessions on WMI/DCOM and WinRM hosts.
Opening a view or editing a command never contacts a host. An explicit fetch uses
the normal single-host connection/credential workflow; healthy sessions are reused.

## Command Arguments

Type arguments directly, for example `events --log Application --count 5`.
Use `events -h` for options, defaults and examples. The extra command-browser and
argument-form buttons have been removed from the terminal and batch input.
The top-level Commands library remains available for managing definitions/plugins.
Types, required fields, choices and numeric bounds are checked locally, including
custom definitions. Batch preflight still checks per-host compatibility.
Arguments are retained in terminal output and history; use credential profiles for secrets.

## Services

Open Services and use Refresh services. Filter by the returned state or table text,
sort columns, and double-click a row (or use Open selected record details) for raw
details in a separate window. Inspect selected runs `svc` for
that service. Restart selected runs `restart-service` only after confirmation.
Both WMI and WinRM prompt before this system-changing operation.

On a successful restart, the view performs one read-only services refresh. A failed
restart is never replayed or treated as proof that nothing changed. Inspect remote
state before deciding whether to act again. WMI does not force-stop dependents;
the existing WinRM implementation may affect dependent services. Separate Start
and Stop actions are not provided in this preview.

A failed refresh preserves earlier records and shows the diagnostic, but disables
service actions until a fresh query succeeds. Actions also require a connected,
idle session. Changing the host endpoint or credential profile clears loaded data.
Data is a snapshot, not a live subscription; refresh before acting on older results.

## Events

Choose a log and count (1 through 10000), then Fetch recent events. This invokes
the existing bounded `events` command. Provider substring, exact event ID, level,
age, and the table's text search filter only the loaded records. Fetch again to
retrieve newer events. Changing the log/count alone does not fetch anything.

Select a row and use Open selected record details, or double-click it, to view the
full message and raw properties as plain text in a separate window. Provider
field differences are retained: for example, WMI may report numeric EventType while
WinRM returns LevelDisplayName. Age filtering accepts timezone-aware ISO or CIM
timestamps and excludes missing, invalid, or timezone-unknown values. Any loaded
time includes those rows again. A failed fetch keeps previous records and shows
the error; there are no partial-success results.

There is no server-side provider/ID/time filter or older-event pagination yet.
WMI retrieval retains its existing scan, byte, row, and time limits, including the
record-number-wrap limitation. Requesting a small count does not download a full log.

## Results And Safety

All dispatched view commands also populate Terminal and History. The redundant Table
tab is removed; History retains its structured result table and export controls. Inspection
tables support sorting, text filtering, copying, and CSV export of displayed fields.
Full raw properties remain in the details window and normal command results.
Tables occupy the full inspection view, including at small window sizes. Event
timestamps are formatted in local time; the original timestamp remains in details.
The host rail is the sole target navigator. Right-click a host to connect/disconnect;
switching hosts retains each workspace. Terminal controls only appear in Terminal.
History has resizable list/output panes, a wider command column, and a Host column
only when All hosts is enabled. Use its search icon to find text within a result.
Full error diagnostics appear directly in the console with the plain-language summary;
there is no Latest error button or separate error popup.

Dedicated view actions require the corresponding saved builtin script to be
unchanged and to have no replacement custom WMI query. A customized alias is refused
with an explanation; review it in Command Library and run it through the normal
command input instead. The prepared view command is frozen before confirmation.
Changing the library during confirmation cannot replace the action about to run.

Recoverable query/provider/local-processing failures preserve healthy connections.
Broken primary transport or uncertain reference cleanup still requires explicit
reconnect. No command is automatically retried. Verification for this preview uses
synthetic sessions, local fixtures, and offscreen Qt, not live hosts or SSH.
The installation and manual checklist are in `packaging/linux/TESTING-WMI.md`.
