"""Built-in interactive operations, executed on the host's existing worker."""
from __future__ import annotations

import ntpath
from pathlib import Path
from uuid import uuid4

from remoteops.commands.plugin_api import PluginContext
from remoteops.commands.templates import PreparedCommand
from remoteops.remoting.transfer import ps_literal


PROCESS_PROPERTIES = {"ProcessId": "uint32", "Name": "string", "ParentProcessId": "uint32",
                      "SessionId": "uint32", "WorkingSetSize": "uint64", "ThreadCount": "uint32",
                      "CreationDate": "datetime", "ExecutablePath": "string", "CommandLine": "string"}
MUTATIONS = {"terminate", "delete-file", "rename-file", "disconnect-session", "logoff-session"}


def run_script(session, script, cancel):
    from remoteops.remoting.session import PsrpSession
    from remoteops.remoting.output import records_text
    marker = "WMIBenchExit_" + uuid4().hex
    wrapped = "& { $global:LASTEXITCODE=0; . {\n" + script + "\n}; $ok=$?; [pscustomobject]@{" + marker + "=$global:LASTEXITCODE; PipelineSucceeded=$ok} }"
    result = (session.run(wrapped, 120, cancel=cancel) if isinstance(session, PsrpSession)
              else session.run(wrapped, 120))
    rows = [row for row in result.records if marker in row]
    result.records = [row for row in result.records if marker not in row]
    if rows:
        code = rows[-1][marker]
        if type(code) is int and code != 0:
            result.exit_code = code
            if not result.stderr:
                result.stderr = f"Remote command exited with code {code}."
        elif not rows[-1].get("PipelineSucceeded") and result.succeeded:
            result.exit_code = 1
            result.stderr = "Remote pipeline reported failure."
        result.stdout = records_text(result.records)
        streams = getattr(session, "_stream_text", "")
        if streams:
            result.stdout += ("\n" if result.stdout else "") + streams
    elif result.succeeded:
        result.exit_code = 1
        result.stderr = "Script ended before reporting its completion status. Inspect remote state before retrying."
    result.command = script
    return result


def remote_path(value):
    if not isinstance(value, str) or not value or len(value) > 900 or any(c in value for c in '\x00\r\n*?"<>|'):
        raise ValueError("Enter an absolute drive path, such as C:\\Windows.")
    value = value.replace("/", "\\")
    drive, tail = ntpath.splitdrive(value)
    if len(drive) != 2 or not drive[0].isalpha() or drive[1] != ":" or not tail.startswith("\\") or ":" in tail:
        raise ValueError("Use an absolute drive path; UNC paths and alternate streams are not supported.")
    return ntpath.normpath(value)


def number(value, label):
    if type(value) is not int or not 1 <= value <= 0xffffffff:
        raise ValueError(f"Select a valid {label} first.")
    return value


def equal(name, value):
    return {"property": name, "operator": "eq", "value": value}


def context_for_session(session, cancel, audit):
    def call(budget, **kwargs):
        command = PreparedCommand("tools", "tools", "Tools", kwargs.pop("script", ""), {}, **kwargs)
        response = session.run_prepared(command, budget)
        if not response.succeeded:
            raise ValueError(response.stderr or "Remote operation failed")
        return response

    def invoke(plan, budget):
        audit.append(f"{plan.namespace}: {plan.path}.{plan.method} attempted")
        rows = call(budget, native_wmi=True, method_plan=plan).records
        if len(rows) != 1:
            raise ValueError("Method result was not confirmed. Inspect remote state before retrying.")
        audit[-1] += f"; ReturnValue={rows[0].get('ReturnValue')!r}"
        return rows[0]

    return PluginContext(backend=session.host.connection_mode.value, cancel=cancel,
                         timeout_seconds=120, modifies_remote=True,
                         query=lambda plan, budget: call(budget, native_wmi=True, wmi_plan=plan).records,
                         invoke=invoke,
                         powershell=lambda script, budget: {"records": call(budget, script=script).records})


def query(ctx, class_name, properties, filters=None):
    if ctx.backend == "wmi_dcom":
        return ctx.query(class_name=class_name, properties=properties, filters=filters, rows=10000)
    from remoteops.commands.wmi_catalog import wql_literal
    where = " AND ".join(f"{f['property']} = {wql_literal(f['value'])}" for f in (filters or []))
    script = f"Get-CimInstance -ClassName {class_name}"
    if where:
        script += " -Filter " + ps_literal(where)
    # DMTF timestamps give both backends a stable process identity token.
    columns = [name if kind != "datetime" else
               "@{n='" + name + "';e={if ($_." + name + ") {[System.Management.ManagementDateTimeConverter]::ToDmtfDateTime($_." + name + ")}}}"
               for name, kind in properties.items()]
    return ps(ctx, script + " | Select-Object -First 10001 -Property " + ",".join(columns))


def ps(ctx, script):
    rows = ctx.powershell("& { $ErrorActionPreference='Stop'; " + script + " }")["records"]
    if len(rows) > 10000:
        raise ValueError("More than 10,000 items matched. No partial results displayed.")
    return rows


def invoke(ctx, path, method, inputs=None, outputs=None):
    result = ctx.invoke(path, method, inputs=inputs, outputs=outputs)
    ctx.require_success(result, message=method)
    return [result]


def perform(ctx, action, args):
    wmi = ctx.backend == "wmi_dcom"
    if action == "launch":
        command = args.get("command", "").strip()
        if not command or len(command) > 30000 or "\x00" in command:
            raise ValueError("Enter a command line of at most 30,000 characters.")
        if not wmi:
            raise ValueError("Use the PowerShell runner on WinRM hosts.")
        inputs = {"CommandLine": ("string", command)}
        if args.get("directory", "").strip():
            inputs["CurrentDirectory"] = ("string", remote_path(args["directory"]))
        return invoke(ctx, "Win32_Process", "Create", inputs, {"ReturnValue": "uint32", "ProcessId": "uint32"})
    if action == "processes":
        return query(ctx, "Win32_Process", PROCESS_PROPERTIES)
    if action == "terminate":
        pid = number(args.get("ProcessId"), "process")
        created = args.get("CreationDate")
        rows = query(ctx, "Win32_Process", PROCESS_PROPERTIES, [equal("ProcessId", pid)])
        if not created or len(rows) != 1 or rows[0].get("CreationDate") != created:
            raise ValueError("Process changed or exited. Refresh the process list.")
        if wmi:
            return invoke(ctx, ctx.object_path("Win32_Process", {"Handle": str(pid)}), "Terminate", {"Reason": ("uint32", 0)})
        # Retain and recheck the process handle before killing; no PID-only Stop-Process.
        return ps(ctx, f"$p=Get-Process -Id {pid}; $h=$p.Handle; "
                  f"if ([System.Management.ManagementDateTimeConverter]::ToDmtfDateTime($p.StartTime) -ne {ps_literal(created)}) "
                  "{throw 'Process changed. Refresh the process list.'}; $p.Kill(); [pscustomobject]@{Terminated=$p.Id}")
    if action == "files":
        path = remote_path(args.get("path", ""))
        if not wmi:
            return ps(ctx, "$p=" + ps_literal(path) + "; if (-not (Test-Path -LiteralPath $p -PathType Container)) "
                      "{throw 'Directory not found'}; Get-ChildItem -LiteralPath $p -Force | Select-Object -First 10001 "
                      "@{n='Name';e={$_.FullName}},@{n='Kind';e={if ($_.PSIsContainer) {'Directory'} else {'File'}}},"
                      "@{n='FileSize';e={$_.Length}},LastWriteTime,Attributes")
        drive, tail = ntpath.splitdrive(path)
        folder = tail.rstrip("\\") + "\\"
        exists = ctx.query(class_name="Win32_Directory", properties={"Name": "string"}, filters=[equal("Name", path)], rows=1)
        if not exists:
            raise ValueError("Directory not found or not accessible.")
        rows = []
        for cls, kind in (("Win32_Directory", "Directory"), ("CIM_DataFile", "File")):
            props = {"Name": "string", "Drive": "string", "Path": "string", "FileSize": "uint64", "LastModified": "datetime"}
            items = ctx.query(class_name=cls, properties=props, filters=[equal("Drive", drive), equal("Path", folder)], rows=10000)
            rows.extend({**item, "Kind": kind} for item in items if item["Name"].casefold() != path.casefold())
        if len(rows) > 10000:
            raise ValueError("More than 10,000 items matched. No partial results displayed.")
        return rows
    if action in {"delete-file", "rename-file"}:
        path = remote_path(args.get("Name", ""))
        if args.get("Kind") != "File":
            raise ValueError("Select a file; folder deletion is not supported.")
        destination = None
        if action == "rename-file":
            name = args.get("new_name", "")
            if not name or name in {".", ".."} or any(c in name for c in '\\/:*?"<>|\x00\r\n') or name.endswith((" ", ".")):
                raise ValueError("Enter a file name without a path.")
            destination = remote_path(ntpath.join(ntpath.dirname(path), name))
        if wmi:
            return invoke(ctx, ctx.object_path("CIM_DataFile", {"Name": path}), "Rename" if destination else "Delete",
                          {"FileName": ("string", destination)} if destination else None)
        script = "$p=" + ps_literal(path) + "; $f=Get-Item -LiteralPath $p -Force; if ($f.PSIsContainer) {throw 'Select a file'}; "
        script += ("Rename-Item -LiteralPath $p -NewName " + ps_literal(args["new_name"]) if destination else "Remove-Item -LiteralPath $p -Force")
        return ps(ctx, script + "; [pscustomobject]@{Path=$p;Completed=$true}")
    if action == "users":
        return query(ctx, "Win32_UserAccount", {"Name": "string", "Domain": "string", "SID": "string",
                                              "Disabled": "boolean", "Lockout": "boolean", "LocalAccount": "boolean"},
                     [equal("LocalAccount", True)])
    if action == "sessions" and wmi:
        return query(ctx, "Win32_LogonSession", {"LogonId": "string", "LogonType": "uint32", "StartTime": "datetime",
                                               "AuthenticationPackage": "string"})
    if action in {"sessions", "disconnect-session", "logoff-session"}:
        if wmi:
            raise ValueError("Session disconnect/logoff requires WinRM; WMI logon IDs are not terminal session IDs.")
        script = Path(__file__).with_name("terminal_sessions.ps1").read_text(encoding="utf-8")
        if action == "sessions":
            return ps(ctx, script + "\n[WMIBench.TerminalSessions]::List()")
        sid = number(args.get("SessionId"), "terminal session")
        user = args.get("User", "")
        if not user:
            raise ValueError("Select a signed-in user session.")
        return ps(ctx, script + f"\n[WMIBench.TerminalSessions]::Change({sid}, {ps_literal(user)}, "
                  + ("$true" if action == "logoff-session" else "$false") + "); [pscustomobject]@{Requested=$true}")
    raise ValueError(f"Unknown tool action: {action}")
