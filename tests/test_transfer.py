import base64
import hashlib
import tempfile
import unittest
from pathlib import Path
from threading import Event

from remoteops.remoting.session import RemoteSessionError
from remoteops.remoting.transfer import transfer_file


class DownloadSession:
    def __init__(self, data, bad_hash=False):
        self.data = data
        self.scripts = []
        self.bad_hash = bad_hash

    def execute(self, script, timeout):
        self.scripts.append(script)
        if "IO.FileInfo" in script:
            return [len(self.data)], ""
        if "ComputeHash" in script:
            return ["bad" if self.bad_hash else hashlib.sha256(self.data).hexdigest()], ""
        return [base64.b64encode(self.data).decode()], ""


class TransferTests(unittest.TestCase):
    def test_upload_commit_failure_reports_uncertain_outcome(self):
        class Session:
            def execute(self, script, timeout):
                if "ComputeHash" in script:
                    return [hashlib.sha256(b"new").hexdigest()], ""
                if "::Move(" in script:
                    raise ConnectionError("lost acknowledgement")
                return [], ""
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source"
            source.write_bytes(b"new")
            with self.assertRaisesRegex(RemoteSessionError, "destination may have been replaced"):
                transfer_file(Session(), "upload", source, "C:\\Temp\\dest", False, Event(), lambda *_: None)

    def test_download_verified_before_destination_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.txt"
            path.write_bytes(b"old")
            session = DownloadSession(b"new\x00data")
            result = transfer_file(session, "download", path, "C:\\Temp\\source.txt", True, Event(), lambda *_: None)
            self.assertEqual(path.read_bytes(), b"new\x00data")
            self.assertIn("SHA-256", result)
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_hash_failure_preserves_existing_destination(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.txt"
            path.write_bytes(b"old")
            with self.assertRaisesRegex(RemoteSessionError, "verification failed"):
                transfer_file(DownloadSession(b"new", True), "download", path, "C:\\Temp\\source.txt", True, Event(), lambda *_: None)
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_cancel_preserves_destination_and_cleans_temporary_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "output.txt"
            path.write_bytes(b"old")
            cancel = Event()
            with self.assertRaisesRegex(RemoteSessionError, "cancelled"):
                transfer_file(DownloadSession(b"new"), "download", path, "C:\\Temp\\source.txt", True, cancel, lambda *_: cancel.set())
            self.assertEqual(path.read_bytes(), b"old")
            self.assertEqual(list(Path(temp).glob("*.part")), [])

    def test_no_overwrite_or_unc_network_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "exists"
            path.touch()
            session = DownloadSession(b"x")
            with self.assertRaises(RemoteSessionError):
                transfer_file(session, "download", path, "C:\\Temp\\file", False, Event(), lambda *_: None)
            with self.assertRaises(RemoteSessionError):
                transfer_file(session, "download", path, "\\\\other\\share\\file", True, Event(), lambda *_: None)
            self.assertFalse(session.scripts)
