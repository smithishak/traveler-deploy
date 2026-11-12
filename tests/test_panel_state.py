import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from split import panel_state as panel


class PanelStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = self.root / 'origin.json'
        self.legacy_credentials = [
            {'name': 'traveler', 'password': 'test-only-original-password'},
            {'name': 'Планшет / “Марии” 🎬', 'password': 'test-only-existing-password'},
        ]
        self.original = panel.json_bytes({
            'inbounds': [{
                'type': 'trojan', 'users': self.legacy_credentials,
                'transport': {'type': 'ws', 'path': '/original-route'},
            }],
            'experimental': {'v2ray_api': {'stats': {
                'enabled': True, 'users': [item['name'] for item in self.legacy_credentials],
            }}},
        })
        self.config.write_bytes(self.original)
        self.apply = Mock(side_effect=self.replace_config)
        self.restart = Mock()
        self.store = panel.StateStore(self.root / 'state', self.config, self.apply, self.restart)

    def replace_config(self, config, original):
        self.assertEqual(self.config.read_bytes(), original)
        panel.atomic_write(self.config, panel.json_bytes(config))

    def test_migration_preserves_credentials_and_stable_device_ids(self):
        _, _, first = self.store.load()
        _, _, second = self.store.load()
        self.assertEqual(first, second)
        self.assertEqual(self.config.read_bytes(), self.original)
        devices = first['users'][0]['devices']
        self.assertEqual([item['name'] for item in devices],
                         [item['name'] for item in self.legacy_credentials])
        self.assertNotIn('test-only', self.store.state_path.read_text(encoding='utf-8'))
        self.assertNotIn('test-only', json.dumps(panel.public_state(first, None)))
        self.apply.assert_not_called()

    def test_device_creation_enrolls_stats_and_user_removal_revokes_device(self):
        state = self.store.action({'action': 'create_user', 'name': 'Мария'})
        self.apply.assert_not_called()
        user_id = state['users'][-1]['id']
        state = self.store.action({
            'action': 'create_device', 'user_id': user_id, 'name': 'Телефон', 'kind': 'phone',
        })
        device = state['users'][-1]['devices'][0]
        config = panel.read_json(self.config)
        credentials = panel.trojan(config)['users']
        self.assertEqual(credentials[:-1], self.legacy_credentials)
        self.assertEqual(credentials[-1]['name'], device['username'])
        self.assertEqual(config['experimental']['v2ray_api']['stats']['users'],
                         [item['name'] for item in credentials])
        self.assertEqual(panel.trojan(config)['transport']['path'], '/original-route')
        self.store.action({'action': 'delete_user', 'user_id': user_id})
        self.assertEqual(panel.trojan(panel.read_json(self.config))['users'], self.legacy_credentials)
        self.assertFalse(self.store.pending_path.exists())

    def test_legacy_profile_cannot_be_deleted_individually_or_with_owner(self):
        _, _, state = self.store.load()
        owner = state['users'][0]
        for action in ('delete_device', 'delete_user'):
            with self.subTest(action=action), self.assertRaises(panel.ActionError):
                self.store.action({
                    'action': action, 'user_id': owner['id'], 'device_id': owner['devices'][0]['id'],
                })
        self.assertEqual(self.config.read_bytes(), self.original)
        self.apply.assert_not_called()

    def test_failed_apply_keeps_metadata_and_config(self):
        _, _, prior = self.store.load()
        self.apply.side_effect = OSError('simulated failed validation')
        with self.assertRaises(OSError):
            self.store.action({
                'action': 'create_device', 'user_id': panel.OWNER_ID, 'name': 'Новый', 'kind': 'phone',
            })
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.store.load()[2], prior)
        self.assertFalse(self.store.pending_path.exists())

    def test_recovery_after_config_rename_finishes_state_change(self):
        def interrupt(config, original):
            self.replace_config(config, original)
            raise KeyboardInterrupt

        self.apply.side_effect = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.store.action({
                'action': 'create_device', 'user_id': panel.OWNER_ID, 'name': 'Ноутбук', 'kind': 'laptop',
            })
        self.assertTrue(self.store.pending_path.exists())
        _, config, state = self.store.load()
        self.assertEqual(state['users'][0]['devices'][-1]['name'], 'Ноутбук')
        self.assertEqual(state['users'][0]['devices'][-1]['kind'], 'laptop')
        self.restart.assert_called_once_with(config)
        self.assertFalse(self.store.pending_path.exists())

    def test_telemetry_requires_fresh_snapshot_and_does_not_guess_usage(self):
        _, _, state = self.store.load()
        path = self.root / 'stats.json'
        now = datetime.now(timezone.utc)
        report = {
            'updated_at': now.isoformat(),
            'users': {'traveler': {'last_seen': now.isoformat(), 'upload': 123, 'download': 456}},
        }
        path.write_bytes(panel.json_bytes(report))
        result = panel.public_state(state, panel.read_telemetry(path))
        original, unknown = result['users'][0]['devices']
        self.assertTrue(result['telemetry'])
        self.assertEqual((original['upload'], original['download']), (123, 456))
        self.assertIsNotNone(original['last_seen'])
        self.assertEqual((unknown['upload'], unknown['download'], unknown['last_seen']), (None,) * 3)
        report['updated_at'] = (now - timedelta(minutes=10)).isoformat()
        path.write_bytes(panel.json_bytes(report))
        self.assertFalse(panel.public_state(state, panel.read_telemetry(path))['telemetry'])


if __name__ == '__main__':
    unittest.main()
