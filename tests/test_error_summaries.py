import unittest

from remoteops.core.models import SessionState
from remoteops.ui.errors import error_summary


class ErrorSummaryTests(unittest.TestCase):
    def test_categories_and_session_state_are_independent(self):
        for detail, expected in (
            ("IWbemServices.ExecQuery 0x80041003", "Access denied"),
            ("WMI query exceeded 10000 rows; no partial results", "too large"),
            ("RPC query deadline expired", "timed out"),
            ("Unexpected CIM class", "parse failed"),
            ("0x80041010", "not found"),
            ("connection refused", "unreachable"),
            ("Unknown command: nope", "Unknown command"),
            ("Service was not found", "unique service"),
            ("StopService failed: ReturnValue=2", "Service action failed"),
            ("Event log changed during retrieval", "Rerun the query"),
            ("WMI enumeration exceeded row cap 10", "too large"),
            ("Traceback: unclassified failure", "diagnostic below"),
        ):
            with self.subTest(detail=detail):
                self.assertIn(expected, error_summary(detail))
                self.assertIn("Session connected", error_summary(detail, SessionState.CONNECTED))
                self.assertIn("Reconnect", error_summary(detail, SessionState.ERROR))
                self.assertNotIn("Traceback", error_summary(detail))

    def test_unknown_mutation_outcome_takes_priority_over_timeout(self):
        summary = error_summary("StartService outcome unconfirmed; timeout", SessionState.CONNECTED)
        self.assertIn("Inspect remote state", summary)
        self.assertIn("Session connected", summary)
        self.assertNotIn("Try a smaller", summary)

    def test_local_method_preparation_failure_is_not_an_uncertain_action(self):
        summary = error_summary("WMI method preparation failed; action was not sent: missing member",
                                SessionState.CONNECTED)
        self.assertIn("No action sent", summary)
        self.assertIn("Session connected", summary)
        self.assertNotIn("unknown", summary)
