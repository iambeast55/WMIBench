import os
import unittest
from unittest.mock import patch

from remoteops.core.network import proxychains_active, resolve_tcp_address


class NetworkTests(unittest.TestCase):
    def test_proxychains_is_detected_from_preload(self):
        with patch("remoteops.core.network.sys.platform", "linux"), patch.dict(
            os.environ, {"LD_PRELOAD": "/usr/lib/libproxychains.so.4"}, clear=True
        ):
            self.assertTrue(proxychains_active())

    def test_proxychains_override_enables_detection(self):
        with patch("remoteops.core.network.sys.platform", "linux"), patch.dict(
            os.environ, {"WMIBENCH_PROXYCHAINS": "1"}, clear=True
        ):
            self.assertTrue(proxychains_active())

    def test_hostname_resolution_uses_getaddrinfo(self):
        with patch("remoteops.core.network.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("198.51.100.10", 0)),
        ]) as getaddrinfo:
            self.assertEqual(str(resolve_tcp_address("server.example.com", allow_hostname=True)),
                             "198.51.100.10")
            getaddrinfo.assert_called_once_with("server.example.com", None, type=1)


if __name__ == "__main__":
    unittest.main()
