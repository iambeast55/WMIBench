import unittest
from implementation import run
from remoteops.commands.plugin_api import FakeContext


class CommandTests(unittest.TestCase):
    def test_filter_and_result(self):
        rows = [{"Name": "Spooler", "State": "Running", "StartMode": "Auto"}]
        ctx = FakeContext(query_results=[rows])
        self.assertEqual(run(ctx, {"service_name": "Spooler"}), rows)
        self.assertIn("Name='Spooler'", ctx.queries[0].query.wql)

    def test_quoted_name_is_a_literal(self):
        ctx = FakeContext(query_results=[[]])
        self.assertEqual(run(ctx, {"service_name": "a'b"}), [])
        self.assertIn("Name='a\\'b'", ctx.queries[0].query.wql)
