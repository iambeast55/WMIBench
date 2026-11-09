# WMIBench Linux Distribution

This package installs WMIBench into a per-user Linux environment.

## Install

```bash
tar -xzf wmibench-preview.tar.gz
cd wmibench-preview
bash ./packaging/linux/install.sh
```

The installer:

- Copies the app into `~/.local/share/remoteops`.
- Creates a Python virtual environment.
- Installs WMIBench and its Python dependencies.
- Adds `wmibench` and compatibility `remoteops` launchers to `~/.local/bin`.
- Adds a desktop entry to `~/.local/share/applications`.

## Run

After this one-time update, use Command Library's **Python commands** action to
create or install command folders. Edit, run offline tests, approve, and reload
without rebuilding or restarting. See `docs/PYTHON_COMMANDS.md`; seven complete
examples are in `examples/plugins/`. Python extensions are trusted local code,
not sandboxed queries. They are disabled until explicitly approved.

`docs/PLUGIN_API.md` documents the v3 invocation/deletion API and all parameter schema
fields. The new generic Linux method codec has not been live-verified. Invocation
requires the remote-change flag and confirmation; never automatically replay a
method after an unconfirmed result. Existing v1/v2 commands remain compatible.
See `docs/ADMIN_PLUGINS.md` for the task/registry plugins and Linux request-size limits.

```bash
wmibench
```

Run through proxychains-ng when required:

```bash
proxychains4 wmibench
```

Enable `proxy_dns` for hostname targets. See `docs/PROXYCHAINS.md` for WMI,
Kerberos, and WinRM port requirements.

If `~/.local/bin` is not on your `PATH`, add this to your shell profile:

```bash
export PATH="$HOME/.local/bin:$PATH"
```

## Uninstall

```bash
~/.local/share/remoteops/packaging/linux/uninstall.sh
```

## Notes

Linux clients do not need local PowerShell. WMIBench talks to Windows hosts over
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

The latest `remoteops-linux-console-fixes-preview.tar.gz` includes this backend for testing.
Extract it, enter `remoteops-linux-console-fixes-preview`, then run the installer above.
Live Linux Connect/sysinfo have been user-confirmed. Reported service/event failures
are addressed by subclass decoding, bounded event retrieval, and command/session
error separation; these fixes still need user lab verification. The archive includes `ADDING_WMI_COMMANDS.md`
with source extension instructions and examples.
This includes custom read-only WMI query definitions, local editor validation/preview,
and import/export examples under `examples/wmi`, plus command categories, search,
favorites, backend filters, and an insert-only picker. Errors use short explanations
with original diagnostics in the console and command History. Local errors
and auxiliary namespace failures retain the primary session. A broken primary
connection still requires manual reconnect; failed actions are never replayed.
The Multi-host toolbar action runs approved read-only commands across selected
hosts with bounded concurrency, explicit approval of new connections, queued
cancellation, independent results, and CSV export. Healthy sessions are retained.
See `docs/MULTI_HOST.md` in the extracted package. The previous multi-host preview
is retained separately.
Type arguments in the host or batch command input; use `ALIAS -h` for options.
Services provides explicit refresh, selected-service inspection, and confirmed restart.
Events fetches a bounded recent set and filters those loaded records locally.
Opening these views does not connect or query. See `docs/COMMAND_WORKSPACES.md`
for the workflow and lab checklist; no live verification was performed for this build.
Credentials now supports editing and deleting profiles, saving encrypted passwords
in SQLite, and reusing them after restart without typing them again. Existing
keychain passwords migrate on successful lookup. Read `docs/CREDENTIALS.md` before
backing up or moving saved credentials: keep the database and local key together.
The host rail replaces horizontal target tabs; right-click a host to connect or
disconnect. Larger text, contrasting icons, wider tables, and separate service/event
detail windows make more of the workspace available for results.
First load backs up valid version-1 command libraries before migrating to version 2.
See [the WMI test guide](TESTING-WMI.md) in this directory for setup and checks.
