import unittest

from remoteops.core.models import Host
from remoteops.remoting.session import FakeSession
from remoteops.remoting.session_manager import SessionManager


class SessionManagerTests(unittest.TestCase):
    def test_session_manager_reuses_connected_session(self) -> None:
        manager = SessionManager(session_factory=FakeSession)
        host = Host(name="Local Dev", address="localhost")

        first = manager.connect(host)
        second = manager.connect(host)

        self.assertIs(first, second)

