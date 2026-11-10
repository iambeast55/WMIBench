# WMIBench

A desktop console for Windows administration over WMI/DCOM and WinRM.
Save hosts and credentials, connect once, and run commands through the same session.

## Install and run

Use the scripts from the repository root. They create `.venv`, install the source
in editable mode, and keep plugins and application data in this folder.

Linux:

```bash
./install.sh
./run.sh
# After install, wmibench is also available from PATH.
```

Windows PowerShell:

```powershell
.\install.ps1
.\run.ps1
```

Windows Command Prompt:

```text
install.cmd
run.cmd
```

See [Windows setup](docs/WINDOWS_SETUP.md) and [Linux WMI testing](packaging/linux/TESTING-WMI.md).
For Linux proxychains use, see [Proxychains](docs/PROXYCHAINS.md).

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
client OS. The DCOM transport (Linux WMI, and Windows WMI with keys/hashes
or an explicit DC) currently supports namespace browsing only. The explorer does
not invoke methods; copied method examples can be used in a plugin.
See [Explorer](docs/EXPLORER.md).

## Plugins

Each plugin is a folder containing `command.json` and `implementation.py`.
Add it through **Commands > Python commands**, or put it in the repository's
`plugins` directory. Valid plugins load automatically; saved edits apply on the next run.

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
