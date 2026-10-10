"""Validated, data-only SELECT definitions for the editable command library."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

from remoteops.commands.wmi_catalog import QuerySpec, WmiCommandError, _identifier, wql_literal


CIM_TYPES = {"string": 8, "boolean": 11, "sint8": 16, "uint8": 17,
             "sint16": 2, "uint16": 18, "sint32": 3, "uint32": 19,
             "sint64": 20, "uint64": 21, "real32": 4, "real64": 5,
             "datetime": 101, "reference": 102, "char16": 103}
OPERATORS = {"eq": "=", "ne": "!=", "lt": "<", "le": "<=", "gt": ">", "ge": ">="}
NULL_OPERATORS = {"is_null": "IS NULL", "not_null": "IS NOT NULL"}


@dataclass(frozen=True)
class WmiQueryPlan:
    query: QuerySpec
    timeout_seconds: int
    columns: tuple[str, ...]


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{label} must be an object containing only: {', '.join(sorted(allowed))}")


def _name(value):
    _identifier(value)
    if len(value) > 128:
        raise ValueError("WMI identifiers must not exceed 128 characters")


def _literal(value, kind):
    if kind.startswith(("uint", "sint")):
        bits = int(kind[4:])
        lower = 0 if kind.startswith("u") else -(2 ** (bits - 1))
        upper = 2**bits - 1 if kind.startswith("u") else 2 ** (bits - 1) - 1
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"Expected a {kind} integer")
    elif kind.startswith("real"):
        if type(value) not in (int, float) or not -1.7976931348623157e308 <= value <= 1.7976931348623157e308:
            raise ValueError(f"Expected a finite {kind} number")
        if kind == "real32" and abs(value) > 3.4028234663852886e38:
            raise ValueError("real32 value is out of range")
    elif kind == "boolean":
        if type(value) is not bool:
            raise ValueError("Expected a boolean value")
    else:
        if not isinstance(value, str) or len(value) > 1024:
            raise ValueError(f"Expected {kind} text of at most 1024 characters")
        if kind == "char16" and len(value.encode("utf-16-le")) != 2:
            raise ValueError("char16 requires one UTF-16 character")
    return wql_literal(value)


def validate_definition(definition, schema):
    _keys(definition, {"operation", "namespace", "class", "properties", "filters", "limits", "columns"}, "WMI definition")
    if definition.get("operation") != "query":
        raise ValueError("Custom WMI definitions support operation 'query' only")
    namespace = definition.get("namespace", "root/cimv2")
    if not isinstance(namespace, str) or namespace.replace("\\", "/").casefold() != "root/cimv2":
        raise ValueError("Custom WMI queries currently require namespace root/cimv2")
    _name(definition.get("class"))
    properties = definition.get("properties")
    if not isinstance(properties, dict) or not 1 <= len(properties) <= 64:
        raise ValueError("WMI properties must map 1..64 property names to CIM type names")
    for name, kind in properties.items():
        _name(name)
        if not isinstance(kind, str) or kind not in CIM_TYPES:
            raise ValueError(f"Unsupported CIM type for {name}: {kind!r}")
    if len({name.casefold() for name in properties}) != len(properties):
        raise ValueError("Duplicate WMI property names")
    limits = definition.get("limits", {})
    _keys(limits, {"rows", "timeout_seconds"}, "WMI limits")
    for field, default, maximum in (("rows", 1000, 10000), ("timeout_seconds", 30, 60)):
        value = limits.get(field, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError(f"WMI {field} must be an integer in 1..{maximum}")
    columns = definition.get("columns", list(properties))
    if (not isinstance(columns, list) or not columns or not all(isinstance(name, str) for name in columns)
            or len(set(columns)) != len(columns) or any(name not in properties for name in columns)):
        raise ValueError("WMI columns must be unique selected property names")
    filters = definition.get("filters", [])
    if not isinstance(filters, list) or len(filters) > 16:
        raise ValueError("WMI filters must be an array of at most 16 comparisons")
    for condition in filters:
        _keys(condition, {"property", "operator", "parameter", "value"}, "WMI filter")
        name, operator = condition.get("property"), condition.get("operator")
        if not isinstance(name, str) or name not in properties:
            raise ValueError("Filter property must be a selected property")
        if not isinstance(operator, str) or operator not in OPERATORS.keys() | NULL_OPERATORS.keys():
            raise ValueError(f"Unsupported WMI filter operator: {operator!r}")
        sources = set(condition) & {"parameter", "value"}
        if operator in NULL_OPERATORS:
            if sources:
                raise ValueError("Null checks do not take a parameter or value")
            continue
        if len(sources) != 1:
            raise ValueError("A comparison requires exactly one parameter or literal value")
        kind = properties[name]
        if kind == "boolean" and operator not in {"eq", "ne"}:
            raise ValueError("Boolean filters support eq/ne only")
        if "parameter" in condition:
            parameter = condition["parameter"]
            if not isinstance(parameter, str) or parameter not in schema:
                raise ValueError(f"Unknown WMI filter parameter: {parameter!r}")
            expected = ("int" if kind.startswith(("uint", "sint")) else "float" if kind.startswith("real")
                        else "bool" if kind == "boolean" else "str")
            if schema[parameter].get("type", "str") != expected:
                raise ValueError(f"Parameter {parameter} must have type {expected} for {name}")
            if "default" not in schema[parameter] and not schema[parameter].get("required"):
                raise ValueError(f"Filter parameter {parameter} needs a default or required=true")
        else:
            _literal(condition["value"], kind)


def compile_definition(definition, schema, values) -> WmiQueryPlan:
    validate_definition(definition, schema)
    clauses = []
    for condition in definition.get("filters", []):
        name, operator = condition["property"], condition["operator"]
        if operator in NULL_OPERATORS:
            clauses.append(f"{name} {NULL_OPERATORS[operator]}")
        else:
            if "parameter" in condition:
                parameter = condition["parameter"]
                if parameter not in values:
                    raise ValueError(f"Missing WMI filter parameter: {parameter}")
                value = values[parameter]
            else:
                value = condition["value"]
            clauses.append(f"{name}{OPERATORS[operator]}{_literal(value, definition['properties'][name])}")
    limits = definition.get("limits", {})
    query = QuerySpec("root/cimv2", definition["class"],
                      {name: CIM_TYPES[kind] for name, kind in definition["properties"].items()},
                      " AND ".join(clauses), max_rows=limits.get("rows", 1000))
    if len(query.wql.encode("utf-16-le")) > 4096:
        raise ValueError("Compiled WMI query exceeds 2048 UTF-16 characters")
    return WmiQueryPlan(query, limits.get("timeout_seconds", 30),
                        tuple(definition.get("columns", query.properties)))


def execute_query_plan(plan, query_callback, timeout_seconds):
    budget = min(timeout_seconds, plan.timeout_seconds)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError("WMI query timeout must be positive and finite")
    deadline = time.monotonic() + budget
    rows = query_callback(plan.query, budget)
    if time.monotonic() > deadline:
        raise TimeoutError("WMI command deadline expired")
    if (not isinstance(rows, list) or len(rows) > plan.query.max_rows
            or any(not isinstance(row, dict) or not plan.query.properties.keys() <= row.keys() for row in rows)):
        raise WmiCommandError("WMI query returned incomplete records or exceeded its row limit")
    # Column selection changes presentation only; selected raw properties remain
    # available in the record, table, export, and persisted history.
    order = list(plan.columns) + [name for name in plan.query.properties if name not in plan.columns]
    return [{name: row[name] for name in order} for row in rows]
