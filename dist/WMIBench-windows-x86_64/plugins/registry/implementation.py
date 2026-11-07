import json
import re

from remoteops.commands.plugin_api import PluginError

HIVES = {"HKCR": 0x80000000, "HKLM": 0x80000002, "HKU": 0x80000003, "HKCC": 0x80000005}
TYPES = {
    "SZ": (1, "StringValue", "sValue", "string"),
    "EXPAND_SZ": (2, "ExpandedStringValue", "sValue", "string"),
    "BINARY": (3, "BinaryValue", "uValue", "uint8[]"),
    "DWORD": (4, "DWORDValue", "uValue", "uint32"),
    "MULTI_SZ": (7, "MultiStringValue", "sValue", "string[]"),
    "QWORD": (11, "QWORDValue", "uValue", "uint64"),
}


def parse_data(kind, data):
    if not isinstance(data, str) or "\0" in data:
        raise PluginError("Data must be NUL-free text; use hex for binary data.")
    if kind in ("DWORD", "QWORD"):
        text = data.strip()
        if not re.fullmatch(r"(?:0[xX][0-9a-fA-F]+|[0-9]+)", text):
            raise PluginError(f"{kind} requires an unsigned decimal or 0x-prefixed integer.")
        value = int(text, 16 if text.lower().startswith("0x") else 10)
        if value >= 2 ** (32 if kind == "DWORD" else 64):
            raise PluginError(f"Data exceeds the {kind} range.")
        return value
    if kind == "BINARY":
        try:
            result = list(bytes.fromhex(data))
        except ValueError:
            raise PluginError("BINARY data requires pairs of hex digits, optionally separated by spaces.") from None
        if len(result) > 1024:
            raise PluginError("BINARY input is limited to 1024 bytes by the plugin API.")
        return result
    if kind == "MULTI_SZ":
        try:
            result = json.loads(data)
        except ValueError:
            raise PluginError('MULTI_SZ data must be a JSON array of strings, e.g. ["one","two"].') from None
        if (not isinstance(result, list) or len(result) > 1024
                or any(not isinstance(item, str) or "\0" in item for item in result)):
            raise PluginError("MULTI_SZ requires at most 1024 NUL-free strings.")
        return result
    return data


def call(ctx, method, inputs, extra_outputs=None):
    result = ctx.invoke("StdRegProv", method, namespace="root/default", inputs=inputs,
                        outputs={"ReturnValue": "uint32", **(extra_outputs or {})})
    ctx.require_success(result, message=method)
    return result


def run(ctx, args):
    action = args.get("action")
    if action not in ("keys", "values", "get", "create-key", "set", "delete-key", "delete-value"):
        raise PluginError("Unknown registry action.")
    hive = args.get("hive", "HKLM")
    if hive not in HIVES:
        raise PluginError("Use HKLM, HKU, HKCR or HKCC. Remote HKCU is not the logged-on user's hive.")
    subkey = args.get("subkey", "")
    if not isinstance(subkey, str) or any(char in subkey for char in ("\0", "/", ":", "\r", "\n")):
        raise PluginError("Subkey must be a hive-relative registry path with backslash separators.")
    parts = subkey.split("\\") if subkey else []
    if any(not part.strip() or part in (".", "..") for part in parts) or (parts and (
            parts[0].upper().startswith("HKEY_") or parts[0].upper() in {*HIVES, "HKCU"})):
        raise PluginError("Supply a relative subkey, without a hive prefix or empty path segments.")
    if not subkey and action not in ("keys", "values"):
        raise PluginError("Root-hive reads/writes/deletion are not allowed by this plugin; provide a subkey.")
    value_action = action in ("get", "set", "delete-value")
    default = args.get("default_value", False)
    if type(default) is not bool:
        raise PluginError("default_value must be true or false.")
    if not value_action and ("name" in args or default or "type" in args or "data" in args):
        raise PluginError("Value options do not apply to a key/enumeration action.")
    if action != "set" and "data" in args:
        raise PluginError("Data is only accepted for set.")
    if action == "delete-value" and "type" in args:
        raise PluginError("delete-value does not accept a type.")
    name = None
    if value_action:
        if default:
            if "name" in args:
                raise PluginError("Choose either name or default_value, not both.")
            name = ""
        else:
            name = args.get("name")
            if not isinstance(name, str) or not name:
                raise PluginError("Provide a value name or explicitly set default_value to true.")
        if "\0" in name:
            raise PluginError("Value names cannot contain NUL.")
    kind = args.get("type")
    if action in ("get", "set") and kind not in TYPES:
        raise PluginError("get/set requires an explicit supported type; use values to inspect stored types.")
    if action == "set" and "data" not in args:
        raise PluginError("set requires data, which may be an empty string.")
    data = parse_data(kind, args["data"]) if action == "set" else None
    inputs = {"hDefKey": ("uint32", HIVES[hive]), "sSubKeyName": ("string", subkey)}
    identity = {"Hive": hive, "Subkey": subkey}
    if action == "keys":
        result = call(ctx, "EnumKey", inputs, {"sNames": "string[]"})
        return [{**identity, "Key": key} for key in (result["sNames"] or [])]
    if action == "values":
        result = call(ctx, "EnumValues", inputs, {"sNames": "string[]", "Types": "sint32[]"})
        names, types = result["sNames"] or [], result["Types"] or []
        if len(names) != len(types):
            raise PluginError("EnumValues returned mismatched name/type arrays; no partial results.")
        labels = {spec[0]: "REG_" + label for label, spec in TYPES.items()}
        return [{**identity, "Name": name, "Type": labels.get(code, "Unknown"), "TypeCode": code}
                for name, code in zip(names, types)]
    if action == "delete-key":
        children = call(ctx, "EnumKey", inputs, {"sNames": "string[]"})["sNames"]
        if children:
            raise PluginError("Key contains subkeys. Recursive deletion is not supported; no deletion was sent.")
    if value_action:
        inputs["sValueName"] = ("string", name)
        identity["Name"] = name
    if action in ("get", "set"):
        _, suffix, field, cim_type = TYPES[kind]
        if action == "set":
            inputs[field] = (cim_type, data)
        result = call(ctx, ("Get" if action == "get" else "Set") + suffix, inputs,
                      {field: cim_type} if action == "get" else None)
        identity["Type"] = "REG_" + kind
        if action == "get":
            identity["Data"] = result[field]
    else:
        result = call(ctx, {"create-key": "CreateKey", "delete-key": "DeleteKey", "delete-value": "DeleteValue"}[action], inputs)
    return [{**identity, "Action": action, "ReturnValue": result["ReturnValue"]}]
