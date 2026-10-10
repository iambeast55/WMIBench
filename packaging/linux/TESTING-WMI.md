# WMI Catalog Preview Test Guide

The preview implements WMI handlers for all 21 existing WinRM aliases, using native
COM on Windows and custom NTLM/DCOM on Linux. Provider fields and behavior can differ
from PowerShell; run `ALIAS -h` for those differences.

The user has confirmed Linux Connect/sysinfo on their target. Additional commands,
namespaces, arrays, and methods are fixture-tested, not yet live-verified. No SSH
deployment, live query, or service change was performed for this expansion.

## Install And Launch

Use a Linux desktop with Python 3.12+, venv support, and dependency download access.
Close RemoteOps; extract outside the application's install directory.

```bash
tar -xzf remoteops-linux-multihost-preview.tar.gz
cd remoteops-linux-multihost-preview
bash packaging/linux/install.sh
~/.local/bin/remoteops
```

Application: `~/.local/share/remoteops`. Saved hosts/history/command-library data
remain in `~/.local/share/RemoteOps`. Existing builtin aliases gain WMI support
without Reset Defaults. Customized builtin scripts do not gain implicit WMI support;
an explicit validated WMI query can be added separately.
The standalone diagnostic at `~/remoteops-diagnostic` is not updated.

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
2. Use Browse commands beside a host's input. Insert a supported alias: it must
   populate the input without running or prompting for credentials. Incompatible
   aliases cannot be inserted even with All backends selected.
3. Connect once, run `svc AServiceThatDoesNotExist`, then `sysinfo`. The first fails
   with a short explanation; the session remains Connected and the second succeeds
   without another password prompt. Test a denied query or unsupported class as well.
4. Open Latest error details using the terminal warning icon; copy the original
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

- Inspect Terminal, Table, and History. Repeat without reconnecting.
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
   Terminal, Table, and History. It should fail locally on a WinRM-only host.
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
