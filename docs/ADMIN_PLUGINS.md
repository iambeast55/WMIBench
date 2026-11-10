# Task And Registry Plugins

Two self-contained, reloadable Python commands: `scheduled-task` and `registry`.
They use WMI on the selected host and its connection credentials, not local registry
APIs, local task commands, SMB or a silent WinRM fallback. Code and offline tests
are in `examples/plugins/<alias>/`.

## Install

### Start/Stop Response Fix

Use the `task-run-fix` application build and updated scheduled-task plugin together.
Some successful `StartByPath`/`StopByPath` calls return no output object. These now
report **Request completed / None returned / TaskCompletion: Not checked**, not an
AttributeError or an invented return code. If an output object is returned, its
ReturnValue is still checked. Creation/export still require their declared output.
Nothing is automatically retried. Provider failures remain failures; names and paths
must match the task exactly (`Cals` and `Calcs` are different names).

Keep personal plugin edits backed up before replacing `implementation.py` and
`test_implementation.py`. Saved plugin edits load automatically; replacing the app
executable requires closing and reopening it. Older app builds reject the updated
run/stop action before dispatch. SYSTEM tasks do not open a window on the signed-in
user's desktop; request completion does not prove the program finished successfully.

### Scheduled-Task Creation Hotfix

`wmibench-task-registry-plugins-taskxml-fix.zip` corrects a creation bug in the
original scheduled-task example. The old plugin wrote `ServiceAccount` into the XML
LogonType element. That value belongs to the API enum, not the
[XML schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema).
Built-in service-account SIDs now identify the principal without that element.
Windows' in-memory task definition parser accepts all three supported accounts and
all three schedules and resolves their logon type to TASK_LOGON_SERVICE_ACCOUNT (5).
Remote registration still needs target-side verification; no task is automatically retried.

For an already installed plugin, open its folder from **Commands > Python commands**,
replace `implementation.py` and `test_implementation.py` with the scheduled-task files
from this hotfix. Keep any personal modifications backed up outside the plugin root.
With the current app, saved changes take effect on the next run automatically.
No application reinstall is required. New installations can use the hotfix's
scheduled-task folder with **Install folder**.

`--working-directory` sets the launched program's current directory, not the scheduler
folder. `--path` selects a scheduler folder. SYSTEM tasks do not open GUI windows in
the signed-in user's desktop, and create does not automatically run a task.

### Initial Installation

1. Close the old app and update the repository checkout. The current source includes API v3.
2. Extract `wmibench-task-registry-plugins.zip`. In **Command Library > Python commands**,
   use **Install folder** for each extracted plugin folder, not the pack's parent.
   Alternatively copy the two folders into the application's plugins directory.
3. The plugins are immediately available. Offline tests are optional. Existing folders
   are not overwritten by Install folder.
4. Use `scheduled-task -h` and `registry -h` for the schemas. After future plugin edits,
   save and run again; no further rebuild is needed for code using the same API.

Repository plugin location: `./plugins`. Existing saved data is retained.

Both manifests declare `modifies_remote: true`. The entire command is excluded from
read-only batches, even for list/get actions. Installed plugins run without activation
or a repeated mutation popup for every action.
Plugin Python runs locally; WMI requests run remotely. No command here retries
a mutation automatically. A confirmed run/start request does not mean the task's
program finished successfully.

## Scheduled Tasks

This is the modern provider, not Win32_ScheduledJob/AT. It queries
`root/Microsoft/Windows/TaskScheduler:MSFT_ScheduledTask`; create/start/stop/export
use the companion `PS_ScheduledTask` methods. Delete uses WMI DeleteInstance.
These contracts were checked against Windows' `SchedProv.mof` and the bundled
ScheduledTasks `.cdxml` definitions. No nonexistent MSFT_ScheduledTask.Run method
is used and the obsolete AT protocol is not enabled.

```text
scheduled-task list
scheduled-task list --path '\Maintenance\'
scheduled-task list --name WMIBenchLab
scheduled-task create WMIBenchLab --executable 'C:\Tools\diagnostic.exe' --arguments '--mode check' --account 'LOCAL SERVICE'
scheduled-task create WMIBenchDaily --executable 'C:\Tools\diagnostic.exe' --account 'LOCAL SERVICE' --schedule daily --start '2026-11-01T09:00:00-05:00'
scheduled-task run WMIBenchLab
scheduled-task stop WMIBenchLab
scheduled-task export WMIBenchLab
scheduled-task delete WMIBenchLab
```

- Mutating actions require an exact name. `--path` defaults to `\` for actions;
  listing defaults to all folders. Folder paths must start/end with `\`.
  Names and paths are not wildcard searches. The folder must already exist.
- Creation accepts an absolute remote executable path without surrounding quotes
  inside the argument value. The executable and argument string are separate XML
  fields; XML is escaped by ElementTree. No shell is added implicitly.
- Creation requires `--account SYSTEM`, `--account 'LOCAL SERVICE'` or
  `--account 'NETWORK SERVICE'`. SYSTEM is highly privileged; there is no implicit
  SYSTEM default. Domain/password-based principals are not implemented. No passwords
  are accepted on the command line or placed in task XML by this plugin.
- The task's runtime identity is the selected service account, not the connection
  identity. WMI credentials must have permission to register/manage the task.
- Default schedule is `demand`: create does not start it; use run explicitly.
  `once` and `daily` require `--start` with an explicit timezone offset. The offset
  is not inferred from the client or remote timezone. Check future/DST transitions
  in the remote scheduler. A time in the past is passed to the provider, not advanced.
- Existing tasks detected by preflight are refused and `Force=False` is sent.
  The preflight is not an atomic lock against another administrator creating the same
  task concurrently. Use unique names; there is no replace/edit action in this pack.
- `--working-directory` and `--description` are optional creation settings.
  The template sets LeastPrivilege and IgnoreNew multiple-instance behavior;
  remaining settings use scheduler defaults (including battery/runtime policies).
- Run starts under the registered principal and conditions. It does not guarantee
  an interactive desktop or return program output. Stop requests termination;
  delete unregisters a task, and is not a substitute for stopping a running task.
- `export` returns XML in `cmdletOutput`; protect exports as potentially sensitive.
  `--limit` is a list row limit, not an ordered or silently truncated prefix.

## Registry

All calls use `root/default:StdRegProv` and the declared hive. The commands below
use a disposable test key; do not test writes/deletions against production settings.

```text
registry keys SOFTWARE --hive HKLM
registry values 'SOFTWARE\WMIBenchLab'
registry create-key 'SOFTWARE\WMIBenchLab'
registry set 'SOFTWARE\WMIBenchLab' --name Label --type SZ --data 'Lab machine'
registry set 'SOFTWARE\WMIBenchLab' --name Enabled --type DWORD --data 1
registry set 'SOFTWARE\WMIBenchLab' --name LargeNumber --type QWORD --data 0xffffffffffffffff
registry set 'SOFTWARE\WMIBenchLab' --name SearchPaths --type MULTI_SZ --data '["C:\\Tools","D:\\Tools"]'
registry set 'SOFTWARE\WMIBenchLab' --name Bytes --type BINARY --data '00 ff 10'
registry get 'SOFTWARE\WMIBenchLab' --name Enabled --type DWORD
registry set 'SOFTWARE\WMIBenchLab' --default-value true --type SZ --data ''
registry delete-value 'SOFTWARE\WMIBenchLab' --name Enabled
registry delete-key 'SOFTWARE\WMIBenchLab'
```

- Hives: HKLM (default), HKU, HKCR, HKCC. Remote HKCU is deliberately not exposed;
  it is not a reliable way to access the interactive user's registry. For a loaded
  user hive use HKU and an explicit SID-relative path. There is no hive-loading action.
- Subkeys are hive-relative, using backslashes. Root enumeration is allowed;
  root-hive modifications are rejected. Root get is also intentionally disallowed.
- `keys` lists direct subkeys. `values` returns value names/types, not their data.
  Null/empty arrays produce no rows; unknown type codes are retained.
- `get` and `set` require an explicit type: SZ, EXPAND_SZ, MULTI_SZ, DWORD, QWORD,
  BINARY. Use `values` first when unsure. EXPAND_SZ reads use GetExpandedStringValue,
  so the returned text may have environment variables expanded by the provider.
- DWORD/QWORD accept unsigned decimal or `0x` hex in the 32/64-bit range.
  MULTI_SZ takes a JSON array of strings; BINARY takes hexadecimal byte pairs.
  Empty SZ/BINARY/MULTI_SZ data may be supplied explicitly. Provider rules still apply.
- A missing name is not silently the default value: use `--default-value true`.
- `set` creates or replaces the named value, including its type. It does not create
  the containing key. `create-key` may create intermediate keys and is idempotent.
- `delete-key` first checks for child keys and refuses them. It does not walk a tree.
  A leaf key's values are removed with the key. Back up data before deleting it.
- This API does not expose provider architecture context: operations use the
  provider-selected registry view, not a selectable 32/64-bit view. Test the view
  on your target; there is no hidden retry through a different view.
- Data and command arguments can appear in results/history. Do not pass secrets.

## Verification And Limits

These plugins have offline fake-context tests for provider names, parameter order,
CIM types, return codes, input rejection and no-retry behavior. The core tests cover
both adapters, native COM method selection, DeleteInstance wire layout, worker audit,
healthy-session retention, lost replies and optional argument forms.
No live remote task or registry changes were made during development.

Native Windows supports up to 65,536 UTF-16 units per method text input. Linux DCOM
still has the 3,968-byte single-request limit: XML plus method metadata must fit.
Large task registrations are rejected before sending, not fragmented. Generic Linux
invocation and deletion remain experimental and are not live-verified. Flat input
arrays are limited to 1,024 elements. Embedded-object inputs are not supported;
task creation uses XML and projects only the scalar ReturnValue output.

Run local tests from the application environment after reviewing the files:

```powershell
.\.venv\Scripts\python.exe -m remoteops.commands.plugin_cli test examples/plugins/scheduled-task --trust
.\.venv\Scripts\python.exe -m remoteops.commands.plugin_cli test examples/plugins/registry --trust
```

For live verification, first list tasks/keys, then use a uniquely named disposable
task and key. Check the chosen principal, trigger, stored registry type/data and
remote task state before exercising run/delete. If a reply is lost, inspect the
remote state before retrying; do not assume the write was rolled back.

## References

- [Task registration](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/register-scheduledtask)
- [Task starting](https://learn.microsoft.com/en-us/powershell/module/scheduledtasks/start-scheduledtask)
- [StdRegProv](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/regprov/stdregprov)
- [EnumValues and type codes](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/regprov/enumvalues-method-in-class-stdregprov)
- [WMI instance deletion](https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/dc4aee01-7b9f-4c1a-8671-658fa334b8a4)
