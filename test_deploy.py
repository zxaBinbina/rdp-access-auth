"""Deployment regression: all system paths and service operations are isolated."""
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from deploy import Deployment, Layout, UNITS, deployment_plan, preflight, unit_files
from management import ManagementError


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.layout = Layout(self.root / 'system')
        self.commands = []
        self.checks = []
        def words(root, **options):
            output = root / 'fixture-words.json'
            output.write_text(json.dumps(['测试' + chr(0x4e00 + i) for i in range(2048)]))
            return output
        def connector(path, progress):
            path.write_bytes(b'fixture-connector-not-executed')
        self.deployment = Deployment(self.layout, check_host=False, runner=self.commands.append,
            health=lambda hostname, port: self.checks.append((hostname, port)), downloader=connector, word_builder=words)
        self.addCleanup(self.deployment.close)
        self.fields = dict(hostname='auth.example.test', rdp_address='desktop.example.test:3389',
                           tunnel_id=7, sakura_token='fixture-sakura-token', password='Fixture-password-1234!')

    def prepare(self):
        self.deployment.prepare(self.fields, 'fixture-cloudflare-token', port=28089)
        self.deployment.worker.join(timeout=5)
        self.assertEqual(self.deployment.phase, 'ready', self.deployment.error)

    def test_preparation_is_private_and_does_not_write_system(self):
        self.prepare()
        self.assertFalse(self.layout.config_dir.exists())
        self.assertEqual(self.commands, [])
        status = json.dumps(self.deployment.status())
        self.assertNotIn('fixture-cloudflare-token', status)
        self.assertNotIn(self.fields['password'], status)
        self.assertNotIn(self.fields['sakura_token'], status)
        self.assertEqual(stat.S_IMODE((self.deployment.stage / 'token').stat().st_mode), 0o600)

    def test_first_install_writes_credentials_and_starts_exact_services(self):
        self.prepare()
        self.deployment.apply()
        self.deployment.worker.join(timeout=5)
        self.assertEqual(self.deployment.phase, 'complete', self.deployment.error)
        value = json.loads(self.layout.config.read_text())
        self.assertEqual(value['sakura_token'], self.fields['sakura_token'])
        self.assertNotIn(self.fields['password'], self.layout.config.read_text())
        self.assertEqual(stat.S_IMODE(self.layout.config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.layout.config_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(self.layout.token.stat().st_mode), 0o600)
        self.assertEqual(self.checks, [('auth.example.test', 28089)])
        self.assertEqual(self.commands, [
            ['systemctl', 'daemon-reload'], ['systemctl', 'enable', '--now', UNITS[0]],
            ['systemctl', 'enable', '--now', UNITS[1]], ['systemctl', 'is-active', '--quiet', *UNITS]])
        self.assertEqual(json.loads(self.layout.receipt.read_text())['status'], 'running')
        with self.assertRaises(ManagementError):
            self.deployment.apply()

    def test_existing_and_late_conflicts_are_never_overwritten(self):
        self.layout.config_dir.mkdir(parents=True)
        self.layout.config.write_text('existing')
        with self.assertRaises(ManagementError):
            self.deployment.prepare(self.fields, 'fixture-cloudflare-token')
        self.assertEqual(self.layout.config.read_text(), 'existing')
        self.assertEqual(self.commands, [])
        self.layout.config.unlink()
        self.layout.config_dir.rmdir()
        self.prepare()
        self.layout.config_dir.mkdir()
        self.layout.config.write_text('installed-by-another-wizard')
        self.deployment.apply()
        self.deployment.worker.join(timeout=5)
        self.assertEqual(self.deployment.phase, 'failed')
        self.assertEqual(self.layout.config.read_text(), 'installed-by-another-wizard')
        self.assertEqual(self.commands, [])

    def test_legacy_deployment_blocks_installation(self):
        old = self.layout.path('/etc/rdp-auth')
        old.mkdir(parents=True)
        self.assertIn(str(old), deployment_plan(self.layout)['conflicts'])
        with self.assertRaises(ManagementError):
            preflight(self.layout, 28089, check_host=False)

    def test_vendor_service_blocks_installation(self):
        unit = self.layout.path('/usr/lib/systemd/system/rdp-access-auth.service')
        unit.parent.mkdir(parents=True)
        unit.write_text('[Service]\nExecStart=/bin/true\n')
        with self.assertRaises(ManagementError):
            preflight(self.layout, 28089, check_host=False)

    def test_changed_artifact_blocks_apply(self):
        self.prepare()
        (self.deployment.stage / 'cloudflared').write_bytes(b'changed')
        self.deployment.apply()
        self.deployment.worker.join(timeout=5)
        self.assertEqual(self.deployment.phase, 'failed')
        self.assertFalse(self.layout.config_dir.exists())
        self.assertEqual(self.commands, [])

    def test_failed_health_stops_new_services_and_preserves_credentials(self):
        self.prepare()
        self.deployment.health = lambda *args: (_ for _ in ()).throw(ManagementError('fixture failure'))
        self.deployment.apply()
        self.deployment.worker.join(timeout=5)
        self.assertEqual(self.deployment.phase, 'failed')
        self.assertEqual(self.commands[-2:], [['systemctl', 'disable', '--now', UNITS[1]],
                                            ['systemctl', 'disable', '--now', UNITS[0]]])
        self.assertTrue(self.layout.config.exists())
        self.assertEqual(json.loads(self.layout.receipt.read_text())['status'], 'needs-attention')

    def test_rejected_input_does_not_prepare_or_install(self):
        for token, port in [('short', 28089), ('bad token with spaces', 28089), ('fixture-valid-token', True),
                            ('fixture-valid-token', 22), ('fixture-valid-token', 65536)]:
            with self.assertRaises(ManagementError):
                self.deployment.prepare(self.fields, token, port)
        self.assertEqual(self.deployment.phase, 'idle')
        self.assertEqual(self.commands, [])

    def test_units_use_credentials_not_command_line_secrets(self):
        units = unit_files(28089)
        self.assertIn('--port 28089', units[UNITS[0]])
        self.assertIn('RDP_AUTH_WORDLIST=%d/words.json', units[UNITS[0]])
        self.assertIn('--token-file %d/token', units[UNITS[1]])
        self.assertIn('DynamicUser=yes', units[UNITS[0]])
        self.assertNotIn('--token fixture', units[UNITS[1]])


if __name__ == '__main__':
    unittest.main()
