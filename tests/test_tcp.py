import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import traveler as app
import tcp_fallback as tcp


class ClientSecurityTests(unittest.TestCase):
    def profile(self, mode):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            (state / 'tcp').mkdir()
            (state / 'enabled.json').write_text('{}')
            (state / 'tcp' / 'server.json').write_text(json.dumps({'inbounds': [{
                'users': [{'password': 'test-only-not-a-real-password'}],
                'transport': {'path': '/test-path'}}]}))
            with patch.object(app, 'STATE', state), \
                    patch.object(app, 'TCP_COMPOSE', state / 'enabled.json'), \
                    patch.object(app, 'env_values', return_value=('vpn.example', 443)), \
                    patch.object(tcp, 'authority', return_value='test-ca-pem'), \
                    patch.object(tcp.socket, 'gethostbyname', return_value='192.0.2.1'):
                return tcp.client_config(mode)

    def test_profile_verifies_ca_and_name_without_bootstrap_dns_loop(self):
        outbound = self.profile('tun')['outbounds'][0]
        self.assertEqual(outbound['server'], '192.0.2.1')
        self.assertEqual(outbound['tls']['server_name'], 'vpn.example')
        self.assertIs(outbound['tls']['insecure'], False)
        self.assertEqual(outbound['tls']['certificate'], 'test-ca-pem')
        self.assertEqual(outbound['transport']['headers']['Host'], 'vpn.example')

    def test_tun_captures_both_families_and_sends_dns_through_tcp_tunnel(self):
        profile = self.profile('tun')
        self.assertEqual(len(profile['outbounds']), 1)  # No direct fallback on failure.
        self.assertTrue(any(':' in address for address in profile['inbounds'][0]['address']))
        self.assertTrue(profile['inbounds'][0]['strict_route'])
        self.assertEqual(profile['dns']['servers'][0]['type'], 'tcp')
        self.assertEqual(profile['dns']['servers'][0]['detour'], profile['route']['final'])

    def test_proxy_does_not_bind_physical_interface_or_change_routes(self):
        profile = self.profile('proxy')
        self.assertNotIn('auto_detect_interface', profile['route'])
        self.assertEqual(profile['inbounds'][0]['listen'], '127.0.0.1')
        self.assertEqual(profile['inbounds'][0]['type'], 'mixed')


if __name__ == '__main__':
    unittest.main()
