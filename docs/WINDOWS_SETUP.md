# Windows Setup

For the standalone build, run **WMIBench.exe**. No Python, pip or installer is
required. Close the old app before replacing its executable. Saved data and plugins
remain in `%USERPROFILE%\AppData\Local\WMIBench`. See [Standalone Deployment](STANDALONE.md).

The alternative source-install instructions below require Python 3.12 or newer.
`wmibench-preview.zip` is a source bundle,
not a standalone Windows installer. Extract it and open PowerShell in the
`wmibench-preview` folder containing `pyproject.toml`.

## First Installation

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\python.exe -m wmibench
```

Dependencies, including native Windows COM support through pywin32, install automatically.
The application is installed in this folder's `.venv`, not Program Files.

## Update An Existing Installation

Close WMIBench first. Open PowerShell in your existing installation folder,
which contains the `.venv` you normally use. Install from the newly extracted
source folder (replace the example path with its actual location):

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade "C:\path\to\wmibench-preview"
.\.venv\Scripts\python.exe -m wmibench
```

This replaces application code in that environment. It does not delete saved hosts,
credentials, history or plugins in `%USERPROFILE%\AppData\Local\WMIBench`.
Use the same Windows account as before to retain access to saved credentials.

## Invocation Fix

The current update also supplies API v3 for the [task/registry plugins](ADMIN_PLUGINS.md).
Install this application update once before using the v3 plugins.

The native Windows adapter now calls `SWbemServices.ExecMethod` rather than
`ExecMethod_` (which belongs to SWbemObject). Local attribute/type errors while
preparing the call report that no action was sent and preserve the connection.
An uncertain failure after dispatch still requires inspecting the remote state;
the application never automatically retries an action. Actual transport failures
can still require reconnecting explicitly.

This fix is covered by offline regression tests, not a live remote invocation test.
For a previous uncertain result, inspect remote state before trying the action again.

Plugins using `ctx.require_success(result)` must include `"ReturnValue": "uint32"`
when supplying an explicit `outputs` dictionary. For process creation, use
`outputs={"ReturnValue": "uint32", "ProcessId": "uint32"}`. Remote WMI creation
does not open an interactive window on the remote user's desktop.

See [Plugin API](PLUGIN_API.md) for the full contract. Plugins live in
`%USERPROFILE%\AppData\Local\WMIBench\plugins`; added folders and saved edits
are discovered automatically on the next run.
