"""Transport-independent built-in WMI commands. No shell or transport fallback.

Query callbacks enumerate completely unless take explicitly requests a prefix.
They enforce max_rows (probing for an extra row for complete queries), and raise
on truncation, timeout, or unsupported CIM types.
They return provider property names and plain Python values, including lists for
arrays and strings for CIM references/datetimes. Method callbacks return a dict
containing an integer ReturnValue and must never retry a mutating method.

Namespaces are explicit and relative to the connected host. The executor cannot
interrupt a blocking callback: adapters must enforce the remaining timeout.
"""

from __future__ import annotations

import json
import math
import ntpath
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

CIM_SINT32 = 3
CIM_STRING = 8
CIM_BOOLEAN = 11
CIM_UINT8 = 17
CIM_UINT16 = 18
CIM_UINT32 = 19
CIM_UINT64 = 21
CIM_DATETIME = 101
CIM_REFERENCE = 102
CIM_ARRAY = 0x2000

DEFAULT_MAX_ROWS = 10000
MAX_EVENT_BYTES = 32 * 1024 * 1024
CIMV2 = "root/cimv2"
STANDARD_CIMV2 = "root/standardcimv2"
TASK_SCHEDULER = "root/microsoft/windows/taskscheduler"
REGISTRY_NAMESPACE = "root/default"

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SCALAR_TYPES = frozenset({2, 3, 4, 5, 8, 11, 16, 17, 18, 19, 20, 21, 101, 102, 103})


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError(f"Invalid WMI identifier: {value!r}")


def _text(value: object) -> str:
    if not isinstance(value, str) or any(
        ord(char) < 32 or 127 <= ord(char) <= 159 or 0xD800 <= ord(char) <= 0xDFFF
        for char in value
    ):
        raise ValueError("WMI text must be a string without control characters or surrogates")
    return value


def _quote(value: str, delimiter: str) -> str:
    escaped = _text(value).replace("\\", "\\\\").replace(delimiter, "\\" + delimiter)
    return delimiter + escaped + delimiter


def wql_literal(value: object) -> str:
    """Encode a WQL value, never an identifier or a query fragment.

    WQL uses backslash escaping, not SQL/PowerShell doubled apostrophes.
    None is rendered as NULL; callers must use IS/IS NOT for null comparisons.
    """
    if value is None:
        return "NULL"
    if isinstance(value, str):
        return _quote(value, "'")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return repr(value)
    raise ValueError("Unsupported WQL literal type or non-finite number")


@dataclass(frozen=True)
class QuerySpec:
    """A SELECT projection with CIM types (array flag is 0x2000).

    where is an internally authored clause, not a raw user-input API.
    Bind values with wql_literal. Identifiers, CIM types, and limits are checked
    again when rendering because properties retains the proposed dict interface.
    """

    namespace: str
    class_name: str
    properties: dict[str, int]
    where: str = ""
    max_rows: int = DEFAULT_MAX_ROWS
    take: int | None = None

    def __post_init__(self) -> None:
        self._validate()
        object.__setattr__(self, "namespace", self.namespace.replace("\\", "/"))
        object.__setattr__(self, "properties", dict(self.properties))

    def _validate(self) -> None:
        if not isinstance(self.namespace, str):
            raise ValueError("Invalid WMI namespace")
        parts = self.namespace.replace("\\", "/").split("/")
        if parts[0].casefold() != "root":
            raise ValueError("WMI namespace must be relative to the connected host under root")
        for part in parts:
            _identifier(part)
        _identifier(self.class_name)
        if not isinstance(self.properties, dict) or not self.properties:
            raise ValueError("A WMI query requires a nonempty property/type dictionary")
        seen = set()
        for name, kind in self.properties.items():
            _identifier(name)
            if name.casefold() in seen:
                raise ValueError(f"Duplicate WMI property: {name}")
            seen.add(name.casefold())
            if type(kind) is not int or kind not in _SCALAR_TYPES | {
                value | CIM_ARRAY for value in _SCALAR_TYPES
            }:
                raise ValueError(f"Unsupported CIM type for {name}: {kind!r}")
        _text(self.where)
        if type(self.max_rows) is not int or not 1 <= self.max_rows <= DEFAULT_MAX_ROWS:
            raise ValueError(f"max_rows must be an integer in 1..{DEFAULT_MAX_ROWS}")
        if self.take is not None and (type(self.take) is not int or not 1 <= self.take <= self.max_rows):
            raise ValueError("take must be an integer in 1..max_rows or None")

    @property
    def wql(self) -> str:
        self._validate()
        query = f"SELECT {', '.join(self.properties)} FROM {self.class_name}"
        return query + (f" WHERE {self.where}" if self.where else "")


_SERVICE = QuerySpec(CIMV2, "Win32_Service", {
    "Name": CIM_STRING, "DisplayName": CIM_STRING, "State": CIM_STRING,
    "StartMode": CIM_STRING,
})
_PROCESS = QuerySpec(CIMV2, "Win32_Process", {
    "Name": CIM_STRING, "ProcessId": CIM_UINT32, "KernelModeTime": CIM_UINT64,
    "UserModeTime": CIM_UINT64, "WorkingSetSize": CIM_UINT64, "ExecutablePath": CIM_STRING,
})

# None marks a workflow requiring execute_command, not a single SELECT.
WMI_CATALOG: dict[str, QuerySpec | None] = {
    "sysinfo": QuerySpec(CIMV2, "Win32_OperatingSystem", {
        "CSName": CIM_STRING, "Caption": CIM_STRING, "Version": CIM_STRING,
        "BuildNumber": CIM_STRING, "OSArchitecture": CIM_STRING,
        "LastBootUpTime": CIM_DATETIME, "TotalVisibleMemorySize": CIM_UINT64,
    }),
    "disk": QuerySpec(CIMV2, "Win32_LogicalDisk", {
        "DeviceID": CIM_STRING, "VolumeName": CIM_STRING,
        "FreeSpace": CIM_UINT64, "Size": CIM_UINT64,
    }, "DriveType=3"),
    "dir": None,
    "services": _SERVICE,
    "restart-service": None,
    "admins": None,
    "pending-reboot": None,
    "topcpu": _PROCESS,
    "procs": _PROCESS,
    "events": QuerySpec(CIMV2, "Win32_NTLogEvent", {
        "Logfile": CIM_STRING, "RecordNumber": CIM_UINT32, "TimeGenerated": CIM_DATETIME,
        "EventCode": CIM_UINT16, "SourceName": CIM_STRING,
        "EventType": CIM_UINT8, "Message": CIM_STRING,
    }),
    "hotfixes": QuerySpec(CIMV2, "Win32_QuickFixEngineering", {
        "HotFixID": CIM_STRING, "Description": CIM_STRING,
        "InstalledBy": CIM_STRING, "InstalledOn": CIM_STRING,
    }),
    "net": QuerySpec(CIMV2, "Win32_NetworkAdapterConfiguration", {
        "Description": CIM_STRING, "IPAddress": CIM_ARRAY | CIM_STRING,
        "IPSubnet": CIM_ARRAY | CIM_STRING, "DefaultIPGateway": CIM_ARRAY | CIM_STRING,
        "DNSServerSearchOrder": CIM_ARRAY | CIM_STRING,
    }, "IPEnabled=TRUE"),
    "ports": QuerySpec(STANDARD_CIMV2, "MSFT_NetTCPConnection", {
        "LocalAddress": CIM_STRING, "LocalPort": CIM_UINT16,
        "OwningProcess": CIM_UINT32, "CreationTime": CIM_DATETIME,
    }, "State=2"),
    "shares": QuerySpec(CIMV2, "Win32_Share", {
        "Name": CIM_STRING, "Path": CIM_STRING, "Description": CIM_STRING, "Type": CIM_UINT32,
    }),
    "users": QuerySpec(CIMV2, "Win32_UserAccount", {
        "Name": CIM_STRING, "Disabled": CIM_BOOLEAN, "Lockout": CIM_BOOLEAN,
        "SID": CIM_STRING, "Description": CIM_STRING,
    }, "LocalAccount=TRUE"),
    "groups": QuerySpec(CIMV2, "Win32_Group", {
        "Name": CIM_STRING, "SID": CIM_STRING, "Description": CIM_STRING,
    }, "LocalAccount=TRUE"),
    "firewall": QuerySpec(STANDARD_CIMV2, "MSFT_NetFirewallProfile", {
        "Name": CIM_STRING, "Enabled": CIM_UINT16, "DefaultInboundAction": CIM_UINT16,
        "DefaultOutboundAction": CIM_UINT16, "NotifyOnListen": CIM_UINT16,
    }),
    "tasks": QuerySpec(TASK_SCHEDULER, "MSFT_ScheduledTask", {
        "TaskName": CIM_STRING, "TaskPath": CIM_STRING, "State": CIM_SINT32,
        "Author": CIM_STRING,
    }),
    "startup": QuerySpec(CIMV2, "Win32_StartupCommand", {
        "Name": CIM_STRING, "Command": CIM_STRING, "Location": CIM_STRING, "User": CIM_STRING,
    }),
    "sessions": QuerySpec(CIMV2, "Win32_LogonSession", {
        "LogonId": CIM_STRING, "LogonType": CIM_UINT32, "StartTime": CIM_DATETIME,
        "AuthenticationPackage": CIM_STRING,
    }, "LogonType=2 OR LogonType=10 OR LogonType=11 OR LogonType=12"),
    "svc": _SERVICE,
    "uptime": QuerySpec(CIMV2, "Win32_OperatingSystem", {
        "CSName": CIM_STRING, "LastBootUpTime": CIM_DATETIME, "LocalDateTime": CIM_DATETIME,
    }),
}
WMI_ALIASES = frozenset(WMI_CATALOG)

WMI_NOTES = {
    "sysinfo": "Seven native OS fields; TotalVisibleMemorySize is in KiB, dates are CIM text.",
    "disk": "Fixed disks only; FreeGB and SizeGB use 1024**3 bytes. Unknown sizes stay null.",
    "dir": "Lists one directory level using Win32_Directory and CIM_DataFile.",
    "services": "Native State and StartMode fields; these differ from Get-Service columns.",
    "restart-service": (
        "Stops, waits for Stopped, starts, and waits for Running within one deadline. "
        "An already stopped service is started. No force or dependent-service stop; "
        "method errors/timeouts are not retried and may leave the service stopped."
    ),
    "admins": (
        "Resolves local Administrators by SID S-1-5-32-544. Returns Win32_GroupUser "
        "GroupComponent/PartComponent references, including nested groups; "
        "does not expand groups or infer PrincipalSource."
    ),
    "pending-reboot": (
        "Reads two HKLM reboot marker keys with root/default StdRegProv.EnumKey. "
        "Missing keys are false; access/provider errors fail. These signals are not exhaustive."
    ),
    "topcpu": (
        "Top processes by cumulative CPU seconds, not utilization percent. "
        "CPU is null if either time counter is unavailable; unknowns sort last."
    ),
    "procs": (
        "CPU seconds = (KernelModeTime + UserModeTime) / 10000000; memory is bytes. "
        "Process names include executable extensions; protected-process paths may be null."
    ),
    "events": (
        "Classic Win32_NTLogEvent logs only, not all Get-WinEvent channels. Finds the highest "
        "record number with bounded probes, then retrieves up to count records in descending "
        "record-number ranges, not a whole-log download. Provider enumeration order is not assumed. "
        "New arrivals after the upper bound are excluded; log clearing/rotation is not a "
        "transactional snapshot and uint32 record-number wrap is not resolved. "
        "Security logs require appropriate privileges."
    ),
    "hotfixes": (
        "Win32_QuickFixEngineering inventory; InstalledOn is provider text, "
        "so no locale-dependent date sorting is attempted."
    ),
    "net": "IP-enabled adapter configurations; addresses, subnets, gateways and DNS stay arrays.",
    "ports": "TCP listeners (State=2); requires root/standardcimv2 MSFT_NetTCPConnection.",
    "shares": "Win32_Share inventory via WMI; native Type, no SMB transport or SMB cmdlet fields.",
    "users": "Local Win32_UserAccount fields; Disabled/Lockout, no LastLogon or PasswordLastSet.",
    "groups": "Local Win32_Group Name, SID, and Description.",
    "firewall": (
        "Requires root/standardcimv2 MSFT_NetFirewallProfile; uint16 policy values are "
        "preserved, including NotConfigured, rather than converted to booleans."
    ),
    "tasks": (
        "Requires root/microsoft/windows/taskscheduler MSFT_ScheduledTask; "
        "native numeric State (0 unknown, 1 disabled, 2 queued, 3 ready, 4 running)."
    ),
    "startup": "Win32_StartupCommand inventory; not every possible startup mechanism.",
    "sessions": (
        "Interactive logon types 2, 10, 11, 12 from Win32_LogonSession. These are security "
        "logon sessions, not quser terminal sessions: no username, terminal session ID, "
        "connection state, or idle time; multiple logons may refer to one user."
    ),
    "svc": "Exact service Name filter, not a wildcard; returns native State and StartMode.",
    "uptime": (
        "Uptime uses the target's LocalDateTime minus LastBootUpTime, with CIM UTC offsets. "
        "Unknown or invalid dates yield null uptime."
    ),
}


def supported_wmi_aliases() -> frozenset[str]:
    """Catalog coverage; adapters still need the required provider/codec capabilities."""
    return WMI_ALIASES


def wmi_notes(alias: str) -> str:
    return WMI_NOTES[_alias(alias)]


def _alias(alias: str) -> str:
    if not isinstance(alias, str) or alias not in WMI_CATALOG:
        raise ValueError(f"Unknown WMI command: {alias!r}")
    return alias


def _values(alias: str, values: Mapping[str, object]) -> dict[str, object]:
    _alias(alias)
    if not isinstance(values, Mapping):
        raise ValueError("Command values must be a mapping")
    defaults: dict[str, object] = {}
    if alias == "dir":
        defaults = {"path": "C:\\"}
    elif alias in {"svc", "restart-service"}:
        defaults = {"service_name": "Spooler"}
    elif alias == "events":
        defaults = {"log": "System", "count": 50}
    elif alias == "topcpu":
        defaults = {"count": 15}
    if any(key not in defaults for key in values):
        raise ValueError(f"Unexpected parameter for WMI command {alias}")
    result = defaults | dict(values)
    for key in ("service_name", "log", "path"):
        if key in result and not _text(result[key]).strip():
            raise ValueError(f"{key} must not be empty")
    if "count" in result:
        count = result["count"]
        if isinstance(count, str) and re.fullmatch(r"[0-9]{1,5}", count):
            count = int(count)
        if type(count) is not int or not 1 <= count <= DEFAULT_MAX_ROWS:
            raise ValueError(f"count must be an integer in 1..{DEFAULT_MAX_ROWS}")
        result["count"] = count
    return result


def make_query(alias: str, values: Mapping[str, object]) -> QuerySpec:
    """Compile a simple built-in query. Workflows require execute_command."""
    parameters = _values(alias, values)
    spec = WMI_CATALOG[alias]
    if spec is None:
        raise ValueError(f"{alias} requires execute_command, not a single query")
    where = spec.where
    if alias == "svc":
        where = "Name=" + wql_literal(parameters["service_name"])
    elif alias == "events":
        where = "Logfile=" + wql_literal(parameters["log"])
    return replace(spec, where=where)


class WmiCommandError(RuntimeError):
    """A complete, successful result could not be obtained from the provider."""


QueryCallback = Callable[[QuerySpec, float], list[dict]]
MethodCallback = Callable[[str, str, str, dict[str, tuple[int, object]], float], dict]


class _Execution:
    def __init__(self, query: QueryCallback, method: MethodCallback, timeout: float):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout_seconds must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be a finite positive number")
        self.deadline = time.monotonic() + timeout
        if not math.isfinite(self.deadline):
            raise ValueError("timeout_seconds is too large")
        self.query_callback = query
        self.method_callback = method
        self.actions: list[str] = []

    def remaining(self) -> float:
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("WMI command exceeded its shared deadline")
        return seconds

    def query(self, spec: QuerySpec) -> list[dict]:
        action = f"{spec.namespace}: {spec.wql}"
        remaining = self.remaining()
        self.actions.append(action)
        rows = self.query_callback(spec, remaining)
        self.remaining()
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise WmiCommandError("Query callback must return a complete list of records")
        if len(rows) > spec.max_rows:
            raise WmiCommandError(f"WMI enumeration exceeded row cap {spec.max_rows}")
        if spec.take is not None and len(rows) > spec.take:
            raise WmiCommandError("Query callback exceeded its requested prefix length")
        return [dict(row) for row in rows]

    def method(self, namespace: str, path: str, method: str,
               inputs: dict[str, tuple[int, object]]) -> int:
        arguments = ", ".join(f"{key}={wql_literal(value)}" for key, (_, value) in inputs.items())
        remaining = self.remaining()
        self.actions.append(f"{namespace}: {path}.{method}({arguments})")
        output = self.method_callback(namespace, path, method, inputs, remaining)
        self.remaining()
        status = output.get("ReturnValue") if isinstance(output, dict) else None
        if type(status) is not int or not 0 <= status <= 0xFFFFFFFF:
            raise WmiCommandError(f"{method} omitted a valid uint32 ReturnValue")
        return status


def _path(class_name: str, **keys: str) -> str:
    _identifier(class_name)
    for key in keys:
        _identifier(key)
    return class_name + "." + ",".join(f'{key}={_quote(value, chr(34))}'
                                       for key, value in keys.items())


def _admins(execution: _Execution) -> list[dict]:
    groups = execution.query(QuerySpec(CIMV2, "Win32_Group", {
        "Name": CIM_STRING, "Domain": CIM_STRING, "SID": CIM_STRING,
    }, "SID='S-1-5-32-544' AND LocalAccount=TRUE"))
    if len(groups) != 1:
        raise WmiCommandError("Expected one local Administrators group with SID S-1-5-32-544")
    group = groups[0]
    if any(not isinstance(group.get(key), str) or not group[key] for key in ("Name", "Domain")):
        raise WmiCommandError("Administrators group is missing its Name/Domain keys")
    reference = _path("Win32_Group", Domain=group["Domain"], Name=group["Name"])
    return execution.query(QuerySpec(CIMV2, "Win32_GroupUser", {
        "GroupComponent": CIM_REFERENCE, "PartComponent": CIM_REFERENCE,
    }, "GroupComponent=" + wql_literal(reference)))


_REBOOT_KEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\Component Based Servicing\RebootPending",
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\WindowsUpdate\Auto Update\RebootRequired",
)


def _pending_reboot(execution: _Execution) -> list[dict]:
    rows = []
    for key in _REBOOT_KEYS:
        status = execution.method(REGISTRY_NAMESPACE, "StdRegProv", "EnumKey", {
            "hDefKey": (CIM_UINT32, 0x80000002), "sSubKeyName": (CIM_STRING, key),
        })
        if status not in {0, 2, 3}:
            raise WmiCommandError(f"StdRegProv.EnumKey failed for HKLM\\{key}: ReturnValue={status}")
        rows.append({"Path": "HKLM:\\" + key, "Exists": status == 0})
    return rows


def _directory_parts(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value or len(value) > 900 or any(
        character in value for character in '\x00\r\n*?"<>|'
    ):
        raise WmiCommandError("Enter an absolute drive path, such as C:\\Windows.")
    path = value.replace("/", "\\")
    drive, tail = ntpath.splitdrive(path)
    if (len(drive) != 2 or not drive[0].isalpha() or drive[1] != ":"
            or not tail.startswith("\\") or ":" in tail):
        raise WmiCommandError("Use an absolute drive path; UNC paths and alternate streams are not supported.")
    path = ntpath.normpath(path)
    _, tail = ntpath.splitdrive(path)
    return path, drive, tail.rstrip("\\") + "\\"


def _directory(execution: _Execution, values: dict[str, object]) -> list[dict]:
    path, drive, folder = _directory_parts(values["path"])
    exists = execution.query(QuerySpec(CIMV2, "Win32_Directory", {
        "Name": CIM_STRING,
    }, "Name=" + wql_literal(path), max_rows=1, take=1))
    if not exists:
        raise WmiCommandError("Directory not found or not accessible.")

    directories = execution.query(QuerySpec(CIMV2, "Win32_Directory", {
        "Name": CIM_STRING, "Drive": CIM_STRING, "Path": CIM_STRING,
        "LastModified": CIM_DATETIME,
    }, "Drive=" + wql_literal(drive) + " AND Path=" + wql_literal(folder)))
    files = execution.query(QuerySpec(CIMV2, "CIM_DataFile", {
        "Name": CIM_STRING, "Drive": CIM_STRING, "Path": CIM_STRING,
        "FileSize": CIM_UINT64, "LastModified": CIM_DATETIME,
    }, "Drive=" + wql_literal(drive) + " AND Path=" + wql_literal(folder)))

    rows = []
    for row in directories:
        name = row.get("Name")
        if isinstance(name, str) and name.casefold() != path.casefold():
            rows.append({
                "Name": name,
                "Kind": "Directory",
                "FileSize": None,
                "LastModified": row.get("LastModified"),
            })
    for row in files:
        name = row.get("Name")
        if isinstance(name, str):
            rows.append({
                "Name": name,
                "Kind": "File",
                "FileSize": row.get("FileSize"),
                "LastModified": row.get("LastModified"),
            })
    if len(rows) > DEFAULT_MAX_ROWS:
        raise WmiCommandError("More than 10,000 items matched. No partial results displayed.")
    return sorted(rows, key=lambda row: (row["Kind"] != "Directory", row["Name"].casefold()))


def _service_row(execution: _Execution, spec: QuerySpec) -> dict:
    rows = execution.query(spec)
    if len(rows) != 1:
        raise WmiCommandError("Service was not found or its name was not unique")
    return rows[0]


def _event_number(row: dict) -> int:
    number = _uint(row.get("RecordNumber"))
    if number is None or number > 0xFFFFFFFF:
        raise WmiCommandError("Event omitted a valid uint32 RecordNumber")
    return number


def _events(execution: _Execution, values: dict[str, object]) -> list[dict]:
    spec = make_query("events", values)

    def ranged(lower, upper, **options):
        return replace(spec, where=f"{spec.where} AND RecordNumber>={lower} AND RecordNumber<={upper}",
                       **options)

    def probe(lower, upper):
        rows = execution.query(ranged(lower, upper, properties={"RecordNumber": CIM_UINT32},
                                      max_rows=1, take=1))
        if not rows:
            return None
        number = _event_number(rows[0])
        if not lower <= number <= upper:
            raise WmiCommandError("Event provider returned a record outside the requested range")
        return number

    def newest(upper):
        seed = probe(0, upper)
        if seed is None or seed == upper:
            return seed
        # A one-row seed may already be the maximum, but WQL has no ORDER BY
        # guarantee. Verify that fast path; otherwise binary-seek the maximum.
        candidate = probe(seed + 1, upper)
        if candidate is None:
            return seed
        lower = candidate
        while lower < upper:
            middle = (lower + upper + 1) // 2
            candidate = probe(middle, upper)
            if candidate is None:
                upper = middle - 1
            else:
                lower = candidate
        return lower

    rows = []
    size = 0
    upper = newest(0xFFFFFFFF)
    while upper is not None and len(rows) < values["count"]:
        remaining = values["count"] - len(rows)
        lower = max(0, upper - remaining + 1)
        batch = execution.query(ranged(lower, upper, max_rows=remaining))
        size += len(json.dumps(batch, ensure_ascii=True).encode("utf-8"))
        if size > MAX_EVENT_BYTES:
            raise WmiCommandError("Events exceeded the aggregate size limit; no partial results returned")
        numbers = [_event_number(row) for row in batch]
        if len(numbers) != len(set(numbers)) or any(not lower <= number <= upper for number in numbers):
            raise WmiCommandError("Event provider returned duplicate or out-of-range records")
        if upper not in numbers:
            raise WmiCommandError("Event log changed during retrieval; run events again")
        for row, number in zip(batch, numbers):
            row["RecordNumber"] = number
        rows.extend(sorted(batch, key=lambda row: row["RecordNumber"], reverse=True))
        if lower == 0 or len(rows) >= values["count"]:
            break
        # Seek across cleared/rotated gaps without walking every missing record ID.
        upper = newest(lower - 1)
    return rows


def _restart_service(execution: _Execution, name: str) -> list[dict]:
    spec = make_query("svc", {"service_name": name})
    path = _path("Win32_Service", Name=name)
    row = _service_row(execution, spec)
    if row.get("State") not in {"Running", "Stopped", "Paused"}:
        raise WmiCommandError(f"Cannot restart service from state {row.get('State')!r}")
    for method, target in (("StopService", "Stopped"), ("StartService", "Running")):
        if row["State"] == target:
            continue
        status = execution.method(CIMV2, path, method, {})
        if status:
            detail = (
                "; dependent services may be running; no force stop attempted"
                if method == "StopService" and status == 3 else ""
            )
            raise WmiCommandError(f"{method} failed: ReturnValue={status}{detail}")
        while True:
            row = _service_row(execution, spec)
            if row.get("State") == target:
                break
            if not isinstance(row.get("State"), str) or not row["State"]:
                raise WmiCommandError("Service query omitted State while polling")
            time.sleep(min(0.25, execution.remaining()))
    return [row]


def _date(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"([0-9]{14})\.([0-9]{6})([+-])([0-9]{3})", value)
    if match is None:
        return None
    try:
        offset = int(match[4]) * (1 if match[3] == "+" else -1)
        return datetime.strptime(match[1] + match[2], "%Y%m%d%H%M%S%f").replace(
            tzinfo=timezone(timedelta(minutes=offset)),
        )
    except ValueError:
        return None


def _uint(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFFFFFFFFFF:
        raise WmiCommandError(f"Expected unsigned integer provider value, got {value!r}")
    return value


def _transform(alias: str, rows: list[dict], values: dict[str, object]) -> list[dict]:
    if alias == "disk":
        result = []
        for row in rows:
            free, size = _uint(row.get("FreeSpace")), _uint(row.get("Size"))
            result.append({
                "DeviceID": row.get("DeviceID"), "VolumeName": row.get("VolumeName"),
                "FreeGB": round(free / 1024**3, 2) if free is not None else None,
                "SizeGB": round(size / 1024**3, 2) if size is not None else None,
            })
        return result
    if alias in {"procs", "topcpu"}:
        result = []
        for row in rows:
            kernel, user = _uint(row.get("KernelModeTime")), _uint(row.get("UserModeTime"))
            result.append({
                "Name" if alias == "topcpu" else "ProcessName": row.get("Name"),
                "Id": row.get("ProcessId"),
                "CPU": (kernel + user) / 10000000 if kernel is not None and user is not None
                else None,
                "WorkingSet64": _uint(row.get("WorkingSetSize")), "Path": row.get("ExecutablePath"),
            })
        if alias == "topcpu":
            result.sort(key=lambda row: (row["CPU"] is not None, row["CPU"] or 0), reverse=True)
            return result[:values["count"]]
        return sorted(result, key=lambda row: str(row["ProcessName"] or "").casefold())
    if alias == "uptime":
        for row in rows:
            boot, now = _date(row.get("LastBootUpTime")), _date(row.get("LocalDateTime"))
            row["Uptime"] = str(now - boot) if boot and now and now >= boot else None
    elif alias == "services":
        rows.sort(key=lambda row: (str(row.get("State") or ""), str(row.get("Name") or "")))
    elif alias == "ports":
        rows.sort(key=lambda row: _uint(row.get("LocalPort")) or 0)
    elif alias == "svc" and not rows:
        raise WmiCommandError("Service was not found")
    return rows


def execute_command(alias: str, values: Mapping[str, object], query_callback: QueryCallback,
                    method_callback: MethodCallback, timeout_seconds: float) -> tuple[list[dict], str]:
    """Run one built-in with a shared deadline and return records plus an action trace.

    Callback exceptions propagate unchanged. Method errors raise WmiCommandError;
    deadline exhaustion raises TimeoutError. Neither methods nor queries are
    retried on failure. Only read-only service-state polling repeats a query.
    """
    parameters = _values(alias, values)
    execution = _Execution(query_callback, method_callback, timeout_seconds)
    if alias == "dir":
        rows = _directory(execution, parameters)
    elif alias == "admins":
        rows = _admins(execution)
    elif alias == "pending-reboot":
        rows = _pending_reboot(execution)
    elif alias == "restart-service":
        rows = _restart_service(execution, parameters["service_name"])
    elif alias == "events":
        rows = _events(execution, parameters)
    else:
        rows = _transform(alias, execution.query(make_query(alias, parameters)), parameters)
    execution.remaining()
    return rows, "\n".join(execution.actions)
