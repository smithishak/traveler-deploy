import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import traveler as app


class ManagerTests(unittest.TestCase):
    def test_saved_dotenv_wins_over_inherited_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.env').write_text("WG_PORT=443\nSERVER_IP=1.2.3.4\nCUSTOM_SETTING=saved\n")
            with patch.object(app, 'ROOT', root), patch.object(app, 'run') as run:
                with patch.dict(app.os.environ, {'WG_PORT': '12345', 'SERVER_IP': 'wrong',
                                                 'CUSTOM_SETTING': 'wrong', 'PATH': '/bin'}):
                    app.compose('config')
                inherited = run.call_args.kwargs['env']
                self.assertNotIn('WG_PORT', inherited)
                self.assertNotIn('SERVER_IP', inherited)
                self.assertNotIn('CUSTOM_SETTING', inherited)
                self.assertEqual(inherited['PATH'], '/bin')

    def test_corrupt_snapshot_rejected_before_stopping_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            target = state / 'backups' / 'test'
            target.mkdir(parents=True)
            (target / '.env').write_text('corrupt')
            (target / 'manifest.json').write_text(json.dumps({'files': {'.env': 'wrong'}}))
            with patch.object(app, 'STATE', state), patch.object(app, 'compose') as compose:
                with self.assertRaisesRegex(app.Failure, 'Контрольная сумма'):
                    app.restore(target, 10)
                compose.assert_not_called()

    def test_backup_outside_managed_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(app, 'STATE', Path(tmp) / 'state'), patch.object(app, 'compose') as compose:
                with self.assertRaises(app.Failure):
                    app.restore(Path(tmp), 10)
                compose.assert_not_called()


if __name__ == '__main__':
    unittest.main()
