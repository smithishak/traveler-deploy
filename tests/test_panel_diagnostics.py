import base64
from contextlib import contextmanager
import hashlib
import http.client
import io
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from split import panel_diagnostics as diagnostics


class WebSocketPeer:
    def __init__(self, valid=True):
        self.valid = valid

    def sendall(self, request):
        key = re.search(rb"Sec-WebSocket-Key: (\S+)", request)[1]
        digest = hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest()
        accept = base64.b64encode(digest) if self.valid else b"bad"
        self.response = (
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n"
        )

    def makefile(self, mode):
        return io.BytesIO(self.response)


def fixture_profile():
    return {"outbounds": [{
        "type": "trojan", "server": "192.0.2.1", "server_port": 443, "password": "secret-fixture",
        "tls": {"enabled": True, "server_name": "vpn.example.org"},
        "transport": {"type": "ws", "path": "/private-fixture"},
    }]}


class DiagnosticsTests(unittest.TestCase):
    def test_rejects_unverified_upgrade(self):
        transport = diagnostics.probe_outbound(fixture_profile())["transport"]
        self.assertEqual(diagnostics.websocket_status(WebSocketPeer(), transport), 101)
        with self.assertRaises(ValueError):
            diagnostics.websocket_status(WebSocketPeer(valid=False), transport)

    @patch.object(diagnostics, "websocket_status", return_value=http.client.BAD_GATEWAY)
    @patch.object(diagnostics, "probe_client")
    @patch.object(diagnostics.socket, "create_connection")
    @patch.object(diagnostics.ssl, "create_default_context", return_value=MagicMock())
    def test_skips_egress_if_websocket_fails(self, tls, connect, probe, ws):
        result = diagnostics.check_route(fixture_profile())
        self.assertEqual([row["status"] for row in result["checks"]], ["pass", "fail", "skipped"])
        self.assertNotIn("secret-fixture", str(result))
        self.assertNotIn("private-fixture", str(result))
        probe.assert_not_called()

    @patch.object(diagnostics, "websocket_status", return_value=101)
    @patch.object(diagnostics.socket, "create_connection")
    @patch.object(diagnostics.ssl, "create_default_context", return_value=MagicMock())
    def test_egress_ip_mismatch_is_separate_from_successful_egress(self, tls, connect, ws):
        @contextmanager
        def fake_client(outbound, check_ip):
            self.assertEqual(outbound["type"], "trojan")
            self.assertTrue(check_ip)
            yield 50123, "fixture-proxy-secret"

        with patch.object(diagnostics, "probe_client", fake_client), patch.object(
            diagnostics, "proxy_get", side_effect=(b"", b"198.51.100.21")
        ) as request:
            result = diagnostics.check_route(fixture_profile(), expected_egress_ip="198.51.100.22")
        self.assertEqual([row["status"] for row in result["checks"]], ["pass", "pass", "pass", "fail"])
        self.assertEqual([call.args[2] for call in request.call_args_list], [diagnostics.PROBE_URL, diagnostics.IP_URL])
        self.assertNotIn("198.51.100.21", str(result))


if __name__ == "__main__":
    unittest.main()
