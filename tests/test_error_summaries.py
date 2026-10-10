import unittest

from remoteops.core.models import SessionState
from remoteops.ui.errors import error_summary


class ErrorSummaryTests(unittest.TestCase):
    def test_categories_and_session_state_are_independent(self):
        for detail, expected in (
            ("IWbemServices.ExecQuery 0x80041003", "permission"),
            ("WMI query exceeded 10000 rows; no partial results", "too large"),
            ("RPC query deadline expired", "too long"),
            ("Unexpected CIM class", "could not be read"),
            ("0x80041010", "does not provide"),
            ("connection refused", "could not be reached"),
            ("Unknown command: nope", "Unknown command"),
            ("Service was not found", "matching service"),
            ("StopService failed: ReturnValue=2", "service action"),
            ("Event log changed during retrieval", "Run the events query"),
            ("WMI enumeration exceeded row cap 10", "too large"),
            ("Traceback: unclassified failure", "error details"),
        ):
            with self.subTest(detail=detail):
                self.assertIn(expected, error_summary(detail))
                self.assertIn("still connected", error_summary(detail, SessionState.CONNECTED))
                self.assertIn("Reconnect explicitly", error_summary(detail, SessionState.ERROR))
                self.assertNotIn("Traceback", error_summary(detail))

    def test_unknown_mutation_outcome_takes_priority_over_timeout(self):
        summary = error_summary("StartService outcome unconfirmed; timeout", SessionState.CONNECTED)
        self.assertIn("Check the remote state", summary)
        self.assertIn("still connected", summary)
        self.assertNotIn("Try a smaller", summary)
