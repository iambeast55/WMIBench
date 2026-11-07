"""Typed, host-relative WMI method plans shared by native COM and DCOM."""
from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass

from remoteops.commands.wmi_catalog import QuerySpec, _identifier, _quote
from remoteops.commands.wmi_definitions import CIM_TYPES

ALL_CIM_TYPES = {**CIM_TYPES, **{name + "[]": kind | 0x2000 for name, kind in CIM_TYPES.items()}}
_KEY = r'[A-Za-z_][A-Za-z0-9_]*'
_VALUE = r'(?:"(?:[^"\\\x00-\x1f]|\\["\\])*"|true|false|-?[0-9]+)'
_PATH = re.compile(rf'{_KEY}(?:=@|\.{_KEY}={_VALUE}(?:,{_KEY}={_VALUE})*)?', re.I)


def namespace_name(namespace):
    # Reuse the query transport's relative-root namespace contract.
    if not isinstance(namespace, str) or len(namespace) > 256:
        raise ValueError("WMI namespace must be relative to this host and at most 256 characters")
    return QuerySpec(namespace, "Example", {"Value": 8}).namespace


def object_path(class_name, keys=None, *, singleton=False):
    _identifier(class_name)
    if type(singleton) is not bool:
        raise ValueError("singleton must be boolean")
    if keys is not None and not isinstance(keys, dict):
        raise ValueError("Object keys must be a dictionary")
    keys = {} if keys is None else keys
    if len(keys) > 16 or (singleton and keys):
        raise ValueError("Use at most 16 object keys, or singleton=True without keys")
    if len({name.casefold() for name in keys if isinstance(name, str)}) != len(keys):
        raise ValueError("Object keys must have unique names")
    pairs = []
    for name, value in keys.items():
        _identifier(name)
        if isinstance(value, str):
            literal = _quote(value, '"')
        elif type(value) is bool:
            literal = "true" if value else "false"
        elif type(value) is int and -(2**63) <= value < 2**64:
            literal = str(value)
        else:
            raise ValueError("Object keys must be text, boolean or 64-bit integers")
        pairs.append(name + "=" + literal)
    path = class_name + ("=@" if singleton else "." + ",".join(pairs) if pairs else "")
    validate_path(path)
    return path


def validate_path(path):
    if (not isinstance(path, str) or not _PATH.fullmatch(path)
            or len(path.encode("utf-16-le")) > 2048):
        raise ValueError("Use a namespace-relative class/instance path, not a host or namespace-qualified path")
    # Reject duplicate keys even when callers supply a path directly.
    if "." in path:
        tail = path.split(".", 1)[1]
        names = [match.group(1).casefold() for match in re.finditer(rf'({_KEY})={_VALUE}(?:,|$)', tail, re.I)]
        if len(names) > 16 or len(set(names)) != len(names):
            raise ValueError("Object paths require at most 16 unique keys")


def cim_value(kind, value, *, max_items=1024):
    """Validate values before transport; arrays cannot contain null elements."""
    if type(kind) is not int or kind not in ALL_CIM_TYPES.values():
        raise ValueError("Unsupported CIM type; embedded objects are not implemented")
    if value is None:
        return
    base = kind & ~0x2000
    if kind & 0x2000:
        if not isinstance(value, (list, tuple)) or len(value) > max_items:
            raise ValueError(f"CIM arrays require at most {max_items} elements")
        for item in value:
            if item is None:
                raise ValueError("Null array elements are not supported")
            cim_value(base, item, max_items=max_items)
    elif base in (8, 101, 102):
        if not isinstance(value, str) or "\0" in value:
            raise ValueError("CIM text must be a NUL-free string")
        try:
            if len(value.encode("utf-16-le")) > 131072:
                raise ValueError("Method text exceeds 65536 UTF-16 code units")
        except UnicodeError as exc:
            raise ValueError("Invalid Unicode in method input") from exc
    elif base == 11:
        if type(value) is not bool:
            raise ValueError("CIM boolean requires a Python bool")
    elif base == 103:
        if not isinstance(value, str) or len(value) != 1 or 0xD800 <= ord(value) <= 0xDFFF or ord(value) > 0xFFFF:
            raise ValueError("CIM char16 requires one non-surrogate UTF-16 character")
    elif base in (4, 5):
        maximum = 3.4028234663852886e38 if base == 4 else 1.7976931348623157e308
        if type(value) not in (int, float) or not -maximum <= value <= maximum:
            raise ValueError("CIM real value must be finite and in range")
    else:
        bits, signed = {2: (16, True), 3: (32, True), 16: (8, True), 17: (8, False),
                        18: (16, False), 19: (32, False), 20: (64, True), 21: (64, False)}[base]
        lower, upper = (-(2**(bits - 1)), 2**(bits - 1) - 1) if signed else (0, 2**bits - 1)
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("CIM integer must have the declared signedness and bit width")


def validate_call(path, method, inputs, outputs):
    validate_path(path)
    _identifier(method)
    if len(method) > 128 or not isinstance(inputs, dict) or len(inputs) > 64:
        raise ValueError("Methods accept at most 64 typed input parameters")
    if not isinstance(outputs, dict) or not 1 <= len(outputs) <= 64:
        raise ValueError("Declare 1..64 output properties; methods without output objects are not implemented")
    for fields in (inputs, outputs):
        for name in fields:
            _identifier(name)
            if len(name) > 128:
                raise ValueError("Method parameter names must not exceed 128 characters")
        if len({name.casefold() for name in fields}) != len(fields):
            raise ValueError("Method parameter names must be unique")
    for name, item in inputs.items():
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(f"{name} requires a (CIM type, value) pair")
        cim_value(*item)
    for kind in outputs.values():
        cim_value(kind, None)


@dataclass(frozen=True)
class MethodPlan:
    namespace: str
    path: str
    method: str
    input_items: tuple
    output_items: tuple
    timeout_seconds: int
    allow_no_output: bool = False

    @property
    def inputs(self):
        return {name: (kind, deepcopy(value)) for name, kind, value in self.input_items}

    @property
    def outputs(self):
        return dict(self.output_items)


@dataclass(frozen=True)
class DeleteInstancePlan:
    namespace: str
    path: str
    timeout_seconds: int


def compile_delete(path, namespace="root/cimv2", timeout_seconds=30):
    validate_path(path)
    if "." not in path and not path.endswith("=@"):
        raise ValueError("Deletion requires an instance path with keys, not a class")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("Method timeout_seconds must be an integer in 1..60")
    return DeleteInstancePlan(namespace_name(namespace), path, timeout_seconds)


def compile_method(path, method, inputs=None, outputs=None, namespace="root/cimv2", timeout_seconds=30,
                   *, allow_no_output=False):
    if type(allow_no_output) is not bool:
        raise ValueError("allow_no_output must be boolean")
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 60:
        raise ValueError("Method timeout_seconds must be an integer in 1..60")
    inputs = {} if inputs is None else inputs
    outputs = {"ReturnValue": "uint32"} if outputs is None else outputs
    if not isinstance(inputs, dict) or not isinstance(outputs, dict):
        raise ValueError("inputs and outputs must be dictionaries")
    typed = {}
    for name, item in inputs.items():
        if (not isinstance(item, (tuple, list)) or len(item) != 2 or not isinstance(item[0], str)
                or item[0] not in ALL_CIM_TYPES):
            raise ValueError(f"{name} requires a ('CIM type name', value) pair")
        typed[name] = (ALL_CIM_TYPES[item[0]], item[1])
    projected = {}
    for name, kind in outputs.items():
        if not isinstance(kind, str) or kind not in ALL_CIM_TYPES:
            raise ValueError(f"Unsupported method output type: {kind!r}")
        projected[name] = ALL_CIM_TYPES[kind]
    validate_call(path, method, typed, projected)
    return MethodPlan(namespace_name(namespace), path, method,
                      tuple((name, kind, tuple(value) if isinstance(value, list) else value)
                            for name, (kind, value) in typed.items()),
                      tuple(projected.items()), timeout_seconds, allow_no_output)
