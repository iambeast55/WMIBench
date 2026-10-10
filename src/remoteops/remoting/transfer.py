"""Chunked, verified file transfer over the existing PSRP runspace."""
from __future__ import annotations

import base64
import hashlib
import os
import sys
from pathlib import Path, PureWindowsPath
from uuid import uuid4

from remoteops.remoting.session import RemoteSessionError

CHUNK_SIZE = 48 * 1024


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def hash_script(path):
    return (f"$s=[IO.File]::OpenRead({path}); $h=[Security.Cryptography.SHA256]::Create(); "
            "try { ([BitConverter]::ToString($h.ComputeHash($s))).Replace('-','') } "
            "finally { $s.Dispose(); $h.Dispose() }")


def transfer_file(session, direction, local_path, remote_path, overwrite, cancel, progress):
    local = Path(local_path)
    remote = PureWindowsPath(remote_path)
    if not remote.is_absolute() or str(remote).startswith("\\\\"):
        raise RemoteSessionError("Use an absolute drive path on the connected host, such as C:\\Temp\\report.txt")
    if direction not in {"upload", "download"}:
        raise RemoteSessionError("Unknown transfer direction")

    def check_cancel():
        if cancel.is_set():
            raise RemoteSessionError("Transfer cancelled; destination was not replaced")

    def execute(script):
        check_cancel()
        output, errors = session.execute("& { $ErrorActionPreference='Stop'; " + script + " }", 120)
        if errors:
            raise RemoteSessionError(errors)
        return str(output[-1]) if output else ""

    quoted = ps_literal(remote)
    digest = hashlib.sha256()
    completed = 0
    if direction == "upload":
        size = local.stat().st_size
        temp = ps_literal(str(remote) + f".remoteops-{uuid4().hex}.part")
        created = False
        try:
            if not overwrite:
                execute(f"if ([IO.File]::Exists({quoted})) {{ throw 'Destination already exists' }}")
            execute(f"$s=[IO.File]::Open({temp},[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None); $s.Dispose()")
            created = True
            progress(0, size)
            with local.open("rb") as source:
                while chunk := source.read(CHUNK_SIZE):
                    check_cancel()
                    encoded = base64.b64encode(chunk).decode("ascii")
                    execute(f"$s=[IO.File]::OpenWrite({temp}); try {{ $null=$s.Seek({completed},[IO.SeekOrigin]::Begin); $b=[Convert]::FromBase64String('{encoded}'); $s.Write($b,0,$b.Length) }} finally {{ $s.Dispose() }}")
                    digest.update(chunk)
                    completed += len(chunk)
                    progress(completed, size)
            actual = execute(hash_script(temp)).lower()
            if actual != digest.hexdigest():
                raise RemoteSessionError("Upload verification failed; destination was not replaced")
            check_cancel()
            try:
                if overwrite:
                    execute(f"if ([IO.File]::Exists({quoted})) {{ [IO.File]::Replace({temp},{quoted},[NullString]::Value) }} else {{ [IO.File]::Move({temp},{quoted}) }}")
                else:
                    execute(f"[IO.File]::Move({temp},{quoted})")
            except Exception as exc:
                raise RemoteSessionError(f"Final publication could not be confirmed; the destination may have been replaced: {exc}") from exc
            created = False
        finally:
            if created:
                original_error = sys.exception()
                # Cleanup is independent of cancellation and limited to this transfer's temp file.
                try:
                    _, errors = session.execute(f"[IO.File]::Delete({temp})", 15)
                    if errors:
                        raise RemoteSessionError(errors)
                except Exception as exc:
                    message = f"Temporary file cleanup failed for {temp}: {exc}"
                    if original_error:
                        original_error.add_note(message)
                    else:
                        raise RemoteSessionError(message) from exc
    else:
        if local.exists() and not overwrite:
            raise RemoteSessionError("Destination already exists")
        temp = local.with_name(local.name + f".remoteops-{uuid4().hex}.part")
        size = int(execute(f"([IO.FileInfo]{quoted}).Length"))
        progress(0, size)
        try:
            with temp.open("xb") as destination:
                while completed < size:
                    check_cancel()
                    count = min(CHUNK_SIZE, size - completed)
                    encoded = execute(f"$s=[IO.File]::OpenRead({quoted}); try {{ $null=$s.Seek({completed},[IO.SeekOrigin]::Begin); $b=[byte[]]::new({count}); $n=$s.Read($b,0,$b.Length); [Convert]::ToBase64String($b,0,$n) }} finally {{ $s.Dispose() }}")
                    chunk = base64.b64decode(encoded, validate=True)
                    if not chunk or len(chunk) > count:
                        raise RemoteSessionError("Remote file changed or returned an invalid chunk")
                    destination.write(chunk)
                    digest.update(chunk)
                    completed += len(chunk)
                    progress(completed, size)
            actual = execute(hash_script(quoted)).lower()
            if actual != digest.hexdigest():
                raise RemoteSessionError("Download verification failed; source may have changed")
            check_cancel()
            if overwrite:
                os.replace(temp, local)
            else:
                # Atomic no-clobber publication; a concurrent file creation cannot be overwritten.
                os.link(temp, local)
                temp.unlink()
        finally:
            temp.unlink(missing_ok=True)
    progress(completed, completed)
    return f"{direction.capitalize()} complete: {completed:,} bytes; SHA-256 {digest.hexdigest()}"
