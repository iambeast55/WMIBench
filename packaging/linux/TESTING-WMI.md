# WMI Catalog Preview Test Guide

The preview implements WMI handlers for all 21 existing WinRM aliases, using native
COM on Windows and custom NTLM/DCOM on Linux. Provider fields and behavior can hmmdiffer
from PowerShell; run `ALIAS -h` for those differences.

The user has confirmed Linux Connect/sysinfo on their target. Additional commands,
namespaces, arrays, and methods are fixture-tested, not yet live-verified. No SSH
deployment, live query, or service change was performed for this expansion.

## Install And Launch

Use a Linux desktop with Python 3.12+, venv support, and dependency download access.
Close WMIBench; extract outside the application's install directory.

```bash
tar -xzf wmibench-preview.tar.gz
cd wmibench-preview
bash packaging/linux/install.sh
~/.local/bin/wmibench
```

Application: `~/.local/share/remoteops`. Saved hosts/history/command-library data
remain in `~/.local/share/RemoteOps`. Existing builtin aliases gain WMI support
without Reset Defaults. Customized builtin scripts do not gain implicit WMI support;
an explicit validated WMI query can be added separately.
The standalone diagnostic at `~/remoteops-diagnostic` is not updated.

## Python Commands Without Rebuilding

1. Open Command Library, then Python commands. Create `my-bios`, open its folder,
   and optionally run its offline tests. Discovery must not execute code or connect.
2. Run `my-bios -h`, then `my-bios` on a connected lab
   WMI host. Check the terminal and History, including its source digest and table.
3. Edit a returned column name and save. Run again on the
   same connection; the new column must appear without restarting or packaging.
4. Introduce a syntax error. New runs must fail with a diagnostic, not run stale code.
   Repair it and run again. Next introduce a runtime exception; the command must
   fail with source details while a healthy session remains connected.
5. Remove the folder; future preparation must reject it. Restore it, close the
   app and reopen: the command must remain available. Change source while
   closed: the next launch must use it without activation.
6. Install each `examples/plugins/` folder; installation must make it available without executing it.
   Run each offline test suite. Try `service-check Spooler` on WMI, and the explicit
   `machine-summary` implementation on WMI and WinRM hosts separately.
7. In a multi-host run, Python commands must be skipped without the separate trusted
   Python opt-in. With opt-in and confirmation, inspect independent results. Commands
   declaring remote changes must remain excluded. Do not use unknown Python code.
8. For a cooperative long-running test, check cancellation inside its loop. The
   terminal cancel control must stop it at a checkpoint, without disconnecting.
   Cancellation cannot force-stop arbitrary Python or roll back remote work.

See `docs/PYTHON_COMMANDS.md` for the API, limitations and trust model. Automated
fixtures cover the loader and both execution paths; no live host tests were run
for this feature.

## Python Invocation API v2

1. Read `docs/PLUGIN_API.md` and run the offline tests in `examples/plugins/service-action`
   and `examples/plugins/registry-list` before approving either command.
2. Import `registry-list`, approve its source, then run `registry-list SOFTWARE` on
   a lab WMI host. Generic invocation requires `modifies_remote: true` even for this
   read method. The GUI must ask for confirmation; cancelling must send nothing.
3. Use `service-action` only with a disposable lab service you intentionally want
   to start/stop. Check the provider ReturnValue, history's method audit, and actual
   service state. A returned code is not a guarantee a state transition has finished.
4. Try a locally invalid typed input: it must fail before invoking a method. A
   provider-reported failure must not break an otherwise healthy session. Never
   automatically repeat a method after a lost or undecodable reply.
5. With a separate test plugin, query an available additional namespace and an
   array-valued field. Compare `rows=5` (overflow cap) with `take=5` (explicit prefix).
   Enumeration order is not guaranteed to be newest-first.
6. A plugin without the remote-change flag must not call `ctx.invoke`. Commands
   declaring that flag must remain excluded from read-only batches, even with the
   trusted-Python batch opt-in checked.

These are manual lab checks, not completed live tests. Windows native COM and Linux
wire fixtures are covered offline; the generic Linux input encoding is experimental.
Both backend paths keep host ownership, bounded helper deadlines and no replay.

## Credentials And Layout Fixes

1. Open Credentials, select an existing profile, and Edit. Supply a password and
   leave Save password in database checked. Connect, disconnect, restart the app,
   and reconnect: the saved password should be reused without prompting.
2. Edit the profile name or username with Password blank. Its ID, host assignments,
   and existing saved password must remain intact. Enter a new password to replace
   it; uncheck Save password to remove it and use prompting instead.
3. Editing/deleting a credential used by connected or busy hosts must be blocked.
   Disconnect those hosts, cancel a deletion, then delete a disposable profile.
   Only that profile/password and its host assignments should be removed.
4. Right-click a host to connect/disconnect. Verify the clicked host is targeted,
   busy actions are disabled, and switching hosts preserves terminal output.
5. Verify there are no horizontal target tabs or duplicate central host headings.
   Check Terminal, History, Services, and Events at large and small window sizes,
   in both themes. Icons should be clearly visible; history columns are resizable.
6. Select a service/event row and use Open selected record details, or double-click
   it. Details should open in a larger window without squeezing the main table.

See `docs/CREDENTIALS.md` for encrypted storage, key backup, legacy migration, and
local-account security limits. This release has no live-host validation.

## Commands And Inspection Views

1. Type `events --log System --count 5` into the terminal. There must be no query or
   password prompt until Run. Check a custom parameterized WMI query and batch input too.
2. Open Services and Events without fetching: neither should connect automatically.
   Refresh services, select a row, inspect it, then filter by state or table text.
3. Only on a disposable test service, cancel Restart selected and verify no change.
   Then approve a restart. Success should refresh services once; failure must not
   retry. WMI does not force-stop dependents; WinRM may affect dependent services.
4. Fetch five events from a chosen log. Provider, event ID, level, age, and table
   text filters must operate on that loaded set without another query. Select a
   row and open its details to read the full message/raw properties. Fetch again for newer records.
5. Test a denied query, then `sysinfo` on the same connection. Previous inspection
   records remain after a failed refresh, but stale service actions are disabled.
   Changing the endpoint or credential profile clears loaded records.
6. Check light/dark themes and a small window; inspection tables use the full view.
   Terminal tools return in Terminal. History retains structured result tables.

See `docs/COMMAND_WORKSPACES.md` for scope and limitations. Standalone service
start/stop and older-event pagination are not part of this preview.

## Multi-Host Checks

Open Multi-host in the toolbar, select lab hosts, and run `sysinfo` or
`events --log System --count 5`. Existing connections are reused; disconnected
hosts require the connection checkbox and explicit confirmation. Start with
Parallel hosts = 1 to check queued cancellation, then increase to 4. Mix WMI and
WinRM hosts to check backend preflight and independent outcomes. A failed host
must not stop or disconnect healthy hosts. Bulk `restart-service` and edited
PowerShell scripts are blocked. Inspect Host results, Combined results, CSV, and
individual History. See `docs/MULTI_HOST.md` for the full workflow and safety checks.

## Organization And Error Recovery

1. In Command Library, combine search, category, backend, and Favorites filters.
   Mark `svc` as a favorite, edit its category, close/reopen, and confirm persistence.
   Export/import a custom command to check that category/favorite metadata survives.
2. Open Python commands and choose Use command for an installed plugin: it must
   populate the terminal without running or prompting for credentials.
3. Connect once, run `svc AServiceThatDoesNotExist`, then `sysinfo`. The first fails
   with a short explanation; the session remains Connected and the second succeeds
   without another password prompt. Test a denied query or unsupported class as well.
4. Select and copy the full error directly from the terminal. Check the original
   diagnostic. History should retain the original stderr from each failed command.
5. Try an unavailable auxiliary namespace command (`ports`, `tasks`, or
   `pending-reboot` on a host lacking its provider/permissions), then `sysinfo`.
   An auxiliary failure must not close the primary root/cimv2 connection.
6. On a disposable connection, interrupt network access and issue a command.
   A genuinely lost primary connection must report that explicit reconnect is
   needed, not claim it is healthy. Restore access and reconnect manually.

No failed action is replayed automatically. An unreadable method result can leave
the connection usable while the action outcome remains unknown; inspect remote
state before repeating a service action. UI/fixture tests cover these boundaries;
live target validation is still yours to perform.

## Connect

1. Select an NTLM profile (`COMPUTER\user` or `DOMAIN\user`).
2. Use a numeric Windows IP, WMI/DCOM, and `root/cimv2` as the host namespace.
3. Connect, entering the password only in the app prompt.

TCP 135 and dynamic RPC exporter ports must be reachable. The account requires
DCOM and relevant namespace permissions. Ports/firewall, scheduled tasks, and
registry commands open and retain additional namespaces on demand. Missing
providers or access rights are reported, not bypassed. Linux Kerberos, hostnames,
cross-exporter references, and WMI file transfer remain unsupported. No Impacket,
SMB, remote shell, or WinRM fallback is used.

## Read-Only Checks

```text
sysinfo
disk
services
svc Spooler
procs
topcpu 10
hotfixes
net
shares
users
groups
admins
startup
sessions
uptime
ports
firewall
tasks
pending-reboot
events System 10
```

- Inspect Terminal and History, including its structured table. Repeat without reconnecting.
- `net` returns arrays; `admins` returns direct member references; `sessions` returns
  security logons, not quser terminals. Firewall/task state is numeric.
- `pending-reboot` reads registry markers; permission errors must not appear as false.
- `events System 5` should return up to five newest records even when the log has
  more than 10000 entries. It uses one-row probes and bounded record-number ranges,
  not a whole-log download. No provider-order or uint32 record-number-wrap guarantee.
  New arrivals beyond the selected upper bound are excluded; clearing/rotation
  during retrieval may produce a command error, without disconnecting the session.
- `services` must include derived service classes such as Win32_TerminalService.
  Try `services`, `svc Spooler`, then `sysinfo` without reconnecting.
- Queries have row, byte, and shared 60-second command limits. Failure does not
  present partial records as a complete result.
- Leave idle for ten minutes; run sysinfo and a secondary-namespace command again,
  then disconnect/reconnect. Repeat with optional idle health checks disabled.
- Try `svc AServiceThatDoesNotExist`, then `sysinfo`: the command should fail but
  the session must remain Connected. Provider access errors, query limits, and
  recognized by-value result decoding errors should behave the same way.
- Broken primary transport/authentication, unknown remote result references, or
  unconfirmed primary cleanup require reconnecting. An auxiliary connection failure
  discards only that namespace, which a later explicit command can open again.
  No automatic replay occurs.

## Service Restart: Changes The Target

Only use a lab service safe to stop. This is not part of connection or health checks.
Read `restart-service -h` first. The GUI asks for confirmation before dispatch.

```text
restart-service YourLabService
```

The handler calls StopService once, waits for Stopped, calls StartService once, and
waits for Running. An already stopped service is started. Dependent services are
not force-stopped. Failure can leave the service stopped; a lost method reply is
uncertain and never automatically retried. Inspect the real state before retrying.
No remote process creation is used.

## Custom Query Authoring Checks

1. Open Command Library. Import `examples/wmi/bios.json` from the extracted archive.
   Save/import/Validate must not prompt for credentials or contact a host.
2. Run `bios -h` without connecting; then run `bios` on your WMI host. Inspect
   Terminal and History. It should fail locally on a WinRM-only host.
3. Import `examples/wmi/service-status.json`; run `my-service Spooler`.
4. Duplicate/edit the command, change its alias, save, and run it without restarting.
5. Export a selected command. Reimporting a conflicting alias must fail without
   changing the existing library. Invalid types/filters must be rejected locally.
6. Check that an upgraded version-1 library has `commands.json.v1.bak`, with all
   original commands preserved. Do not reset the library to add the examples.

## Extending And Reporting

See `ADDING_WMI_COMMANDS.md` at the archive root for actual extension points, a BIOS
example, the runtime JSON schema, parameter/type/method recipes, tests, and packaging.

Report the alias/arguments (excluding secrets), exact error, target Windows version,
Linux/Python version, whether sysinfo still works, and idle time. Do not share
passwords or authentication tokens. Cleanup warnings indicate unconfirmed release.
