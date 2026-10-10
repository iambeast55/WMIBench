"""Verify our OS decoder against Windows' own local WMI custom marshaler.

Read-only, no remote credentials or policy changes. No captured host data is saved.
Run on Windows with PYTHONPATH=src. This does not validate remote RPC framing.
"""
import ctypes as c
import sys
from uuid import UUID

from remoteops.remoting.wmi_query import CLASS_INTERFACE, OS_PROPERTIES, OS_QUERY, decode_os_object


def main():
    if sys.platform != "win32":
        raise SystemExit("This independent native-marshaler smoke check requires Windows.")
    import win32com.client

    pointer = c.c_void_p
    guid_type = c.c_ubyte * 16

    def guid(value):
        return guid_type.from_buffer_copy(UUID(str(value)).bytes_le)

    def method(obj, slot, restype, *types):
        vtable = c.cast(obj, c.POINTER(c.POINTER(pointer))).contents
        return c.WINFUNCTYPE(restype, pointer, *types)(vtable[slot])

    def check(status):
        if status < 0:
            raise RuntimeError(f"Native COM failed: 0x{status & 0xFFFFFFFF:08x}")

    ole = c.OleDLL("ole32")
    automation = c.WinDLL("oleaut32")
    automation.SysAllocString.argtypes = [c.c_wchar_p]
    automation.SysAllocString.restype = pointer
    automation.SysFreeString.argtypes = [pointer]
    automation.SysFreeString.restype = None
    strings, objects = [], []

    def bstr(value):
        value = automation.SysAllocString(value)
        if not value:
            raise MemoryError("SysAllocString failed")
        strings.append(value)
        return value

    def output():
        value = pointer()
        objects.append(value)
        return value

    check(ole.CoInitializeEx(None, 2))
    try:
        locator = output()
        check(ole.CoCreateInstance(
            guid("4590f811-1d3a-11d0-891f-00aa004b2e24"), None, 1,
            guid("dc12a687-737f-11cf-884d-00aa004b2e24"), c.byref(locator),
        ))
        services = output()
        check(method(locator, 3, c.c_long, pointer, pointer, pointer, pointer, c.c_long,
                     pointer, pointer, c.POINTER(pointer))(
            locator, bstr("root\\cimv2"), None, None, None, 0, None, None, c.byref(services),
        ))
        ole.CoSetProxyBlanket.argtypes = [pointer, c.c_ulong, c.c_ulong, pointer,
                                        c.c_ulong, c.c_ulong, pointer, c.c_ulong]
        check(ole.CoSetProxyBlanket(services, 10, 0, None, 3, 3, None, 0))
        enumerator = output()
        check(method(services, 20, c.c_long, pointer, pointer, c.c_long, pointer,
                     c.POINTER(pointer))(
            services, bstr("WQL"), bstr(OS_QUERY), 0x30, None, c.byref(enumerator),
        ))
        instance, returned = output(), c.c_ulong()
        check(method(enumerator, 4, c.c_long, c.c_long, c.c_ulong, c.POINTER(pointer),
                     c.POINTER(c.c_ulong))(
            enumerator, 10000, 1, c.byref(instance), c.byref(returned),
        ))
        if returned.value != 1:
            raise RuntimeError("Native OS query returned no instance")
        stream = output()
        check(ole.CreateStreamOnHGlobal(None, True, c.byref(stream)))
        ole.CoMarshalInterface.argtypes = [pointer, c.POINTER(guid_type), pointer,
                                          c.c_ulong, pointer, c.c_ulong]
        check(ole.CoMarshalInterface(stream, c.byref(guid(CLASS_INTERFACE)), instance,
                                     2, None, 0))
        seek = method(stream, 5, c.c_long, c.c_longlong, c.c_ulong, c.POINTER(c.c_ulonglong))
        length = c.c_ulonglong()
        check(seek(stream, 0, 1, c.byref(length)))
        check(seek(stream, 0, 0, None))
        buffer, read = c.create_string_buffer(length.value), c.c_ulong()
        check(method(stream, 3, c.c_long, pointer, c.c_ulong, c.POINTER(c.c_ulong))(
            stream, buffer, length.value, c.byref(read),
        ))
        try:
            record = decode_os_object(buffer.raw[:read.value])
            native = win32com.client.Dispatch("WbemScripting.SWbemLocator").ConnectServer(
                ".", "root\\cimv2"
            ).ExecQuery(OS_QUERY)
            for item in native:
                expected = {name: item.Properties_(name).Value for name in OS_PROPERTIES}
                expected["TotalVisibleMemorySize"] = int(expected["TotalVisibleMemorySize"])
                assert record == expected, "Decoded properties differ from native WMI values"
            print(f"Native WMI codec smoke passed: {read.value} bytes, {len(record)} properties.")
        finally:
            check(seek(stream, 0, 0, None))
            ole.CoReleaseMarshalData.argtypes = [pointer]
            check(ole.CoReleaseMarshalData(stream))
    finally:
        for obj in reversed(objects):
            if obj:
                method(obj, 2, c.c_ulong)(obj)
        for value in strings:
            automation.SysFreeString(value)
        ole.CoUninitialize()


if __name__ == "__main__":
    main()
