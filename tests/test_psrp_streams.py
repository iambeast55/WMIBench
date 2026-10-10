import unittest

from remoteops.remoting.session import _format_ps_error_stream, _stringify_ps_output


class FakeStreams:
    def __init__(self, errors=None):
        self.error = errors or []


class FakeError:
    def __init__(self, message):
        self.message = message


class PsrpStreamTests(unittest.TestCase):
    def test_empty_streams_are_not_errors(self) -> None:
        self.assertEqual(_format_ps_error_stream(FakeStreams()), "")

    def test_error_stream_records_are_readable(self) -> None:
        self.assertEqual(_format_ps_error_stream(FakeStreams([FakeError("Access denied")])), "Access denied")

    def test_output_list_is_rendered_as_lines(self) -> None:
        self.assertEqual(_stringify_ps_output(["one", "two"]), "one\ntwo")

