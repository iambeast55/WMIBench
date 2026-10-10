# RemoteOps

RemoteOps is a cross-platform desktop console for Windows administration over persistent
WinRM/PowerShell Remoting sessions.

The core workflow is:

```text
Connect once -> keep session alive -> run many commands -> inspect results -> disconnect
```

RemoteOps is designed for Windows and Linux clients. Linux clients do not need PowerShell
installed locally; commands are sent to remote Windows hosts over PSRP/WinRM and executed on
the remote machine.

## Current status

The latest feature package is `dist/remoteops-linux-multihost-preview.tar.gz`.
It adds bounded, read-only multi-host execution with connection approval, session
reuse, queued cancellation, independent outcomes, combined results, CSV export,
and individual host history. See [Multi-Host Execution](docs/MULTI_HOST.md).
The previous organization/recovery package remains `dist/remoteops-linux-usability-preview.tar.gz`.
It retains WMI handlers for all 21 builtin WinRM aliases and custom read-only
queries, and adds command search, categories, favorites, backend filters, and an
insert-only command picker. Errors have plain-language summaries, a copyable
Latest error details button, and original command diagnostics in History.
Local processing failures keep healthy sessions; optional WMI namespace failures
no longer close the primary connection. The previous authoring package remains
`dist/remoteops-linux-wmi-authoring-preview.tar.gz`.
The previous service/events fix package remains `dist/remoteops-linux-wmi-preview.tar.gz`.
See `packaging/linux/TESTING-WMI.md` for installation/manual checks and
[Adding WMI Commands](docs/ADDING_WMI_COMMANDS.md) for extension instructions.
Linux Connect/sysinfo have been confirmed by the user; new commands, namespaces,
and methods are fixture-tested but not live-verified. `restart-service` changes the
target; other commands collect data. Alias coverage does not mean identical output.

### Version 0.2.0

- WinRM uses a retained single-runspace pool. Idle health checks default to 60 seconds
  and can be configured per host. A lost session requires an explicit reconnect.
- Terminal input accepts saved command-library aliases only. Unknown commands never
  reach a host. Run `events -h` for help, or `events --log Application --count 25`.
- Each host has Terminal, Table, and History views with its command input always
  available. History searches commands and output and includes date/outcome filters.
- Structured PSRP objects and native WMI records populate sortable, filterable tables
  with cell copying and CSV export. Output includes find, highlights, and next/previous.
- The toolbar theme toggle persists light/dark mode between launches.
- Connected WinRM hosts support upload/download with progress, cancellation, overwrite
  control, temporary files, and SHA-256 verification. Use a drive path on the target;
  network-share paths are not used. Files are verified before publication. A connection
  loss during final publication is reported as an uncertain outcome rather than success.
- WMI/DCOM uses a shared builtin catalog through native Windows COM or the
  **experimental Linux WMI backend**, with
  retained authenticated associations, namespace references, and DCOM lifetime pings.
  Additional namespaces open on demand; methods are narrowly limited to service
  start/stop and read-only registry enumeration. Further live validation is deferred
  to user testing in the preview archive.
  WMI hosts never use SMB or silently switch to WinRM. WMI-only transfers are unavailable.

Existing host/history databases migrate automatically. Existing `commands.json` entries
are retained; valid version-1 command libraries are backed up to `commands.json.v1.bak`
before an atomic version-2 upgrade. User-added saved command definitions are trusted library entries and may
execute; arbitrary PowerShell typed directly into the terminal is rejected.

Command parameter metadata accepts `_schema` definitions with `type` (`str`, `int`,
`bool`, `float`), `default`, `required`, `description`, `choices`, `min`, and `max`. `_positional`
lists positional parameter names; `_examples` supplies help examples. Scripts use
fully quoted placeholders such as `'$service_name'`; values are inserted as literal
single-quoted strings. Both `--service-name VALUE` and `service_name=VALUE` work.

For WMI limitations and remaining work, see [WMI_PLAN.md](WMI_PLAN.md).

The application includes:

- Persistent session abstractions.
- A session manager that reuses live sessions per host/auth profile.
- SQLite-backed host, credential profile, and command history storage.
- Built-in command templates for common administration tasks.
- A PySide6 desktop shell.
- A fake session backend for local development and tests.
- A persistent PSRP backend for WinRM sessions.
- A compact host list with one terminal-style workspace tab per host.
- Terminal-style command aliases, such as `sysinfo`, that resolve to full PowerShell.
- A runtime-editable command library stored in `commands.json`, so aliases can be changed
  without rebuilding or reinstalling the package.
- A Modern Ops Console interface with searchable hosts, movable host tabs, inline credential
  selection, connection status text, and progress feedback.
- A monospace terminal with restrained output colors, per-host Up/Down command history,
  wrapping, and copy/save/clear tools. Inventory refreshes preserve open terminals.

## Install for development

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

On Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

## Run

```bash
remoteops
```

or:

```bash
python -m remoteops
```

## Architecture

```text
src/remoteops/
  core/        Dataclasses, SQLite repository, credential helpers
  remoting/    Persistent sessions, PSRP backend, fake backend, session manager
  commands/    Terminal aliases and built-in command templates
  ui/          PySide6 desktop interface
```

## UI workflow

- Add auth profiles from the toolbar, then assign them while adding or editing a host.
- Pick a host from the compact rail on the left.
- Work inside that host's tab: connect, disconnect, issue commands, and read full output/errors.
- Use the colored host indicator to see disconnected, connected, running, and error state.
- Open Auth Profiles or Commands from the toolbar when you need reference/configuration.
- Run saved command aliases from the host tab's command line; use `-h` for local help.
- Edit commands from the Commands toolbar button, or edit `commands.json` directly and use
  Reload File in the Commands dialog.

Built-in aliases include:

```text
sysinfo
disk
services
restart-service Spooler
admins
pending-reboot
topcpu 20
events Application 25
events log=Security count=10
hotfixes
net
ports
shares
users
groups
firewall
tasks
startup
sessions
svc Spooler
uptime
```

## Runtime commands

On first launch, RemoteOps writes the default command aliases to:

```text
<RemoteOps app data>/commands.json
```

On Linux this is usually:

```text
~/.local/share/RemoteOps/commands.json
```

On Windows this is usually:

```text
%LOCALAPPDATA%\RemoteOps\commands.json
```

You can update commands without rebuilding the package:

- Use the Commands toolbar button to add, edit, delete, reload, or reset commands.
- Or edit `commands.json` directly and click Reload File in the Commands dialog.
- Command definitions can include `parameters`; aliases support positional options through
  `_positional` and `key=value` overrides.

## Experimental RPC probe (custom Linux WMI, part 1)

The source tree includes a standard-library-only diagnostic for the first layer of
our own RPC/DCOM/WMI client. It sends one unauthenticated bind to the endpoint
mapper (v3.0, NDR v2.0), validates the response, and closes the connection. It does
not query endpoints, activate DCOM objects, authenticate, or execute WMI commands.
It does not use Impacket, SMB, WinRM, or PowerShell.

From the repository root on Linux, replace the example IP with an approved target:

```bash
PYTHONPATH=src python3 -m remoteops.remoting.rpc_probe 192.0.2.10 --timeout 5
```

For an installation containing this updated source, omit `PYTHONPATH=src` and use
that installation's Python interpreter. The existing tarball has not been rebuilt
for this diagnostic-only milestone.

- `-h` lists options; `--port` defaults to 135.
- Only numeric IPv4/IPv6 addresses are accepted in this initial version; DNS and
  scoped IPv6 addresses are not supported.
- One total timeout covers connect, send, and receive, including partial reads.
- Responses are bounded to 4096 bytes. Fragmented bind responses and authentication
  trailers are explicitly unsupported at this stage.
- Exit codes: 0 for accepted bind, 1 for network/protocol/rejection failure,
  2 for invalid arguments, and 130 for keyboard cancellation.
- Success means **RPC handshake verified**, not WMI access or valid credentials.
  Even success on port 135 does not prove the later DCOM endpoint is reachable.
- Failure of this unauthenticated probe does not by itself establish whether an
  authenticated WMI connection would succeed.

Part 1 validation: 62 tests passed on Windows, including 19 probe tests and a real loopback
fixture that verifies no RPC request follows the bind. A real bind to the local
Windows endpoint mapper also passed. The 19 probe tests additionally passed on
Ubuntu 24.04.4 / Python 3.12.3, followed by a successful RPC bind from Ubuntu to an
approved remote Windows host on TCP 135. Part 1 is verified on both platforms;
authentication and WMI queries remain outside this milestone. See `WMI_PLAN.md`
for the next milestones.

## Experimental authenticated RPC probe (part 2a)

The separate `remoteops.remoting.rpc_auth_probe` diagnostic adds NTLMv2 packet
privacy and header signing. It uses our own RPC code with the existing pyspnego
dependency for authentication and cryptography (now requiring version 0.12.2 or
later). It makes exactly one read-only `IObjectExporter.ServerAlive` call and
closes the connection. No WMI objects are activated and no WMI commands are run.

Run with the project's virtual-environment Python, from the repository root:

```bash
PYTHONPATH=src .venv/bin/python -m remoteops.remoting.rpc_auth_probe 192.0.2.10 --username 'COMPUTER\user' --timeout 10
```

Replace the IP and account with an approved target. Password entry is hidden and
interactive; there is no password argument, password file, or plaintext fallback.
An explicit bare username is accepted for a local account, but qualified account
names avoid ambiguity. The diagnostic does not load or change saved GUI credentials.

- Success requires verifying the protected server response, not merely generating
  an NTLM authenticate token. An access-denied fault is a failure, never connected.
- NTLMv1/LM, missing privacy/header signing, invalid signatures, and changed security
  contexts are refused. `LM_COMPAT_LEVEL`, if set, must be in 3..5.
- One total deadline, 4096-byte packets, numeric unscoped IPs, and little-endian
  responses. Part 4b adds protected response assembly (1 MiB / 512 fragments maximum).
  No retries, automatic protocol fallback, or general
  RPC method/command selector. Exit codes match the first diagnostic.
- Use only approved, trusted targets. NTLM is not Kerberos-style mutual server
  authentication. Do not enable authentication-provider debug/token logging.
- This does not establish WMI permissions, a reusable WMI session, or GUI readiness.
  Kerberos is deferred. The installer has not been rebuilt for these diagnostics.

Validation: all 80 local tests pass, including 18 authenticated-probe tests using
synthetic NTLM peers for correct/incorrect passwords, tampering, replay, downgrade
rejection, deadlines, and cleanup. A live Ubuntu-to-Windows NTLMv2/privacy/header-signing
test returned ServerAlive status 0. One nonexistent-account attempt was rejected
with RPC fault 0x00000005; no bad-password attempts were made against the real account.
All 37 RPC-focused tests also pass on Ubuntu with Python 3.12.3 and pyspnego 0.12.2.

## Experimental DCOM activation probe (part 3)

The separate `remoteops.remoting.dcom_probe` diagnostic implements the next small
step: discover the DCOM version, activate only the WMI login interface, connect to
its returned RPC exporter endpoint, and release the acquired reference. Discovery
uses unauthenticated ServerAlive2 as required by DCOM; activation and release require
NTLMv2 packet privacy and header signing. It uses our own RPC/NDR32 code, not Impacket.

```bash
PYTHONPATH=src .venv/bin/python -m remoteops.remoting.dcom_probe 192.0.2.10 --username 'COMPUTER\user' --timeout 30
```

Replace the example IP/account with an approved Windows target. Password entry is
hidden; no password argument is accepted. Use `-h` for options.
Stage and outcome messages include UTC timestamps (`+00:00`) for matching server events.

For a standalone Linux diagnostic bundle, put the five modules `rpc_probe.py`,
`rpc_auth_probe.py`, `dcom_wire.py`, `wmi_query.py`, and `dcom_probe.py` inside
`remoteops/remoting/` under the bundle directory. Place
`packaging/diagnostics/remoteops-dcom` at the bundle root, make it executable, and
create a `.venv` there with `pyspnego==0.12.2` installed. This is separate from the
full desktop application and needs neither Qt nor PowerShell. Run it from any directory:

```bash
~/remoteops-diagnostic/remoteops-dcom 192.0.2.10 --username 'COMPUTER\user' --timeout 20
```

It remains installed until its bundle directory is removed. No password is stored.

- Without `--namespace` or `--query-os`, no namespace login is attempted.
  `--query-os` adds the fixed read-only query described below. No mode creates
  processes, runs shell commands, or integrates with the GUI.
- Uses only TCP RPC, never SMB, WinRM, or a Windows relay. A returned binding cannot
  redirect the connection to another host: only its TCP port is used on the supplied IP.
- One discovery/activation/namespace/query deadline; release gets up to five extra seconds.
  No automatic retries. A failed/lost release response is not reported as success.
  If a malformed/lost activation response prevents identifying the reference, cleanup
  is explicitly unconfirmed. Temporary remote objects may require server rundown.
- Supports NDR32, little-endian packets up to 4096 bytes, and null ORPC extensions.
  Protected responses can span up to 512 fragments / 1 MiB. Interface references
  require standard OBJREFs; OS values use only the WMI class-object custom marshaler.
  Other forms fail clearly. Request fragmentation is not implemented.
- Exit codes: 0 only after activation and release are verified, 1 on failure,
  2 for invalid input, 130 on keyboard cancellation with cleanup outcome reported.
- Activation-only success does not establish namespace permissions. Namespace mode
  verifies access but closes its handles; neither mode provides a persistent session.
  These diagnostic modes are separate from the experimental GUI session backend below.
  The existing Linux tarball remains unchanged.

Prior part 4a validation: **128 tests passed on Windows; 85 protocol-focused tests
passed on Ubuntu**. Tests include NDR bounds/truncation, exact request layouts, protected
object headers, tampering, reference release, cancellation, and no-fallback behavior.
The decoder ignores arbitrary ORPCTHAT flags as required by
[MS-DCOM 2.2.13.4](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/10ffacb5-c8b2-4b6a-bccc-9e515818661f),
while still checking the extension pointer and all following fields.

**Live activation and release now pass.** At 2026-09-22 00:34:58 UTC, Ubuntu obtained
IWbemLevel1Login from the approved Windows host, reached its returned TCP exporter,
and verified protected reference release with DCOM 5.7. This completes the part 3
lab gate, not namespace/query or GUI readiness. Earlier access-denied faults were
not explained by this parser fix; no target settings were changed by the agent.
For access failures on other hosts, inspect DistributedCOM events and the account's
remote DCOM rights. See Microsoft's [remote WMI prerequisites](https://learn.microsoft.com/en-us/windows/win32/wmisdk/connecting-to-wmi-remotely-starting-with-vista).
Do not disable DCOM hardening or UAC as a diagnostic workaround.

### Namespace access (part 4a)

Add `--namespace` to obtain an IWbemServices interface through the protected
IWbemLevel1Login.NTLMLogin call. The existing RPC authentication supplies identity;
the WMI method does not send a second password. The default locale and null context
are used. Both the login and namespace reference counts are released before success
is reported. Reference release is verified, not merely dispatched.

```bash
~/remoteops-diagnostic/remoteops-dcom 192.0.2.10 --username 'COMPUTER\user' --namespace root/cimv2 --timeout 20
```

Namespace paths must start with `root`, use `/` or `\` separators, and contain
ASCII identifier segments (letters/underscore followed by letters/digits/underscore),
up to 256 characters total. Server-qualified paths and host redirection are rejected.
No namespace is created or modified. No query is run, and no connection is retained
after this diagnostic completes. No server security policy is changed.

Live Ubuntu-to-Windows `root/cimv2` login and release of both references passed at
2026-09-22 00:43:25 UTC. A nonexistent namespace returned `0x8004100e` and exit code 1
at 00:43:52 UTC, with release of the login reference confirmed. Namespace access
denial (`0x80041003`) is fixture-tested, not live-verified with a denied account.
The 17 new tests also cover request layout, interface identity, malformed replies,
both reference counts, cancellation, CLI validation, and no query dispatch.

This subpart supports namespace interfaces on the activated exporter only. A different
OXID fails explicitly; cross-exporter resolution is not yet implemented. A lost,
malformed, or unsupported namespace response may leave the new reference's cleanup
unconfirmed, while the known login reference is still released when possible.
There are no automatic retries or transport fallbacks.

Protocol reference: [MS-WMI NTLMLogin](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/40d194a2-c28a-485b-97f6-11a7c08f147e).

### Read-only OS query (part 4b)

`--query-os` logs into `root/cimv2`, executes one fixed SELECT against
`Win32_OperatingSystem`, and enumerates exactly one instance plus end-of-results.
It prints a JSON record only after the login, services, and enumerator references
have all been released successfully. Other namespaces are rejected for this mode.

```bash
PYTHONPATH=src .venv/bin/python -m remoteops.remoting.dcom_probe 192.0.2.10 --username 'COMPUTER\user' --query-os --timeout 30
```

After updating all five modules in the standalone bundle, its launcher also accepts
`--query-os`. The existing Ubuntu installation has **not** been updated for part 4b.

Returned fields are `CSName`, `Caption`, `Version`, `BuildNumber`, `OSArchitecture`,
`LastBootUpTime`, and `TotalVisibleMemorySize` (an integer in KiB). Null values remain
JSON null; CIM datetime text retains its UTC offset and any wildcard fields. The
decoder handles compressed Unicode, UTF-16, class defaults, and property declaration
order. It reads only the projected string, datetime, and uint64 values; this is not
a general-purpose CIM object decoder or arbitrary-query entry point.

Each response fragment must pass NTLM privacy/signature verification before assembly.
Enumeration timeouts, empty results, multiple OS instances, malformed data, and
unconfirmed release fail explicitly. Cross-exporter references remain unsupported;
lost or malformed replies report uncertain cleanup while known references are released.

Validation: **151 local tests pass**, including 23 new query/fragment tests. The
read-only `tools/smoke_wmi_codec.py` check also passed against Windows' own native
custom marshaler: a 1356-byte object decoded to seven properties exactly matching
native WMI values. That checks the object decoder, not remote NDR/RPC interoperability.
SSH, Ubuntu deployment, and the live Ubuntu-to-Windows query gate are deferred at
the user's request. Session reuse and initial GUI integration are now implemented
in source as described below; their live validation is also deferred.

Protocol references:
[ExecQuery](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/49ee7019-aa2d-49ef-948a-05c468e37d31),
[Next](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/141623d4-a170-4b09-8fea-3cb8f78fbbfe),
[WMI custom marshaling](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/46710c5c-d7ab-4e4c-b4a5-ebff311fdcd1),
[WMIO encoding](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/0ac1987a-b6e9-4288-bc4a-0a8ca8555fc6),
[BSTR representation](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-oaut/f547135a-d76a-42c3-916f-30b4e46d79bc).

### Experimental Linux sessions (parts 5 and 6)

The current source selects the application-owned `DcomWmiSession` on Linux and
retains the native Windows COM backend on Windows. In Add Host, select WMI/DCOM,
use the Windows target's numeric IP, namespace `root/cimv2`, and an NTLM profile
with an explicit username/password. The resolver uses TCP 135 and the exporter
port returned by activation. Hostname resolution, Kerberos, and cross-exporter or
non-default resolver endpoints are not implemented in this backend.

Connect verifies namespace access and a complete OS query before showing Connected.
Subsequent `sysinfo` runs reuse the authenticated services, enumerator, reference
release, and resolver associations. Each query acquires and releases its own
enumerator. The namespace reference remains until disconnect. Structured results
flow through the existing Terminal, Table, and History views. The catalog now adds
the other builtin aliases, bounded multi-row queries, scalar/array decoding, and
narrowly scoped method workflows. Extra namespace clients are retained until
disconnect. Sysinfo keeps the seven part 4b OS fields, not the WinRM field set.
See `ALIAS -h` for backend differences and query limits.

DCOM ComplexPing maintains the live OID set; SimplePing refreshes it every 60 seconds
while idle. Both use NTLM packet privacy and run on the same per-host worker as
commands. Protocol maintenance remains enabled when optional idle health checks
are disabled. Maintenance does not postpone a longer configured health interval.
Pings also run around queries; query budgets are capped at 60 seconds, connection
setup uses 30 seconds, idle pings use 15 seconds, and cleanup gets five extra seconds.
The application's session scheduler must keep running for idle lifetime maintenance.

Provider/query errors, row/size limits, completed enumeration timeouts, and decoding
errors within recognized by-value WMI objects release only the query enumerator;
the connection stays usable. Class validation accepts encoded subclasses of the
requested class, including service subclasses. Events uses bounded record-number
probes and ranges to return the requested count without downloading the whole log.
Broken transport/authentication, unknown result references, unconfirmed cleanup,
or failed lifetime maintenance invalidate the affected connection. A failed auxiliary
namespace is discarded while the primary session stays connected; the next explicit
command needing that namespace can open a fresh connection. A broken primary
connection still requires explicit reconnect. Local formatting errors do not close
healthy sessions. WinRM timeouts retain the pool when stopping and cleanup succeed.
A recognized by-value method result that cannot be decoded leaves WMI connected,
but its action outcome is unconfirmed: inspect the remote state before retrying.
No operation is automatically retried. Disconnect
attempts exact public-reference release and ping-set removal, always closes local
sockets, and reports uncertain cleanup when a reply is lost. Shutdown continues
closing other hosts even if one host's cleanup fails.

Local verification covers retained associations, ping sets, OID sharing, no-ping
references, sequence handling, timeouts, error cleanup, worker ownership, explicit
reconnect, and Qt table/history/disconnect integration using synthetic peers.
**The user has confirmed Connect/sysinfo; the expanded catalog remains experimental.**
New queries, namespaces, arrays, and methods require user lab verification.
The preview includes the extension cookbook. No SSH, Ubuntu deployment, or live
remote operations were performed for this expansion.

Lifetime protocol references:
[ComplexPing](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/629629ba-71df-4110-bb6b-d6c5dca5f993),
[SimplePing](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/9de39908-e519-4393-8b3e-97b8038cc6a7),
[client ping lifecycle](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-dcom/8abedee5-e226-43e7-bb7c-34d0ac6a3c38).

## Security notes

- Passwords should not be stored in SQLite.
- Credential secrets are intended to live in the OS keychain via `keyring`.
- HTTPS WinRM should be preferred.
- HTTP, Basic auth, and certificate validation bypasses should be treated as explicit admin choices.

## Linux distributable

Create a Linux installer tarball from this repository on Windows:

```powershell
.\packaging\linux\build_tarball.ps1
```

The output is:

```text
dist/remoteops-linux.tar.gz
```

On Linux:

```bash
tar -xzf remoteops-linux.tar.gz
cd remoteops-linux
bash ./packaging/linux/install.sh
```
