# RemoteOps Linux Distribution

This package installs RemoteOps into a per-user Linux environment.

## Install

```bash
tar -xzf remoteops-linux-multihost-preview.tar.gz
cd remoteops-linux-multihost-preview
bash ./packaging/linux/install.sh
```

The installer:

- Copies the app into `~/.local/share/remoteops`.
- Creates a Python virtual environment.
- Installs RemoteOps and its Python dependencies.
- Adds a `remoteops` launcher to `~/.local/bin`.
- Adds a desktop entry to `~/.local/share/applications`.

## Run

```bash
remoteops
```

If `~/.local/bin` is not on your `PATH`, add this to your shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Uninstall

```bash
~/.local/share/remoteops/packaging/linux/uninstall.sh
```

## Notes

Linux clients do not need local PowerShell. RemoteOps talks to Windows hosts over
WinRM/PSRP and runs PowerShell remotely on those hosts.

For Kerberos from Linux, the workstation still needs normal enterprise Kerberos
configuration, such as `/etc/krb5.conf` and a valid ticket from `kinit`.

Version 0.2.0 adds a persistent runspace, idle keepalive, history and output search,
table results, dark mode, command help, and verified WinRM file transfers. Run the
installer again to update; user data is stored separately in `~/.local/share/RemoteOps`.

Only saved command-library aliases can run in the terminal. Try `events -h`.

Current source includes an experimental Linux WMI/DCOM backend for all 21 builtin aliases:
numeric target IP, root/cimv2, and NTLM with explicit credentials. It retains RPC
associations and the namespace reference, maintains DCOM object lifetimes, and
keeps sessions after recoverable command errors. Broken transport or unconfirmed
reference cleanup still requires explicit reconnect. Results use the normal tables/history.
Run `ALIAS -h` for provider fields and limitations. `restart-service` changes the
target and asks for GUI confirmation; the other handlers collect information.
No Impacket, SMB, or WinRM fallback is
included, and transfers are unavailable for WMI-only hosts.

The latest `remoteops-linux-multihost-preview.tar.gz` includes this backend for testing.
Extract it, enter `remoteops-linux-multihost-preview`, then run the installer above.
Live Linux Connect/sysinfo have been user-confirmed. Reported service/event failures
are addressed by subclass decoding, bounded event retrieval, and command/session
error separation; these fixes still need user lab verification. The archive includes `ADDING_WMI_COMMANDS.md`
with source extension instructions and examples.
This includes custom read-only WMI query definitions, local editor validation/preview,
and import/export examples under `examples/wmi`, plus command categories, search,
favorites, backend filters, and an insert-only picker. Errors use short explanations
with original diagnostics in Latest error details and command History. Local errors
and auxiliary namespace failures retain the primary session. A broken primary
connection still requires manual reconnect; failed actions are never replayed.
Earlier service/events and authoring archives remain available separately.
The Multi-host toolbar action runs approved read-only commands across selected
hosts with bounded concurrency, explicit approval of new connections, queued
cancellation, independent results, and CSV export. Healthy sessions are retained.
See `docs/MULTI_HOST.md` in the extracted package. The earlier usability preview
also remains available separately.
First load backs up valid version-1 command libraries before migrating to version 2.
See [the WMI test guide](TESTING-WMI.md) in this directory for setup and checks.
