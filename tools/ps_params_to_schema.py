"""Convert a PowerShell param() block into a WMIBench parameters object.

This is intentionally a small, offline converter. It does not execute the
PowerShell file. The parser handles the common parameter declaration forms:

    [Parameter(Mandatory = $true, Position = 0)]
    [ValidateSet('Fast', 'Safe')]
    [string]$Mode = 'Safe'

WMIBench command schemas support scalar str/int/float/bool values. PowerShell
arrays and rich objects are therefore emitted as strings with a warning.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_TYPE_MAP = {
    "string": "str",
    "str": "str",
    "int": "int",
    "int16": "int",
    "int32": "int",
    "int64": "int",
    "long": "int",
    "uint": "int",
    "uint16": "int",
    "uint32": "int",
    "uint64": "int",
    "short": "int",
    "byte": "int",
    "sbyte": "int",
    "float": "float",
    "single": "float",
    "double": "float",
    "decimal": "float",
    "bool": "bool",
    "boolean": "bool",
    "switch": "bool",
}


def _matching(text: str, start: int, opening: str = "(", closing: str = ")") -> int:
    """Return the matching delimiter, ignoring strings and PowerShell comments."""
    depth = 0
    quote = None
    comment = False
    index = start
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if comment:
            if char in "\r\n":
                comment = False
        elif quote:
            if quote == "'" and char == "'" and next_char == "'":
                index += 1
            elif quote == '"' and char == '`':
                index += 1
            elif char == quote:
                quote = None
        elif char == "#":
            comment = True
        elif char in "'\"":
            quote = char
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    raise ValueError("Unclosed param() block")


def _split_top_level(text: str) -> list[str]:
    parts = []
    start = 0
    depth = 0
    quote = None
    comment = False
    index = 0
    while index < len(text):
        char = text[index]
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if comment:
            if char in "\r\n":
                comment = False
        elif quote:
            if quote == "'" and char == "'" and next_char == "'":
                index += 1
            elif quote == '"' and char == '`':
                index += 1
            elif char == quote:
                quote = None
        elif char == "#":
            comment = True
        elif char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            if text[start:index].strip():
                parts.append(text[start:index].strip())
            start = index + 1
        index += 1
    if text[start:].strip():
        parts.append(text[start:].strip())
    return parts


def _attributes(declaration: str) -> tuple[list[str], str]:
    attributes = []
    index = 0
    while True:
        while index < len(declaration) and declaration[index].isspace():
            index += 1
        if index >= len(declaration) or declaration[index] != "[":
            break
        end = _matching(declaration, index, "[", "]")
        attributes.append(declaration[index + 1:end].strip())
        index = end + 1
    return attributes, declaration[index:].strip()


def _attribute_value(attribute: str, key: str) -> str | None:
    match = re.search(rf"\b{re.escape(key)}\s*=\s*([^,\)]+)", attribute, re.I)
    return match.group(1).strip() if match else None


def _literal(value: str):
    value = value.strip()
    if value.casefold() == "$true":
        return True
    if value.casefold() == "$false":
        return False
    if value.casefold() in {"$null", "$()"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        if value[0] == "'":
            return value[1:-1].replace("''", "'")
        return value[1:-1].replace('`"', '"').replace("``", "`")
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        return float(value)
    return None


def _choices(attribute: str) -> list:
    match = re.search(r"ValidateSet\s*\((.*)\)", attribute, re.I | re.S)
    if not match:
        return []
    values = []
    for item in _split_top_level(match.group(1)):
        literal = _literal(item)
        values.append(item.strip() if literal is None else literal)
    return values


def _convert(script: str) -> tuple[dict, list[str]]:
    match = re.search(r"\bparam\s*\(", script, re.I)
    if not match:
        raise ValueError("No param() block was found")
    end = _matching(script, match.end() - 1)
    declarations = _split_top_level(script[match.end():end])
    schema = {}
    positions = {}
    warnings = []

    for declaration in declarations:
        attributes, body = _attributes(declaration)
        variable = re.match(r"\$(\w+)(?:\s*=\s*(.*))?$", body, re.S)
        if not variable:
            warnings.append(f"Skipped parameter declaration: {body[:80]}")
            continue
        name, raw_default = variable.group(1), variable.group(2)
        if not _NAME.fullmatch(name):
            warnings.append(f"Skipped unsupported parameter name: {name}")
            continue

        type_name = "str"
        for attribute in attributes:
            if "(" not in attribute:
                mapped = _TYPE_MAP.get(attribute.strip().casefold())
                if mapped:
                    type_name = mapped
                elif attribute.strip().casefold().endswith("[]"):
                    warnings.append(f"{name}: array type emitted as str")
                continue
            base = attribute.split("(", 1)[0].strip().casefold()
            if base in _TYPE_MAP:
                type_name = _TYPE_MAP[base]

        spec = {"type": type_name}
        if type_name == "bool" and any(a.strip().casefold() == "switch" for a in attributes):
            spec["default"] = False

        for attribute in attributes:
            if attribute.casefold().startswith("parameter"):
                mandatory = _attribute_value(attribute, "Mandatory")
                if mandatory and mandatory.casefold() == "$true":
                    spec["required"] = True
                position = _attribute_value(attribute, "Position")
                if position and re.fullmatch(r"\d+", position):
                    positions[name] = int(position)
            if attribute.casefold().startswith("validateset"):
                choices = _choices(attribute)
                if choices:
                    spec["choices"] = choices
            if attribute.casefold().startswith("validatepattern"):
                warnings.append(f"{name}: ValidatePattern is not supported by WMIBench")
            if attribute.casefold().startswith("validaterange"):
                values = _choices(attribute.replace("ValidateRange", "ValidateSet", 1))
                if len(values) == 2 and all(isinstance(value, (int, float)) for value in values):
                    spec["min"], spec["max"] = values

        if raw_default is not None:
            literal = _literal(raw_default)
            if literal is None and raw_default.strip().casefold() not in {"$null", "$()"}:
                warnings.append(f"{name}: complex or computed default was omitted")
            elif literal is not None:
                spec["default"] = literal
                spec.pop("required", None)
        schema[name] = spec

    result = {"_schema": schema}
    if positions:
        result["_positional"] = [name for name, _ in sorted(positions.items(), key=lambda item: item[1])]
    return result, warnings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Convert PowerShell param() declarations to a WMIBench parameters object")
    parser.add_argument("script", type=Path, help="Local .ps1/.psm1 file")
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation (default: 2)")
    args = parser.parse_args(argv)
    try:
        script = args.script.read_text(encoding="utf-8-sig")
        result, warnings = _convert(script)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)
    print(json.dumps(result, indent=args.indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
