"""Read local WMI metadata and compare the COM and PowerShell adapters."""
import base64
import json
import subprocess

from remoteops.core.models import AuthMethod, CredentialProfile, Host
from remoteops.remoting.explorer import native_inspect, powershell_script, request
from remoteops.remoting.wmi import NativeWmiClient


def main():
    profile = CredentialProfile(name="Local metadata", username="", method=AuthMethod.NTLM)
    client = NativeWmiClient(Host(name="Local", address="."), profile, None)
    try:
        for action in ("namespaces", "classes", "describe", "instances"):
            args = request(action, {"class_name": "Win32_Process", "properties": ["Name", "ProcessId"], "limit": 3})
            native = native_inspect(client, action, args, 60)
            script = "& {\n" + powershell_script(action, args) + "\n} | Select-Object -ExpandProperty ExplorerJson"
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, text=True, timeout=70)
            if result.returncode:
                raise RuntimeError(result.stderr)
            cim = json.loads(result.stdout)
            if action == "describe":
                key = lambda row: (row.get("Kind"), row.get("Method", ""), row.get("Direction", ""), row["Name"], row.get("Type", ""))
                assert {key(row) for row in native} == {key(row) for row in cim}, "COM/CIM schema differs"
            elif action in {"classes", "namespaces"}:
                assert {row["Name"] for row in native} == {row["Name"] for row in cim}
            else:
                assert len(native) == len(cim) == 3
            print(f"{action}: COM {len(native)}, CIM {len(cim)}")
    finally:
        client.close()


if __name__ == "__main__":
    main()
