from datetime import datetime
from pathlib import PureWindowsPath
import re
import xml.etree.ElementTree as ET

from remoteops.commands.plugin_api import PluginError

NAMESPACE = "root/Microsoft/Windows/TaskScheduler"
STATES = {0: "Unknown", 1: "Disabled", 2: "Queued", 3: "Ready", 4: "Running"}
ACCOUNTS = {"SYSTEM": "S-1-5-18", "LOCAL SERVICE": "S-1-5-19", "NETWORK SERVICE": "S-1-5-20"}
CREATE_OPTIONS = {"executable", "arguments", "working_directory", "account", "schedule", "start", "description"}


def task_name(value):
    if not isinstance(value, str) or not value.strip() or re.search(r'[\\/:*?"<>|\x00-\x1f]', value):
        raise PluginError("Provide one exact task name, without a folder or wildcards.")
    if value != value.strip() or value in (".", ".."):
        raise PluginError("Task names cannot have surrounding whitespace or be dot segments.")
    return value


def task_path(value):
    if not isinstance(value, str) or not value.startswith("\\") or not value.endswith("\\"):
        raise PluginError("Task path must start and end with a backslash, e.g. \\Maintenance\\.")
    parts = value[1:-1].split("\\") if value != "\\" else []
    for part in parts:
        task_name(part)
    return value


def absolute_path(value, label):
    if not isinstance(value, str) or not value.strip() or not PureWindowsPath(value).is_absolute():
        raise PluginError(f"{label} must be an absolute remote Windows path.")
    if any(char in value for char in ('"', '\0', '\r', '\n', '*', '?')):
        raise PluginError(f"{label} cannot contain quotes, wildcards or control characters.")
    return value


def task_xml(args):
    executable = absolute_path(args.get("executable"), "Executable")
    account = args.get("account")
    if account not in ACCOUNTS:
        raise PluginError("Create requires an explicit account: SYSTEM, LOCAL SERVICE or NETWORK SERVICE.")
    schedule = args.get("schedule", "demand")
    if schedule not in ("demand", "once", "daily"):
        raise PluginError("Schedule must be demand, once or daily.")
    start = args.get("start")
    if schedule == "demand" and start is not None:
        raise PluginError("A demand-only task does not accept start.")
    if schedule != "demand":
        try:
            when = datetime.fromisoformat(start)
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError()
            start = when.isoformat(timespec="seconds")
        except (TypeError, ValueError):
            raise PluginError("Once/daily requires start as an ISO datetime with an explicit UTC offset.") from None
    working = args.get("working_directory")
    if working is not None:
        absolute_path(working, "Working directory")
    root = ET.Element("Task", {"version": "1.2", "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task"})
    if args.get("description"):
        ET.SubElement(ET.SubElement(root, "RegistrationInfo"), "Description").text = args["description"]
    if schedule != "demand":
        trigger = ET.SubElement(ET.SubElement(root, "Triggers"), "TimeTrigger" if schedule == "once" else "CalendarTrigger")
        ET.SubElement(trigger, "StartBoundary").text = start
        ET.SubElement(trigger, "Enabled").text = "true"
        if schedule == "daily":
            ET.SubElement(ET.SubElement(trigger, "ScheduleByDay"), "DaysInterval").text = "1"
    principal = ET.SubElement(ET.SubElement(root, "Principals"), "Principal", {"id": "TaskAccount"})
    ET.SubElement(principal, "UserId").text = ACCOUNTS[account]
    # ServiceAccount is an API enum, not a valid task XML LogonType value.
    # Built-in service accounts are identified by UserId; omit LogonType.
    ET.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    settings = ET.SubElement(root, "Settings")
    ET.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ET.SubElement(settings, "AllowStartOnDemand").text = "true"
    ET.SubElement(settings, "Enabled").text = "true"
    action = ET.SubElement(ET.SubElement(root, "Actions", {"Context": "TaskAccount"}), "Exec")
    ET.SubElement(action, "Command").text = executable
    if args.get("arguments"):
        ET.SubElement(action, "Arguments").text = args["arguments"]
    if working is not None:
        ET.SubElement(action, "WorkingDirectory").text = working
    xml = ET.tostring(root, encoding="unicode")
    if re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', xml):
        raise PluginError("Task text contains characters XML does not allow.")
    if len(xml.encode("utf-16-le")) > 131072:
        raise PluginError("Task XML exceeds the 65536 UTF-16-unit API limit.")
    return xml


def run(ctx, args):
    action = args.get("action", "list")
    if action not in ("list", "create", "run", "stop", "export", "delete"):
        raise PluginError("Unknown task action.")
    if action != "create" and CREATE_OPTIONS.intersection(args):
        raise PluginError("Executable, account and schedule options only apply to create.")
    name = task_name(args["name"]) if "name" in args else None
    path = task_path(args["path"]) if "path" in args else None
    if action == "list":
        filters = []
        if name is not None:
            filters.append({"property": "TaskName", "operator": "eq", "value": name})
        if path is not None:
            filters.append({"property": "TaskPath", "operator": "eq", "value": path})
        rows = ctx.query(namespace=NAMESPACE, class_name="MSFT_ScheduledTask",
                         properties={"TaskPath": "string", "TaskName": "string", "State": "sint32", "Description": "string"},
                         filters=filters, rows=args.get("limit", 1000))
        return [{**row, "StateLabel": STATES.get(row["State"], "Unknown")} for row in rows]
    if name is None:
        raise PluginError("This action requires an exact task name.")
    path = path or "\\"
    identity = {"TaskPath": path, "TaskName": name}
    if action == "delete":
        result = ctx.delete_instance(ctx.object_path("MSFT_ScheduledTask", identity), namespace=NAMESPACE)
        return [{**identity, **result}]
    outputs = {"ReturnValue": "uint32"}
    if action == "create":
        xml = task_xml(args)
        # A preflight check avoids accidental replacement; Force=False is also sent.
        existing = ctx.query(namespace=NAMESPACE, class_name="MSFT_ScheduledTask",
                             properties={"TaskPath": "string", "TaskName": "string"},
                             filters=[{"property": key, "operator": "eq", "value": value}
                                      for key, value in identity.items()], rows=2)
        if existing:
            raise PluginError("Task already exists. This plugin does not replace tasks; choose another name.")
        method = "RegisterByXml"
        inputs = {"Force": ("boolean", False), "Xml": ("string", xml), "Password": ("string", None),
                  "User": ("string", ACCOUNTS[args["account"]]), "TaskPath": ("string", path), "TaskName": ("string", name)}
    elif action == "export":
        method = "ExportByName"
        inputs = {"TaskName": ("string", name), "TaskPath": ("string", path)}
        outputs["cmdletOutput"] = "string"
    else:
        method = "StartByPath" if action == "run" else "StopByPath"
        inputs = {"TaskPath": ("string", path), "TaskName": ("string", name)}
    ctx.progress(f"{method}: {path}{name}")
    options = {}
    if action in ("run", "stop"):
        if "invoke_allow_no_output" not in ctx.capabilities:
            raise PluginError("Update WMIBench for scheduled-task run/stop support; no action was sent.")
        options["allow_no_output"] = True
    try:
        result = ctx.invoke("PS_ScheduledTask", method, namespace=NAMESPACE, inputs=inputs, outputs=outputs, **options)
    except PluginError as exc:
        if action in ("run", "stop"):
            raise PluginError(f"{method} failed for {path}{name}. Check the exact task name, path and permissions.\n"
                              f"Original error: {exc}") from exc
        if action != "create":
            raise
        raise PluginError(f"RegisterByXml failed for {path}{name}. Check whether the task exists before retrying. "
                          f"The provider did not confirm creation.\nOriginal error: {exc}") from exc
    if action in ("run", "stop") and result == {}:
        return [{**identity, "Action": action, "Status": "Request completed",
                 "ProviderOutput": "None returned", "TaskCompletion": "Not checked"}]
    ctx.require_success(result, message=method)
    return [{**identity, "Action": action, **result}]
