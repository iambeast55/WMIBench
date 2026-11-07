"""Bounded NDR32 ExecMethod codec; no transport or live operations.

Wire sources: MS-WMI 3.1.4.3.22 (op24), MS-DCOM 2.2.14/2.2.18.6,
and MS-WMIO 2.2/2.3.3. In particular, ppOutParams is a unique pointer
to an interface pointer, not the usual top-level ref output parameter.

https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmi/cbce28cd-fd45-4df1-9ec8-416e6bb33691
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/0ac1987a-b6e9-4288-bc4a-0a8ca8555fc6
https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-wmio/96d48962-118e-4c2f-bd6d-8046fb81ef64

The generic scalar/flat-array input class follows the grammar; provider interoperability
has not been established by a live call or a native-marshaler fixture. Legacy built-in
calls retain their narrower validation and the independent EnumKey wire fixture.
"""
from __future__ import annotations

import re
import struct
from uuid import UUID

from remoteops.remoting import wmi_query
from remoteops.remoting.dcom_wire import (
    DcomStatusError,
    Reader,
    _interface_pointer,
    _orpcthat,
    orpcthis,
)
from remoteops.remoting.rpc_auth_probe import MAX_RESPONSE
from remoteops.remoting.rpc_probe import MAX_FRAGMENT, RpcProtocolError

MAX_TEXT_UNITS = 1024
MAX_REQUEST_BYTES = MAX_FRAGMENT - 128
_ENUM_INPUTS = {"hDefKey": 19, "sSubKeyName": 8}
_SERVICE_PATH = re.compile(r'Win32_Service\.Name="(?:[^"\\\x00-\x1f]|\\["\\])+"', re.I)


def _text(value: object, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or (not value and not empty) or "\0" in value:
        raise ValueError(f"{label} must be a {'possibly empty ' if empty else ''}NUL-free string")
    if len(value) > MAX_TEXT_UNITS:
        raise ValueError(f"{label} exceeds {MAX_TEXT_UNITS} UTF-16 code units")
    try:
        encoded = value.encode("utf-16-le")
    except UnicodeError as exc:
        raise ValueError(f"{label} is not valid Unicode") from exc
    if len(encoded) > MAX_TEXT_UNITS * 2:
        raise ValueError(f"{label} exceeds {MAX_TEXT_UNITS} UTF-16 code units")
    return value


def _validate_method(path: str, method: str, inputs: dict[str, tuple[int, object]]) -> str:
    _text(path, "Object path")
    _text(method, "Method name")
    if not isinstance(inputs, dict):
        raise ValueError("Method inputs must be a dictionary")
    if method.casefold() in ("stopservice", "startservice"):
        if not _SERVICE_PATH.fullmatch(path) or inputs:
            raise ValueError("Service methods require a Win32_Service.Name instance and no inputs")
        return "StopService" if method.casefold() == "stopservice" else "StartService"
    if method.casefold() != "enumkey" or path.casefold() != "stdregprov":
        raise ValueError("Only Win32_Service StartService/StopService and StdRegProv EnumKey are allowed")
    if inputs.keys() != _ENUM_INPUTS.keys():
        raise ValueError("EnumKey requires exactly hDefKey and sSubKeyName")
    for name, kind in _ENUM_INPUTS.items():
        item = inputs[name]
        if not isinstance(item, tuple) or len(item) != 2 or type(item[0]) is not int or item[0] != kind:
            raise ValueError(f"{name} requires a (CIM type {kind}, value) tuple")
    root = inputs["hDefKey"][1]
    if type(root) is not int or not 0 <= root <= 0xFFFFFFFF:
        raise ValueError("hDefKey must be a uint32 integer")
    _text(inputs["sSubKeyName"][1], "sSubKeyName", empty=True)
    return "EnumKey"


def _block(data: bytes) -> bytes:
    return struct.pack("<I", len(data) + 4) + data


def _heap(data: bytes) -> bytes:
    return struct.pack("<I", len(data) | 0x80000000) + data


def _encoded_string(text: str) -> bytes:
    # MS-WMIO 2.2.78 requires Latin-1 compression when every code point fits.
    if all(ord(char) <= 255 for char in text):
        return b"\0" + text.encode("latin-1") + b"\0"
    return b"\1" + text.encode("utf-16-le") + b"\0\0"


def _parameters(inputs: dict[str, tuple[int, object]]) -> bytes:
    """Emit a bounded typed input instance; input order supplies parameter ID qualifiers."""
    class_heap = bytearray(_encoded_string("__PARAMETERS"))
    instance_heap = bytearray(_encoded_string("__PARAMETERS"))

    def check_heap_budget():
        if len(class_heap) + len(instance_heap) > MAX_REQUEST_BYTES:
            raise ValueError(f"ExecMethod request exceeds {MAX_REQUEST_BYTES} bytes")

    def encoded_value(kind, value):
        base = kind & ~0x2000
        if kind & 0x2000:
            values = b"".join(encoded_value(base, item) for item in value)
            offset = len(instance_heap)
            instance_heap.extend(struct.pack("<I", len(value)) + values)
            check_heap_budget()
            return struct.pack("<I", offset)
        if base in (8, 101, 102):
            offset = len(instance_heap)
            instance_heap.extend(_encoded_string(value))
            check_heap_budget()
            return struct.pack("<I", offset)
        if base == 11:
            value = 0xFFFF if value else 0
        elif base == 103:
            value = ord(value)
        return struct.pack("<" + wmi_query.VALUE_FORMATS[base], value)

    def string_ref(text: str) -> int:
        offset = len(class_heap)
        class_heap.extend(_encoded_string(text))
        check_heap_budget()
        return offset

    def qualifier(name: str, kind: int, value: bytes, flavor: int) -> bytes:
        return struct.pack("<IBI", string_ref(name), flavor, kind) + value

    # No HeapItem is shared, including repeated qualifier-name strings (2.2.66).
    abstract = qualifier("abstract", 11, b"\xff\xff", 0x10)
    lookup_entries = []
    size = (len(inputs) + 3) // 4
    defaults_nd, instance_nd = bytearray(size), bytearray(size)
    defaults, values = bytearray(), bytearray()
    for order, (name, (kind, value)) in enumerate(inputs.items()):
        name_ref = string_ref(name)
        qualifiers = qualifier("in", 11, b"\xff\xff", 0x12)
        qualifiers += qualifier("ID", 3, struct.pack("<i", order), 0x12)
        info_ref = len(class_heap)
        class_heap.extend(struct.pack("<IHII", kind, order, len(values), 0))
        class_heap.extend(_block(qualifiers))
        lookup_entries.append((name.casefold(), struct.pack("<II", name_ref, info_ref)))
        width = 4 if kind & 0x2000 else struct.calcsize("<" + wmi_query.VALUE_FORMATS[kind])
        defaults_nd[order // 4] |= 1 << (2 * (order % 4))
        defaults.extend(b"\xff" * width)
        if value is None:
            instance_nd[order // 4] |= 1 << (2 * (order % 4))
            values.extend(bytes(width))
        else:
            values.extend(encoded_value(kind, value))

    lookup = b"".join(entry for _, entry in sorted(lookup_entries))
    cls = b"\0" + struct.pack("<II", 0, size + len(values)) + _block(b"") + _block(abstract)
    cls += struct.pack("<I", len(inputs)) + lookup + defaults_nd + defaults + _heap(class_heap)
    instance = b"\0" + bytes(4) + instance_nd + values + _block(b"") + b"\1"
    instance += _heap(instance_heap)
    payload = b"\2" + _block(cls) + _block(instance)
    envelope = struct.pack("<II", 0x574F454D, 4)
    envelope += wmi_query.CLASS_INTERFACE.bytes_le + wmi_query.CLASS_MARSHALER.bytes_le
    return envelope + bytes(8) + struct.pack("<II", 0x12345678, len(payload)) + payload


def _enum_parameters(inputs):
    return _parameters({name: inputs[name] for name in _ENUM_INPUTS})


def method_request(version: tuple[int, int], causality: UUID, path: str, method: str,
                   inputs: dict[str, tuple[int, object]], *, outputs=None) -> bytes:
    """Build op24 for a namespace-relative path, with synchronous outputs.

Without outputs, enforce legacy builtin signatures. Explicit outputs enable typed
generic calls; input insertion order specifies parameter IDs. Requests must fit
the existing single-fragment sender.
"""
    if outputs is None:
        method = _validate_method(path, method, inputs)
    else:
        from remoteops.commands.wmi_invocation import validate_call
        validate_call(path, method, inputs, outputs)
    data = orpcthis(version, causality) + wmi_query._bstr(path, 0x20000)
    data += bytes(-len(data) % 4) + wmi_query._bstr(method, 0x20004)
    data += bytes(-len(data) % 4) + bytes(8)  # lFlags=0, pCtx=NULL.
    if inputs:
        obj = _enum_parameters(inputs) if outputs is None else _parameters(inputs)
        data += struct.pack("<III", 0x20008, len(obj), len(obj)) + obj
        data += bytes(-len(data) % 4)
    else:
        data += bytes(4)  # pInParams=NULL is required for no-input methods.
    # Each top-level unique parameter includes its pointee before the next parameter:
    # ppOutParams != NULL, *ppOutParams = NULL, ppCallResult = NULL.
    data += struct.pack("<III", 0x2000C, 0, 0)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError(f"ExecMethod request exceeds {MAX_REQUEST_BYTES} bytes")
    return data


def delete_request(version, causality, path):
    # MS-WMI 3.1.4.3.14: synchronous op16, no context or call-result reference.
    from remoteops.commands.wmi_invocation import compile_delete
    compile_delete(path)
    data = orpcthis(version, causality) + wmi_query._bstr(path, 0x20000)
    data += bytes(-len(data) % 4) + bytes(12)
    if len(data) > MAX_REQUEST_BYTES:
        raise ValueError(f"DeleteInstance request exceeds {MAX_REQUEST_BYTES} bytes")
    return data


def decode_delete(data):
    reader = Reader(data, limit=MAX_RESPONSE)
    _orpcthat(reader)
    if reader.u32():
        raise RpcProtocolError("DeleteInstance returned an unrequested ppCallResult pointer")
    status = reader.u32()
    reader.finish()
    if status:
        raise DcomStatusError("IWbemServices.DeleteInstance", status)


def decode_method(data: bytes, properties: dict[str, int], *, allow_no_output=False) -> dict:
    """Decode the reply to method_request; ReturnValue is provider data, not HRESULT."""
    reader = Reader(data, limit=MAX_RESPONSE)
    _orpcthat(reader)
    if not reader.u32():
        raise RpcProtocolError("ExecMethod ppOutParams outer pointer changed to NULL")
    obj = _interface_pointer(reader)
    if reader.u32():
        raise RpcProtocolError("ExecMethod returned an unrequested ppCallResult pointer")
    status = reader.u32()
    reader.finish()
    if status:
        if obj is not None:
            raise RpcProtocolError("Failed ExecMethod returned unexpected output parameters")
        raise DcomStatusError("IWbemServices.ExecMethod", status)
    if obj is None:
        if allow_no_output:
            return {}
        # The complete HRESULT-success reply has no object references to release.
        raise wmi_query.WmiQueryError(
            "Method returned no output object; outcome unconfirmed; do not automatically retry")
    # A validated by-value envelope has no remote object references to release.
    # A body decode failure leaves the channel usable, but not the method outcome known.
    wmi_query._custom_envelope(obj)
    try:
        return wmi_query.decode_instance(obj, "__PARAMETERS", properties)
    except RpcProtocolError as exc:
        raise wmi_query.WmiQueryError(
            f"Method result could not be read; outcome unconfirmed; do not automatically retry: {exc}"
        ) from exc
