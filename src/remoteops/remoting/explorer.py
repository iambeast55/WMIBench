"""WMI namespace and class inspection on an established host session."""
from __future__ import annotations

import json
import re
import time

from remoteops.commands.wmi_definitions import CIM_TYPES
from remoteops.core.models import ConnectionMode, SessionState
from remoteops.remoting.output import plain_value
from remoteops.remoting.transfer import ps_literal

MAX_ITEMS = 10000
MAX_BYTES = 16 * 1024 * 1024
TYPE_NAMES = {value: name for name, value in CIM_TYPES.items()}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", value):
        raise ValueError("Invalid WMI name.")
    return value


def request(action, args):
    if action not in {"namespaces", "classes", "describe", "instances"}:
        raise ValueError("Unknown explorer operation.")
    namespace = args.get("namespace", "root/cimv2")
    if not isinstance(namespace, str):
        raise ValueError("Enter a namespace under root.")
    namespace = namespace.replace("\\", "/").strip("/")
    if len(namespace) > 256 or namespace.split("/")[0].casefold() != "root":
        raise ValueError("Enter a namespace under root.")
    for part in namespace.split("/"):
        identifier(part)
    result = {"namespace": namespace}
    if action in {"describe", "instances"}:
        result["class_name"] = identifier(args.get("class_name"))
    if action == "instances":
        properties = args.get("properties")
        if not isinstance(properties, list) or not 1 <= len(properties) <= 64:
            raise ValueError("Select between 1 and 64 properties.")
        result["properties"] = list(dict.fromkeys(identifier(name) for name in properties))
        limit = args.get("limit", 100)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Row limit must be between 1 and 1,000.")
        result["limit"] = limit
    return result


def checked_rows(rows):
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Invalid explorer response.")
    if len(rows) > MAX_ITEMS or len(json.dumps(rows).encode("utf-8")) > MAX_BYTES:
        raise ValueError("Explorer result exceeds its size limit.")
    return rows


def native_inspect(client, action, args, timeout, cancel=None):
    from remoteops.remoting.wmi import com_timeout
    deadline = time.monotonic() + timeout
    rows = []
    size = 0

    def add(row):
        nonlocal size
        if cancel is not None and cancel.is_set():
            raise ValueError("Explorer request cancelled.")
        if time.monotonic() >= deadline:
            raise TimeoutError("Explorer request timed out.")
        size += len(json.dumps(row).encode("utf-8"))
        if len(rows) >= MAX_ITEMS or size > MAX_BYTES:
            raise ValueError("Explorer result exceeds its size limit.")
        rows.append(row)

    def qualifiers(obj):
        return {q.Name: plain_value(q.Value) for q in obj.Qualifiers_}

    def property_row(prop, kind="Property", method="", direction=""):
        q = qualifiers(prop)
        return {"Kind": kind, "Name": prop.Name, "Type": TYPE_NAMES.get(int(prop.CIMType), "object")
                + ("[]" if prop.IsArray else ""), "Method": method, "Direction": direction,
                "Key": bool(q.get("key", q.get("Key", False))), "Qualifiers": q}

    with com_timeout(timeout):
        if action == "namespaces":
            return client.query("SELECT Name FROM __NAMESPACE", timeout, MAX_ITEMS)
        if action == "instances":
            wql = "SELECT " + ", ".join(args["properties"]) + " FROM " + args["class_name"]
            return checked_rows(client.query(wql, timeout, args["limit"], take=args["limit"]))
        if action == "classes":
            for obj in client.services.SubclassesOf("", 16):
                add({"Name": str(obj.Path_.Class)})
        else:
            obj = client.services.Get(args["class_name"], 0x20000)
            add({"Kind": "Class", "Name": str(obj.Path_.Class), "Qualifiers": qualifiers(obj)})
            for prop in obj.Properties_:
                add(property_row(prop))
            for method in obj.Methods_:
                q = qualifiers(method)
                add({"Kind": "Method", "Name": method.Name, "Static": bool(q.get("Static", q.get("static", False))),
                     "Qualifiers": q})
                for direction, parameters in (("In", method.InParameters), ("Out", method.OutParameters)):
                    if parameters is not None:
                        for prop in parameters.Properties_:
                            add(property_row(prop, "Parameter", method.Name, direction))
    return rows


def powershell_script(action, args):
    namespace = ps_literal(args["namespace"])
    head = "$ErrorActionPreference='Stop'; $rows = @(\n"
    if action == "namespaces":
        body = f"Get-CimInstance -Namespace {namespace} -ClassName __NAMESPACE | Select-Object -First 10001 -Property Name"
    elif action == "classes":
        body = f"Get-CimClass -Namespace {namespace} | Select-Object -First 10001 @{{n='Name';e={{$_.CimClassName}}}}"
    elif action == "instances":
        body = (f"Get-CimInstance -Namespace {namespace} -ClassName {args['class_name']} "
                f"-Property {','.join(args['properties'])} | Select-Object -First {args['limit']} "
                f"-Property {','.join(args['properties'])}")
    else:
        # Emit plain records before PSRP serialization; CIM metadata has cyclic references.
        head = """$ErrorActionPreference='Stop'
function Qualifiers($items) {
    $q = @{}; foreach ($item in $items) { $q[$item.Name] = $item.Value }; return $q
}
function TypeName($kind) {
    $name = ([string]$kind).ToLowerInvariant().Replace('array','[]')
    if ($name -eq 'instance') { return 'object' }
    if ($name -eq 'instance[]') { return 'object[]' }
    return $name
}
$rows = @(\n"""
        body = f"$c = Get-CimClass -Namespace {namespace} -ClassName {args['class_name']}\n" + """
[pscustomobject]@{Kind='Class'; Name=$c.CimClassName; Qualifiers=(Qualifiers $c.CimClassQualifiers)}
foreach ($p in $c.CimClassProperties) {
    $q = Qualifiers $p.Qualifiers
    [pscustomobject]@{Kind='Property'; Name=$p.Name; Type=(TypeName $p.CimType); Key=[bool]$q['Key']; Qualifiers=$q}
}
foreach ($m in $c.CimClassMethods) {
    $q = Qualifiers $m.Qualifiers
    [pscustomobject]@{Kind='Method'; Name=$m.Name; Static=[bool]$q['Static']; Qualifiers=$q}
    foreach ($p in $m.Parameters) {
        $pq = Qualifiers $p.Qualifiers
        foreach ($direction in @('In','Out')) {
            if ($pq[$direction]) {
                [pscustomobject]@{Kind='Parameter'; Name=$p.Name; Type=(TypeName $p.CimType); Method=$m.Name; Direction=$direction; Qualifiers=$pq}
            }
        }
    }
    if ([string]$m.ReturnType -ne 'Unknown') {
        [pscustomobject]@{Kind='Parameter'; Name='ReturnValue'; Type=(TypeName $m.ReturnType); Method=$m.Name; Direction='Out'; Qualifiers=@{}}
    }
}
"""
    return head + body + """
)
if ($rows.Count -gt 10000) { throw 'Explorer result exceeds 10,000 items.' }
$json = ConvertTo-Json -InputObject @($rows) -Depth 12 -Compress
if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 16777216) { throw 'Explorer result exceeds 16 MiB.' }
[pscustomobject]@{ExplorerJson=$json}
"""


def perform(session, action, args, cancel=None):
    args = request(action, args)
    if cancel is not None and cancel.is_set():
        raise ValueError("Explorer request cancelled.")
    if session.host.connection_mode is ConnectionMode.WINRM:
        from remoteops.remoting.session import PsrpSession
        script = powershell_script(action, args)
        result = (session.run(script, 60, cancel=cancel) if isinstance(session, PsrpSession)
                  else session.run(script, 60))
        if not result.succeeded:
            raise ValueError(result.stderr or "Explorer request failed.")
        if len(result.records) != 1 or not isinstance(result.records[0].get("ExplorerJson"), str):
            raise ValueError("Explorer response is missing.")
        payload = result.records[0]["ExplorerJson"]
        if len(payload.encode("utf-8")) > MAX_BYTES:
            raise ValueError("Explorer result exceeds 16 MiB.")
        return checked_rows(json.loads(payload))
    from remoteops.remoting.wmi import WmiSession
    if not isinstance(session, WmiSession):
        if action == "namespaces":
            from remoteops.remoting.tools import context_for_session
            return context_for_session(session, cancel, []).query(
                namespace=args["namespace"], class_name="__NAMESPACE", properties={"Name": "string"}, rows=10000)
        raise ValueError("Class inspection is not available with this DCOM transport. Use WinRM or native Windows WMI.")
    deadline = time.monotonic() + 60
    session.state = SessionState.RUNNING
    try:
        client = session._namespace_client(args["namespace"], 60)
        try:
            return native_inspect(client, action, args, max(0.01, deadline - time.monotonic()), cancel)
        except Exception as exc:
            session._operation_error(client, exc)
    finally:
        if session.state is SessionState.RUNNING:
            session.state = SessionState.CONNECTED


def query_snippet(namespace, class_name, properties, limit=100):
    types = {row["Name"]: row["Type"] for row in properties if row.get("Type", "").removesuffix("[]") in CIM_TYPES}
    if not types:
        return "# No properties supported by ctx.query."
    types = dict(list(types.items())[:64])
    entries = "\n".join(f"            {name!r}: {kind!r}," for name, kind in types.items())
    return ("def run(ctx, args):\n    return ctx.query(\n"
            f"        namespace={namespace!r},\n        class_name={class_name!r},\n"
            f"        properties={{\n{entries}\n        }},\n        rows={limit}, take={limit},\n    )\n")


def method_snippet(namespace, class_name, method, parameters):
    selected = [row for row in parameters if row.get("Method") == method["Name"]]
    if any(row["Type"].removesuffix("[]") not in CIM_TYPES for row in selected):
        return "# This method uses types not supported by ctx.invoke."
    inputs = [f"            {p['Name']!r}: ({p['Type']!r}, args[{p['Name']!r}])," for p in selected if p["Direction"] == "In"]
    outputs = {p["Name"]: p["Type"] for p in selected if p["Direction"] == "Out"}
    target = repr(class_name) if method.get("Static") else 'args["object_path"]'
    return ("# Set modifies_remote to true in command.json.\ndef run(ctx, args):\n"
            f"    result = ctx.invoke(\n        {target}, {method['Name']!r},\n"
            f"        namespace={namespace!r},\n        inputs={{\n" + "\n".join(inputs) + "\n        },\n"
            f"        outputs={outputs!r},\n    )\n"
            + ("    ctx.require_success(result)\n" if "ReturnValue" in outputs else "")
            + "    return [result]\n")
