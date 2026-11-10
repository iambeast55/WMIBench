# Windows Setup

WMIBench runs from the repository folder. Python 3.12 or newer is required.

## Install

Open PowerShell in the folder containing `pyproject.toml`:

```powershell
.\install.ps1
.\run.ps1
```

The editable install creates `.venv` in the repository. Plugins go in
`.\plugins`; the database, credential key, history, and settings remain beside it.

If script execution is restricted, use:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\install.ps1
powershell.exe -ExecutionPolicy Bypass -File .\run.ps1
```

Command Prompt wrappers are also provided:

```text
install.cmd
run.cmd
```

## Update

Close WMIBench, pull or copy the updated source, then run `install.ps1` again.
The editable install updates application code without moving repository-local data.

## Plugin API

See [Plugin API](PLUGIN_API.md) for the command contract. Added plugin folders and
saved edits are discovered automatically on the next command dispatch.
