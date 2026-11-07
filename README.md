# WMIBench

A desktop console for Windows administration over WMI/DCOM and WinRM.
Save hosts and credentials, connect once, and run commands through the same session.

## Run

The Windows standalone build includes Python and its dependencies. Launch
`WMIBench.exe`; no pip installation is needed. Portable bundles also include
their command configuration and plugins beside the executable.

The portable Windows bundle is staged under
`dist/WMIBench-windows-x86_64/`. Linux bundles are built on Linux because
PyInstaller does not cross-compile; use `python packaging/standalone/build.py --bundle`
on the Linux target. See [Standalone deployment](docs/STANDALONE.md).

For development, create a virtual environment, activate it, then install and launch:

```text
python -m venv .venv
python -m pip install -e .
python -m wmibench
```

Platform-specific setup:
[Windows](docs/WINDOWS_SETUP.md), [Linux](packaging/linux/TESTING-WMI.md).

## Workspace

The host tree supports named groups, drag-and-drop ordering, and right-click
connect/disconnect, move, rename, and delete actions. Deleting a group moves its
hosts to Ungrouped. Deleting a host disconnects it and keeps its history and credentials.

- **Terminal:** command aliases, argument completion, command history, and full error output.
- **History:** previous runs, structured results, search, and CSV export.
- **Services / Events:** dedicated views with filters and record details.
- **Tools:** command runner, file browser, processes, and users/sessions.
- **Explorer:** WMI namespaces, classes, property types, method parameters, instance queries,
  and Python examples for plugins.
- **Multi-host:** bounded read-only batches with per-host results and combined exports.

Examples:

```text
sysinfo
services
events --log Application --count 25
svc Spooler
events --help
```

Completion follows the selected connection type and each command's parameter schema.
Tab or Enter accepts a suggestion; accepting it does not run the command.
Up/Down recalls earlier commands when the suggestion list is closed.

Explorer class inspection works with native Windows WMI and with WinRM from either
client OS. The portable DCOM transport (Linux WMI, and Windows WMI with keys/hashes
or an explicit DC) currently supports namespace browsing only. The explorer does
not invoke methods; copied method examples can be used in a plugin.
See [Explorer](docs/EXPLORER.md).

## Plugins

Each plugin is a folder containing `command.json` and `implementation.py`.
Add it through **Commands > Python commands**, or put it in the data folder's
`plugins` directory. Valid plugins load automatically; saved edits apply on the next run.

- Windows: `%LOCALAPPDATA%\RemoteOps\plugins`
- Linux: `~/.local/share/RemoteOps/plugins`
- Portable bundle: the `plugins` folder beside `WMIBench.exe` or `wmibench`

The `RemoteOps` directory name and Python imports are retained for compatibility.
Plugins run locally under your account. Their `ctx` calls use the selected remote
connection; plugin code also has access to local files and application data.

- [Write a command](docs/PYTHON_COMMANDS.md)
- [Plugin API and parameter schema](docs/PLUGIN_API.md)
- [Scheduled-task and registry plugins](docs/ADMIN_PLUGINS.md)
- [Built-in WMI command development](docs/ADDING_WMI_COMMANDS.md)

## Connections And Limits

Recoverable command errors leave healthy sessions connected. Broken transports
require reconnecting. Failed operations are not automatically replayed.

WMI process launches return a process ID, not console output. File transfers and
terminal-session actions require WinRM. No WMI operation silently switches transports.
The Linux DCOM backend is experimental; Connect/sysinfo have been user-verified,
but expanded metadata and method support need further target testing.

Credentials support Kerberos with a password, RC4 key, or AES key, and NTLM with a
password or NT hash. Kerberos profiles take a domain and DC IP; DNS queries use the
DC or an optional DNS server without changing system settings. See [Authentication](docs/AUTHENTICATION.md)
for connection examples and the live-testing limits of this preview.

Saved passwords and keys are encrypted in SQLite. The encryption key is protected by DPAPI
on Windows or file permissions on Linux. Back up both the database and key;
copying the database alone does not transfer saved credentials to another account.
See [Credentials](docs/CREDENTIALS.md).

## Development

```text
python -m unittest discover -s tests -v
python packaging/standalone/build.py --variant management
```

Tests use fake transports and local fixtures. They do not run commands on remote hosts.
Protocol notes and remaining backend work are in [WMI plan](WMI_PLAN.md).
