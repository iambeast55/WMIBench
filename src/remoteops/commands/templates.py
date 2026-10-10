from __future__ import annotations

import json
import re
import shlex
import os
import tempfile
import math
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from remoteops.core.models import CommandTemplate
from remoteops.commands.wmi_catalog import WMI_ALIASES, WMI_NOTES
from remoteops.commands.wmi_definitions import WmiQueryPlan, compile_definition, validate_definition


BUILTIN_TEMPLATES: tuple[CommandTemplate, ...] = (
    CommandTemplate(
        name="System summary",
        alias="sysinfo",
        description="Show hostname, OS caption, version, uptime, and logged-on user.",
        script=(
            "$os = Get-CimInstance Win32_OperatingSystem\n"
            "[pscustomobject]@{\n"
            "  ComputerName = $env:COMPUTERNAME\n"
            "  Caption = $os.Caption\n"
            "  Version = $os.Version\n"
            "  LastBootUpTime = $os.LastBootUpTime\n"
            "  UserName = $os.RegisteredUser\n"
            "}"
        ),
    ),
    CommandTemplate(
        name="Disk free space",
        alias="disk",
        description="List fixed disks with free space and size in GB.",
        script=(
            "Get-CimInstance Win32_LogicalDisk -Filter \"DriveType=3\" | "
            "Select-Object DeviceID, VolumeName, "
            "@{Name='FreeGB';Expression={[math]::Round($_.FreeSpace / 1GB, 2)}}, "
            "@{Name='SizeGB';Expression={[math]::Round($_.Size / 1GB, 2)}}"
        ),
    ),
    CommandTemplate(
        name="List services",
        alias="services",
        description="List services with status, name, and display name.",
        script="Get-Service | Sort-Object Status, Name | Select-Object Status, Name, DisplayName",
    ),
    CommandTemplate(
        name="Restart service",
        alias="restart-service",
        description="Restart a named service and return its final status.",
        parameters={"service_name": "Spooler", "_positional": ["service_name"]},
        script=(
            "$name = '$service_name'\n"
            "Restart-Service -Name $name -Force\n"
            "Get-Service -Name $name | Select-Object Status, Name, DisplayName"
        ),
    ),
    CommandTemplate(
        name="Local administrators",
        alias="admins",
        description="List members of the local Administrators group.",
        script=(
            "$adminGroup = Get-LocalGroup | Where-Object { $_.SID -eq 'S-1-5-32-544' }\n"
            "if (-not $adminGroup) { throw 'Built-in Administrators group was not found by SID S-1-5-32-544.' }\n"
            "Get-LocalGroupMember -Group $adminGroup.Name | Select-Object Name, ObjectClass, PrincipalSource"
        ),
    ),
    CommandTemplate(
        name="Pending reboot signals",
        alias="pending-reboot",
        description="Check common registry signals for pending reboot state.",
        script=(
            "$paths = @(\n"
            "  'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Component Based Servicing\\RebootPending',\n"
            "  'HKLM:\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\WindowsUpdate\\Auto Update\\RebootRequired'\n"
            ")\n"
            "$paths | ForEach-Object { [pscustomobject]@{ Path = $_; Exists = Test-Path $_ } }"
        ),
    ),
    CommandTemplate(
        name="Top processes by CPU",
        alias="topcpu",
        description="Show the top processes sorted by cumulative CPU time.",
        parameters={"count": "15", "_positional": ["count"]},
        script=(
            "$count = [int]'$count'\n"
            "Get-Process | Sort-Object CPU -Descending | Select-Object -First $count "
            "Name, Id, CPU, WorkingSet64, Path"
        ),
    ),
    CommandTemplate(
        name="Running processes",
        alias="procs",
        description="List running processes with process id, CPU, memory, and path.",
        script=(
            "Get-Process | Sort-Object ProcessName | "
            "Select-Object ProcessName, Id, CPU, WorkingSet64, Path"
        ),
    ),
    CommandTemplate(
        name="Recent event log entries",
        alias="events",
        description="Read recent events from a selected Windows event log.",
        parameters={"log": "System", "count": "50", "_positional": ["log", "count"]},
        script=(
            "$log = '$log'\n"
            "$count = [int]'$count'\n"
            "Get-WinEvent -LogName $log -MaxEvents $count | "
            "Select-Object TimeCreated, Id, ProviderName, LevelDisplayName, Message"
        ),
    ),
    CommandTemplate(
        name="Installed hotfixes",
        alias="hotfixes",
        description="List installed Windows hotfixes and installation dates.",
        script="Get-HotFix | Sort-Object InstalledOn -Descending | Select-Object HotFixID, Description, InstalledBy, InstalledOn",
    ),
    CommandTemplate(
        name="Network configuration",
        alias="net",
        description="Show IP addresses, interfaces, DNS servers, and default routes.",
        script=(
            "Get-NetIPConfiguration | Select-Object InterfaceAlias, InterfaceDescription, "
            "IPv4Address, IPv6Address, DNSServer, IPv4DefaultGateway"
        ),
    ),
    CommandTemplate(
        name="Listening TCP ports",
        alias="ports",
        description="List listening TCP ports with owning process id.",
        script=(
            "Get-NetTCPConnection -State Listen | Sort-Object LocalPort | "
            "Select-Object LocalAddress, LocalPort, OwningProcess, CreationTime"
        ),
    ),
    CommandTemplate(
        name="SMB shares",
        alias="shares",
        description="List local SMB shares and paths.",
        script="Get-SmbShare | Select-Object Name, Path, Description, ShareState, FolderEnumerationMode",
    ),
    CommandTemplate(
        name="Local users",
        alias="users",
        description="List local user accounts and enabled/last logon state.",
        script="Get-LocalUser | Select-Object Name, Enabled, LastLogon, PasswordLastSet, Description",
    ),
    CommandTemplate(
        name="Local groups",
        alias="groups",
        description="List local groups.",
        script="Get-LocalGroup | Select-Object Name, SID, Description",
    ),
    CommandTemplate(
        name="Firewall profiles",
        alias="firewall",
        description="Show Windows Firewall profile state.",
        script="Get-NetFirewallProfile | Select-Object Name, Enabled, DefaultInboundAction, DefaultOutboundAction, NotifyOnListen",
    ),
    CommandTemplate(
        name="Scheduled tasks",
        alias="tasks",
        description="List scheduled tasks with state and task path.",
        script="Get-ScheduledTask | Select-Object TaskName, TaskPath, State, Author",
    ),
    CommandTemplate(
        name="Startup commands",
        alias="startup",
        description="List startup commands from CIM.",
        script="Get-CimInstance Win32_StartupCommand | Select-Object Name, Command, Location, User",
    ),
    CommandTemplate(
        name="Logged on sessions",
        alias="sessions",
        description="Show interactive logon sessions from quser when available.",
        script="quser 2>&1",
    ),
    CommandTemplate(
        name="Service status by name",
        alias="svc",
        description="Query a service by name.",
        parameters={"service_name": "Spooler", "_positional": ["service_name"]},
        script="Get-Service -Name '$service_name' | Select-Object Status, Name, DisplayName, ServiceType, StartType",
    ),
    CommandTemplate(
        name="Computer uptime",
        alias="uptime",
        description="Show boot time and uptime.",
        script=(
            "$os = Get-CimInstance Win32_OperatingSystem\n"
            "$uptime = (Get-Date) - $os.LastBootUpTime\n"
            "[pscustomobject]@{ ComputerName=$env:COMPUTERNAME; LastBootUpTime=$os.LastBootUpTime; Uptime=$uptime.ToString() }"
        ),
    ),
)

TEMPLATES_BY_ALIAS = {template.alias: template for template in BUILTIN_TEMPLATES if template.alias}

CATEGORIES = {
    "System": {"sysinfo", "disk", "uptime", "hotfixes", "pending-reboot", "startup"},
    "Services": {"services", "svc", "restart-service"},
    "Processes": {"procs", "topcpu", "tasks"},
    "Network": {"net", "ports", "shares", "firewall"},
    "Accounts": {"admins", "users", "groups", "sessions"},
    "Events": {"events"},
}


def command_category(template):
    if template.category.strip():
        return template.category.strip()
    return next((name for name, aliases in CATEGORIES.items() if template.alias in aliases), "Custom")


def parse_json(content):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    def constant(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(content, object_pairs_hook=pairs, parse_constant=constant)


def render_template(template: CommandTemplate, values: dict[str, str]) -> str:
    """Render a built-in template using conservative string substitution."""

    def replace(match):
        key = match[2]
        if key not in values or not isinstance(values[key], (str, int, float, bool)):
            return match[0]
        return "'" + _escape_powershell_single_quoted(str(values[key])) + "'"
    return re.sub(r"(['\"])\$([A-Za-z][A-Za-z0-9_]*)\1", replace, template.script)


class CommandRegistry:
    """Runtime command library backed by an editable JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.templates: list[CommandTemplate] = []
        self.load()

    def load(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._commit(deepcopy(list(BUILTIN_TEMPLATES)))
            return

        original = self.path.read_bytes()
        version, candidates = self._parse(original)
        if version == 1:
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if backup.exists():
                if backup.read_bytes() != original:
                    raise CommandValidationError(f"Migration backup already contains different data: {backup}")
            else:
                with backup.open("xb") as stream:
                    stream.write(original)
            self._write(self.path, candidates)
        self.templates = candidates

    @staticmethod
    def _parse(content):
        data = parse_json(content)
        if not isinstance(data, dict) or type(data.get("version", 1)) is not int or data.get("version", 1) not in (1, 2):
            raise CommandValidationError("Unsupported command library version; expected 1 or 2")
        if set(data) - {"version", "commands"} or not isinstance(data.get("commands"), list):
            raise CommandValidationError("Command file must contain a commands array")
        version = data.get("version", 1)
        if not all(isinstance(item, dict) for item in data["commands"]):
            raise CommandValidationError("Commands must be objects")
        if version == 1 and any(item.get("wmi") is not None for item in data["commands"]):
            raise CommandValidationError("WMI definitions require command library version 2")
        candidates = [_template_from_dict(item) for item in data["commands"]]
        CommandRegistry._validate_aliases(candidates)
        return version, candidates

    @staticmethod
    def _validate_aliases(templates) -> None:
        aliases = [t.alias.lower() for t in templates]
        if len(set(aliases)) != len(aliases) or any(not re.fullmatch(r"[a-z][a-z0-9-]*", a) for a in aliases):
            raise ValueError("Command aliases must be unique and contain letters, digits, or hyphens.")
        if len({t.id for t in templates}) != len(templates):
            raise CommandValidationError("Command IDs must be unique")
        for template in templates:
            validate_template(template)

    def save(self) -> None:
        self._commit(self.templates)

    @staticmethod
    def _write(path, templates):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 2,
            "commands": [_template_to_dict(template) for template in templates],
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def _commit(self, candidates):
        self._validate_aliases(candidates)
        self._write(self.path, candidates)
        self.templates = deepcopy(candidates)

    def reset_to_defaults(self) -> None:
        self._commit(deepcopy(list(BUILTIN_TEMPLATES)))

    def resolve(self, command_line: str) -> tuple[str, str]:
        return resolve_command(command_line, self.templates)

    def prepare(self, command_line: str) -> PreparedCommand:
        # Reload on dispatch so external library edits take effect without a restart.
        self.load()
        return prepare_command(command_line, self.templates)

    def aliases(self) -> dict[str, CommandTemplate]:
        return {template.alias.lower(): template for template in self.templates if template.alias}

    def upsert(self, template: CommandTemplate) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", template.alias.lower()):
            raise CommandValidationError("Alias must contain only letters, digits, or hyphens")
        if any(t.alias.lower() == template.alias.lower() and t.id != template.id for t in self.templates):
            raise CommandValidationError("This alias already exists")
        candidates = deepcopy(self.templates)
        for index, existing in enumerate(candidates):
            if existing.id == template.id:
                candidates[index] = deepcopy(template)
                self._commit(candidates)
                return
        candidates.append(deepcopy(template))
        self._commit(candidates)

    def delete(self, template_id: str) -> None:
        self._commit([template for template in self.templates if template.id != template_id])

    def import_file(self, path: Path):
        _, imported = self._parse(path.read_bytes())
        self._commit(self.templates + imported)

    def export_file(self, path: Path, templates=None):
        candidates = self.templates if templates is None else templates
        self._validate_aliases(candidates)
        if path.resolve() == self.path.resolve():
            raise CommandValidationError("Export to a separate file, not the active library")
        self._write(path, candidates)


def resolve_command(
    command_line: str, templates: list[CommandTemplate] | tuple[CommandTemplate, ...] | None = None
) -> tuple[str, str]:
    if not command_line.strip():
        return "", "empty"
    prepared = prepare_command(command_line, templates)
    return (prepared.help_text, "help") if prepared.help_text else (prepared.script, prepared.name)


class CommandValidationError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedCommand:
    command_line: str
    alias: str
    name: str
    script: str
    values: dict[str, object]
    help_text: str = ""
    native_wmi: bool = False
    wmi_plan: WmiQueryPlan | None = None
    read_only_modes: tuple[str, ...] = ()


def argument_schema(template: CommandTemplate) -> dict[str, dict]:
    if not isinstance(template.parameters, dict):
        raise CommandValidationError("Command parameters must be an object")
    explicit = template.parameters.get("_schema", {})
    if not isinstance(explicit, dict):
        raise CommandValidationError("Argument _schema must be an object")
    schema = {}
    for key, value in template.parameters.items():
        if key.startswith("_"):
            continue
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key):
            raise CommandValidationError(f"Invalid parameter name: {key}")
        schema[key] = {"type": "bool" if isinstance(value, bool) else "int" if key == "count" or isinstance(value, int)
                       else "float" if isinstance(value, float) else "str", "default": value}
        if key == "count":
            schema[key].update(min=1, max=10000, description="Maximum number of records")
        if key == "service_name":
            schema[key]["description"] = "Service name"
        if key == "log":
            schema[key]["description"] = "Windows event log name"
    for key, spec in explicit.items():
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", key) or not isinstance(spec, dict):
            raise CommandValidationError("Invalid argument schema")
        schema[key] = {**schema.get(key, {}), **spec}
    return schema


def _argument_value(value, spec):
    kind = spec.get("type", "str")
    if kind == "int":
        if type(value) not in (int, str):
            raise ValueError("expected an integer")
        value = int(value)
        if not spec.get("min", -2**63) <= value <= spec.get("max", 2**63 - 1):
            raise ValueError("out of range")
    elif kind == "bool":
        normalized = str(value).lower()
        if normalized not in {"true", "false"}:
            raise ValueError("expected true or false")
        value = normalized == "true"
    elif kind == "float":
        if type(value) not in (int, float, str):
            raise ValueError("expected a number")
        value = float(value)
        if not math.isfinite(value) or not spec.get("min", -float("inf")) <= value <= spec.get("max", float("inf")):
            raise ValueError("out of range or non-finite")
    elif kind != "str" or not isinstance(value, str):
        raise ValueError("expected text or a supported argument type")
    if "choices" in spec and value not in spec["choices"]:
        raise ValueError("not an accepted value")
    return value


def validate_template(template):
    if not isinstance(template.category, str) or len(template.category) > 80:
        raise CommandValidationError("Command category must be text of at most 80 characters")
    if type(template.favorite) is not bool:
        raise CommandValidationError("Command favorite must be boolean")
    if not template.name.strip() or not re.fullmatch(r"[a-z][a-z0-9-]*", template.alias.lower()):
        raise CommandValidationError("Command name and valid alias are required")
    if not template.script.strip() and template.wmi is None:
        raise CommandValidationError("Provide a PowerShell script or a WMI query")
    schema = argument_schema(template)
    positional = template.parameters.get("_positional", [])
    if (not isinstance(positional, list) or any(not isinstance(name, str) or name not in schema for name in positional)
            or len(set(positional)) != len(positional)):
        raise CommandValidationError(f"Invalid positional arguments for {template.alias}")
    for name, spec in schema.items():
        if spec.get("type", "str") not in {"str", "int", "bool", "float"}:
            raise CommandValidationError(f"Unsupported argument type for {name}")
        if "choices" in spec and not isinstance(spec["choices"], list):
            raise CommandValidationError(f"Argument choices must be an array: {name}")
        if "required" in spec and type(spec["required"]) is not bool:
            raise CommandValidationError(f"Argument required must be boolean: {name}")
        for limit in ("min", "max"):
            if limit in spec and (type(spec[limit]) not in (int, float)
                                  or (type(spec[limit]) is float and not math.isfinite(spec[limit]))):
                raise CommandValidationError(f"Invalid argument bound: {name}.{limit}")
        if "min" in spec and "max" in spec and spec["min"] > spec["max"]:
            raise CommandValidationError(f"Argument minimum exceeds maximum: {name}")
        if "default" in spec:
            try:
                _argument_value(spec["default"], spec)
            except (ValueError, TypeError, OverflowError) as exc:
                raise CommandValidationError(f"Invalid default for {name}: {exc}") from exc
    if template.wmi is not None:
        validate_definition(template.wmi, schema)
        if all("default" in spec for spec in schema.values()):
            compile_definition(template.wmi, schema,
                               {name: _argument_value(spec["default"], spec) for name, spec in schema.items()})


def command_backends(template):
    result = ["WinRM"] if template.script.strip() else []
    builtin = TEMPLATES_BY_ALIAS.get(template.alias.lower())
    if template.wmi is not None or (builtin and builtin.script == template.script and template.alias.lower() in WMI_ALIASES):
        result.append("WMI/DCOM")
    return result


def command_read_only_modes(template):
    """Only audited builtin scripts and constrained WMI SELECT plans qualify."""
    builtin = TEMPLATES_BY_ALIAS.get(template.alias.lower())
    trusted = bool(builtin and builtin.script == template.script
                   and template.alias.lower() != "restart-service"
                   and not template.parameters.get("_modifies"))
    modes = ["winrm"] if trusted else []
    if template.wmi is not None or (trusted and template.alias.lower() in WMI_ALIASES):
        modes.append("wmi_dcom")
    return tuple(modes)


def command_help(template: CommandTemplate) -> str:
    schema = argument_schema(template)
    positional = template.parameters.get("_positional", list(schema) if template.alias == "restart-service" else [])
    usage = template.alias + "".join(f" [{name}]" for name in positional) + (" [options]" if schema else "")
    lines = [template.name, template.description, "", f"Usage: {usage}", "", "Options:", "  -h, --help    Show this help without connecting"]
    for name, spec in schema.items():
        details = f"{spec.get('description', name)}; type: {spec.get('type', 'str')}"
        if "default" in spec:
            details += f"; default: {spec['default']}"
        if spec.get("required"):
            details += "; required"
        if "choices" in spec:
            details += f"; choices: {', '.join(map(str, spec['choices']))}"
        if "min" in spec or "max" in spec:
            details += f"; range: {spec.get('min', '*')}..{spec.get('max', '*')}"
        lines.append(f"  --{name.replace('_', '-')} VALUE    {details}")
    builtin = TEMPLATES_BY_ALIAS.get(template.alias.lower())
    native = builtin is not None and builtin.script == template.script and template.alias in WMI_ALIASES
    lines += ["", "Backends: " + ", ".join(command_backends(template)),
              "Changes remote system: " + ("no (WMI query)" if template.wmi is not None and not template.script.strip()
                                           else "yes" if template.alias == "restart-service" or template.parameters.get("_modifies")
                                           else "no" if builtin else "unspecified")]
    if template.wmi is not None:
        definition = template.wmi
        lines.append("WMI clients: Windows native COM; Linux experimental DCOM (NTLM).")
        lines.append(f"WMI query: root/cimv2 / {definition['class']} (read-only SELECT)")
        lines.append("WMI fields: " + ", ".join(f"{name}: {kind}" for name, kind in definition["properties"].items()))
        lines.append(f"WMI limits: {definition.get('limits', {}).get('rows', 1000)} rows, "
                     f"{definition.get('limits', {}).get('timeout_seconds', 30)} seconds; no partial results.")
    elif native:
        lines.append("WMI clients: Windows native COM; Linux experimental DCOM (NTLM).")
        lines.append("WMI behavior: " + WMI_NOTES.get(template.alias, "Native provider fields."))
    default_example = template.alias + "".join(f" --{key.replace('_', '-')} {spec['default']}" for key, spec in schema.items() if "default" in spec)
    examples = template.parameters.get("_examples", [default_example])
    lines += ["", "Examples:", *[f"  {example}" for example in examples]]
    return "\n".join(lines)


def prepare_command(command_line: str, templates=None) -> PreparedCommand:
    stripped = command_line.strip()
    if not stripped or any(c in stripped for c in "\r\n\x00"):
        raise CommandValidationError("Enter one registered command.")

    try:
        lexer = shlex.shlex(stripped, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        lexer.escape = ""
        parts = list(lexer)
    except ValueError as exc:
        raise CommandValidationError(str(exc)) from exc

    alias = parts[0].lower() if parts else stripped.lower()
    template_map = {
        template.alias.lower(): template
        for template in (templates if templates is not None else BUILTIN_TEMPLATES)
        if template.alias
    }
    template = template_map.get(alias)
    if template is None:
        raise CommandValidationError(f"Unknown command '{alias}'. Only saved command-library aliases can run.")
    validate_template(template)
    if any(arg in {"-h", "--help"} for arg in parts[1:]):
        return PreparedCommand(stripped, alias, template.name, "", {}, command_help(template))
    schema = argument_schema(template)
    values = {name: spec["default"] for name, spec in schema.items() if "default" in spec}
    positional = template.parameters.get("_positional", list(schema) if alias == "restart-service" else [])
    assigned = set()
    position = 0
    index = 1
    while index < len(parts):
        part = parts[index]
        if "=" in part:
            name, value = part.split("=", 1)
            name = name.lstrip("-").replace("-", "_")
        elif part.startswith("-"):
            name = part.lstrip("-").replace("-", "_")
            index += 1
            if index == len(parts):
                raise CommandValidationError(f"Missing value for {part}")
            value = parts[index]
        else:
            if position >= len(positional):
                raise CommandValidationError(f"Unexpected argument '{part}'. Use {alias} -h.")
            name, value = positional[position], part
            position += 1
        if name not in schema or name in assigned:
            raise CommandValidationError(f"Unknown or repeated argument '{name}'. Use {alias} -h.")
        assigned.add(name)
        values[name] = value
        index += 1
    for name, spec in schema.items():
        if name not in values:
            if spec.get("required"):
                raise CommandValidationError(f"Missing required argument '{name}'")
            values[name] = ""
        try:
            values[name] = _argument_value(values[name], spec)
        except (ValueError, TypeError, OverflowError) as exc:
            raise CommandValidationError(f"Invalid {name}: {exc}. Use {alias} -h.") from exc
    builtin = TEMPLATES_BY_ALIAS.get(alias)
    native = bool(builtin and builtin.script == template.script and alias in WMI_ALIASES)
    plan = compile_definition(template.wmi, schema, values) if template.wmi is not None else None
    return PreparedCommand(stripped, alias, template.name, render_template(template, values), values,
                           native_wmi=native or plan is not None, wmi_plan=plan,
                           read_only_modes=command_read_only_modes(template))


def _template_to_dict(template: CommandTemplate) -> dict[str, object]:
    return {
        "id": template.id,
        "name": template.name,
        "alias": template.alias,
        "description": template.description,
        "script": template.script,
        "parameters": template.parameters,
        "wmi": template.wmi,
        "category": template.category,
        "favorite": template.favorite,
    }


def _template_from_dict(data: dict[str, object]) -> CommandTemplate:
    if set(data) - {"id", "name", "alias", "description", "script", "parameters", "wmi", "category", "favorite"}:
        raise CommandValidationError("Unknown command definition field")
    if any(not isinstance(data[key], str) for key in ("id", "name", "alias", "description", "script") if key in data):
        raise CommandValidationError("Command identity, description, and script must be strings")
    if not isinstance(data.get("parameters", {}), dict):
        raise CommandValidationError("Command parameters must be an object")
    template = CommandTemplate(
        name=str(data.get("name") or ""),
        alias=str(data.get("alias") or ""),
        description=str(data.get("description") or ""),
        script=str(data.get("script") or ""),
        parameters=dict(data.get("parameters") or {}),
        wmi=deepcopy(data.get("wmi")),
        category=data.get("category", ""),
        favorite=data.get("favorite", False),
    )
    if data.get("id"):
        template.id = str(data["id"])
    return template


def _escape_powershell_single_quoted(value: str) -> str:
    return value.replace("'", "''")
