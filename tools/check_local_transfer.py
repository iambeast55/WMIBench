"""Exercise transfer scripts against local PowerShell, using only temporary files."""
import base64
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from threading import Event

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from remoteops.remoting.transfer import transfer_file


class LocalScriptSession:
    def execute(self, script, timeout):
        wrapped = "$ErrorActionPreference='Stop'; $output=@(& { " + script + " }); ConvertTo-Json -InputObject $output -Compress -Depth 8"
        result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", "-"],
                                input=wrapped + "\n", capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError(result.stderr)
        return json.loads(result.stdout), ""


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    source, remote, downloaded = root / "source.bin", root / "remote ' file.bin", root / "download.bin"
    content = bytes(range(256)) * 400
    source.write_bytes(content)
    remote.write_bytes(b"previous")
    downloaded.write_bytes(b"previous")
    session = LocalScriptSession()
    transfer_file(session, "upload", source, str(remote), True, Event(), lambda *_: None)
    assert remote.read_bytes() == content
    transfer_file(session, "download", downloaded, str(remote), True, Event(), lambda *_: None)
    assert downloaded.read_bytes() == content
    assert not list(root.glob("*.part"))
    print("Local PowerShell upload/download passed: 102400 binary bytes, quoted paths, overwrite, SHA-256.")
