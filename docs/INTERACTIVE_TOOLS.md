# Interactive Host Tools

Install the `wmibench-preview` source bundle using the existing
[Windows setup](WINDOWS_SETUP.md) or Linux installer. Existing hosts, credentials,
commands and installed plugins are retained. Connect a host, select **Tools**, then
choose a tool from the selector. These are built-in views, not plugins to install.

| Tool | WinRM | WMI/DCOM |
| --- | --- | --- |
| Command runner | Remote PowerShell, output/error records and native exit code | Remote process creation, provider code and PID |
| Files | Folder browsing, rename/delete files, verified upload/download | Folder and file metadata, rename/delete files |
| Processes | Refresh/live inventory and end process | Refresh/live inventory and end process |
| Users / sessions | Local account inventory; terminal sessions with disconnect/sign out | Local account inventory and WMI logon inventory |

No tool silently opens another transport or connection. All operations run on the
selected host's existing serial worker. Normal validation, access-denied, provider
and script errors leave a healthy connection usable. A genuinely lost transport
still requires explicit reconnect. Actions are never automatically retried.

## Command Runner

WinRM runs a PowerShell script remotely. For example:

```powershell
Get-Date
Get-Process | Select-Object -First 10 Name, Id, WorkingSet64
```

Native executables can be invoked from PowerShell too. The last native exit code is
reported; PowerShell error records still make the operation fail. Output appears
when the operation finishes, not as a streaming interactive shell. Prompts and
interactive console programs are not supported. The operation limit is 120 seconds.
Cancel requests a pipeline stop; confirmed stops keep the runspace. Cancellation
does not undo completed actions or guarantee that child processes stopped.

WMI takes a Windows executable command line and optional working directory:

```text
C:\Windows\System32\cmd.exe /c whoami
```

WMI returns a PID on successful creation, **not stdout, stderr, or the program's
eventual exit code**. Shell operators require an explicit shell such as `cmd.exe /c`.
Remote creation is noninteractive: launching a GUI does not put it on the signed-in
user's desktop. The caller should not repeatedly launch a program to check completion.
See Microsoft's [remote process creation documentation](https://learn.microsoft.com/en-us/windows/win32/wmisdk/creating-processes-remotely).

## Files

Enter an absolute drive directory, refresh, and double-click a directory to open it.
The up-arrow goes to its parent. Filter and sort the returned table; record details
show full paths. WMI filters each listing by Drive and Path, not a disk-wide filename
scan. Listings over 10,000 entries fail without displaying partial results.

Select a file to rename or delete. Rename accepts a new filename in the same folder.
Folder deletion, recursive operations, UNC paths and alternate streams are not
supported. WMI providers and permissions may refuse a file operation without losing
the connection. See the [CIM_DataFile contract](https://learn.microsoft.com/en-us/windows/win32/cimwin32prov/cim-datafile).

WinRM upload/download reuses the existing chunked transfer, SHA-256 verification,
temporary publication and overwrite controls. Upload pre-fills the selected directory
and derives the remote filename from the local selection. WMI-only connections do
not transfer file contents, use SMB, or stage output-capture files.

## Processes And Sessions

Processes includes PID, name, parent PID, session ID, working-set bytes, thread count,
creation time, executable path and command line where permissions permit. Live refresh
runs every 3-60 seconds, only while visible, connected and idle. It pauses during inline
confirmation and stops on a refresh failure or disconnect. Successful background polls
do not fill history; manual refreshes and errors do.

End process rechecks PID plus creation time before acting. A changed or exited process
requires a refresh. WMI cannot atomically compare creation time and terminate: a small
race remains between verification and the provider method. Termination may lose work.

WinRM uses Windows WTS APIs remotely, not localized `quser` text parsing. Select a
signed-in terminal session to disconnect it (leave programs running) or sign it out
(end the user session). The username is rechecked before dispatch. WTS actions are
asynchronous requests; refresh to inspect the resulting state. Session reuse between
verification and the native API call remains possible. WTS query/action permissions
are required, and endpoints that prohibit Add-Type cannot use this view. See
[Microsoft's session administration API overview](https://learn.microsoft.com/en-us/windows/win32/termserv/terminal-services-administration).

WMI LogonId is not a terminal session ID. That view is inventory only, including
service/network logons. Local user accounts can be inspected on either transport;
account creation, password resets and enable/disable are not included in this preview.

## Quieter Feedback

Routine validation, import/export and operation errors stay in the view or status bar.
Destructive tool actions use an inline confirmation with the exact selected target.
Plugins become available automatically when added; there are no activation prompts.
Offline tests run when explicitly selected. Plugin execution has no additional
mutation popup on every invocation. The manifest declaration still gates method
access and read-only batch eligibility; it is not a sandbox.

Runner scripts and operation details are retained in local history. Do not put secrets
in commands; use saved connection credentials. Successful background process refreshes
are not retained. Existing explicit confirmations for service restart, credential
deletion, batch connection approval and library reset remain.

## Manual Acceptance Checks

1. On a connected WinRM host, run `Get-Date`, then `cmd /c exit 9`; verify output and
   failure code, then successfully run another command without reconnecting.
2. Run `Start-Sleep 60`, cancel it, then run `Get-Date`. Do not use a mutating script
   to test cancellation.
3. In a disposable remote directory, browse, upload, download and compare hashes.
   Rename and delete only a test file. Repeat metadata/rename/delete on a WMI host.
4. Launch an approved short-lived test executable over WMI; verify its PID, not GUI
   visibility or stdout. Test process termination only against a disposable process.
5. Enable live process refresh, switch views, run another operation and disconnect.
   Verify refresh does not queue work or silently reconnect.
6. Check user/session inventories. Test disconnect/sign-out only on a disposable
   signed-in test session with no unsaved work.

Automated tests use fixtures and local UI rendering. The WTS helper has been compiled
and its enumeration checked locally. New remote workflows, especially Linux DCOM
method calls, still need these target-side acceptance checks.
