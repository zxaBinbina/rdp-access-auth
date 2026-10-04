"""Exercise migration failure and recovery using real SQLite and fake systemd."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
import unittest
from unittest.mock import patch

from installation import installed_targets, preferred_target
from management import ManagementError, Target, read_config
from migration import Migration, sqlite_backup, credential_line, failure_summary
from test_management import ManagementFixture


class MigrationTests(ManagementFixture):
    def setUp(self):
        super().setUp()
        self.target = Target(self.root / 'etc/rdp-auth/portal-settings.json',
                             self.root / 'var/lib/rdp-auth/state.sqlite3', 'rdp-auth.service',
                             self.root / 'usr/local/lib/rdp-auth', 'legacy')
        self.create()
        self.target.state.parent.mkdir(parents=True)
        self.make_state()
        words = self.target.runtime / 'wordlists/objects.json'
        words.parent.mkdir(parents=True)
        words.write_text(json.dumps(['测试' + chr(0x4e00 + i) for i in range(2048)]))
        self.before = self.target.config.read_bytes()
        self.active, self.commands, self.checks = True, [], []
        self.unit = self.root / 'etc/systemd/system/rdp-auth.service'
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text('[Service]\nExecStart=old-program\n')
        self.migration = self.new_migration()
        self.addCleanup(self.migration.close)

    def new_migration(self):
        return Migration(self.target, root=self.root, check_host=False, probe=self.probe,
                         runner=self.run_system, health=self.health, validator=self.validate)

    def probe(self, target):
        changed = hasattr(self, 'migration') and self.migration.dropin.exists()
        return dict(LoadState='loaded', ActiveState='active' if self.active else 'inactive', Type='simple',
                    WorkingDirectory='/usr/lib/rdp-access-auth' if changed else str(target.runtime),
                    ExecStart='/usr/bin/rdp-auth serve --port 18089' if changed else '/usr/bin/python3 -m gunicorn --bind 127.0.0.1:18089 portal:app',
                    StateDirectory='rdp-auth', FragmentPath=str(self.unit), DropInPaths='')

    def run_system(self, command):
        self.commands.append(command)
        if command[1] == 'stop':
            self.active = False
        elif command[1] == 'start':
            self.active = True

    def health(self, hostname, port):
        self.checks.append((hostname, port))

    def validate(self, config, database, words):
        self.assertNotEqual(database, self.target.state)
        self.assertEqual(config.read_bytes(), self.before)
        with closing(sqlite3.connect(database)) as db, db:
            self.assertEqual(db.execute('SELECT public_key FROM passkeys').fetchone()[0], 'keep-this-key')
            db.execute('DELETE FROM passkeys')  # Must only modify the disposable copy.

    def start(self):
        plan = self.migration.plan()
        self.migration.start(plan['revision'])
        self.migration.worker.join(timeout=5)
        self.assertFalse(self.migration.worker.is_alive())

    def assert_credentials(self):
        self.assertEqual(self.target.config.read_bytes(), self.before)
        with closing(sqlite3.connect(self.target.state)) as db:
            self.assertEqual(db.execute('SELECT public_key FROM passkeys').fetchone()[0], 'keep-this-key')
            self.assertEqual(db.execute('SELECT generation FROM temporary_password').fetchone()[0], 7)

    def test_success_preserves_identity_credentials_and_backups(self):
        self.start()
        self.assertEqual(self.migration.phase, 'complete', self.migration.error)
        self.assert_credentials()
        record = self.migration.record()
        self.assertEqual(record['phase'], 'complete')
        backup = Path(record['backup'])
        self.assertEqual((backup / 'settings.json').read_bytes(), self.before)
        self.assertEqual(stat.S_IMODE((backup / 'state.sqlite3').stat().st_mode), 0o600)
        content = self.migration.dropin.read_text()
        self.assertIn('WorkingDirectory=/usr/lib/rdp-access-auth', content)
        self.assertIn(str(self.target.state), content)
        for key in ('session_key', 'sakura_token', 'password_hash'):
            self.assertNotIn(read_config(self.target.config)[0][key], content)
            self.assertNotIn(read_config(self.target.config)[0][key], json.dumps(self.migration.status()))
        self.assertFalse(any(c[1] in ('enable', 'disable') for c in self.commands))
        self.assertTrue(self.migration.plan()['adopted'])

    def test_failed_new_runtime_restores_database_and_original_service(self):
        def health(hostname, port):
            if self.migration.dropin.exists():
                with closing(sqlite3.connect(self.target.state)) as db, db:
                    db.execute('DELETE FROM passkeys')
                raise ManagementError('fixture startup failure')
        self.migration.health = health
        self.start()
        self.assertEqual(self.migration.phase, 'failed')
        self.assertEqual(self.migration.record()['phase'], 'rolled-back')
        self.assertFalse(self.migration.dropin.exists())
        self.assertTrue(self.active)
        self.assert_credentials()

    def test_inactive_deployment_stays_inactive_after_check(self):
        self.active = False
        self.start()
        self.assertEqual(self.migration.phase, 'complete', self.migration.error)
        self.assertFalse(self.active)
        self.assertEqual(len(self.checks), 1)

    def test_runtime_incompatible_does_not_stop_original(self):
        self.migration.validator = lambda *a: (_ for _ in ()).throw(ManagementError('incompatible fixture'))
        self.start()
        self.assertEqual(self.migration.phase, 'failed')
        self.assertEqual(self.commands, [])
        self.assert_credentials()

    def test_stale_service_plan_is_rejected(self):
        plan = self.migration.plan()
        self.unit.write_text('changed by another admin')
        with self.assertRaises(ManagementError):
            self.migration.start(plan['revision'])
        self.assertEqual(self.commands, [])

    def test_restart_after_interruption_offers_and_executes_recovery(self):
        self.start()
        record = self.migration.record()
        self.migration._write_record(record, 'switching')
        with closing(sqlite3.connect(self.target.state)) as db, db:
            db.execute('DELETE FROM passkeys')
        fresh = self.new_migration()
        self.addCleanup(fresh.close)
        self.assertTrue(fresh.status()['recovery_required'])
        fresh.start(recover=True)
        fresh.worker.join(timeout=5)
        self.assertEqual(fresh.phase, 'complete', fresh.error)
        self.assert_credentials()
        self.assertFalse(self.migration.dropin.exists())

    def test_stop_failure_can_be_recovered_without_database_backup(self):
        self.migration.runner = lambda c: (_ for _ in ()).throw(ManagementError('fixture systemd failure'))
        self.start()
        self.assertTrue(self.migration.status()['recovery_required'])
        self.migration.runner = self.run_system
        self.migration.start(recover=True)
        self.migration.worker.join(timeout=5)
        self.assertEqual(self.migration.phase, 'complete', self.migration.error)
        self.assert_credentials()

    def test_sqlite_backup_includes_wal_entries(self):
        with closing(sqlite3.connect(self.target.state)) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute("INSERT INTO passkeys VALUES ('wal', 'wal-key')")
            db.commit()
            destination = self.root / 'backup.sqlite3'
            sqlite_backup(self.target.state, destination)
            with closing(sqlite3.connect(destination)) as backup:
                self.assertEqual(backup.execute('SELECT count(*) FROM passkeys').fetchone()[0], 2)

    def test_credential_failure_is_retained_after_rollback_and_reopen(self):
        original = self.migration.probe
        def probe(target):
            values = original(target)
            if self.migration.dropin.exists():
                values.update(ExecMainStatus='243', Result='exit-code')
            return values
        self.migration.probe = probe
        self.migration.health = lambda *args: (_ for _ in ()).throw(ManagementError('fixture failure')) if self.migration.dropin.exists() else None
        self.start()
        self.assertIn('243/CREDENTIALS', self.migration.error)
        self.assertEqual(self.migration.record()['phase'], 'rolled-back')
        self.assertIn('243/CREDENTIALS', self.new_migration().status()['error'])
        self.assert_credentials()

    def test_credential_directive_uses_plain_standard_paths(self):
        self.assertEqual(credential_line('words', '/etc/rdp-auth/words.json'),
                         'LoadCredential=words:/etc/rdp-auth/words.json\n')
        for path in ('/tmp/中文 100%/words.json', '/tmp/words\nExecStart=/bin/false'):
            with self.assertRaises(ManagementError):
                credential_line('words', path)

    def test_custom_wordlist_path_is_staged_under_standard_backup_path(self):
        from management import save_config
        source = self.root / '中文 有空格 100%'
        source.mkdir()
        words = source / 'words.json'
        words.write_bytes((self.target.runtime / 'wordlists/objects.json').read_bytes())
        save_config(self.target, dict(wordlist_path=str(words)))
        self.before = self.target.config.read_bytes()
        self.start()
        self.assertEqual(self.migration.phase, 'complete', self.migration.error)
        self.assertNotIn(str(words), self.migration.dropin.read_text())
        self.assertIn(str(Path(self.migration.record()['backup']) / 'objects.json'), self.migration.dropin.read_text())
        self.assert_credentials()


class DetectionTests(ManagementFixture):
    def test_fresh_and_partial_installations(self):
        self.assertEqual(installed_targets(self.root), [])
        (self.root / 'etc/rdp-auth').mkdir(parents=True)
        targets = installed_targets(self.root)
        self.assertEqual([t.profile for t in targets], ['legacy'])
        self.assertEqual(preferred_target(targets, lambda t: {}), targets[0])

    def test_two_layouts_prefer_running_legacy(self):
        for name in ('rdp-auth', 'rdp-access-auth'):
            (self.root / 'etc' / name).mkdir(parents=True)
        targets = installed_targets(self.root)
        selected = preferred_target(targets, lambda t: dict(ActiveState='active' if t.profile == 'legacy' else 'inactive'))
        self.assertEqual(selected.profile, 'legacy')

    def test_launcher_routes_fresh_and_existing_hosts(self):
        import rdp_manager
        for profile in (None, 'legacy', 'system'):
            target = Target(self.root/'config', self.root/'state', 'fixture.service', self.root, profile) if profile else None
            with patch('deploy.elevate_if_needed'), patch('installation.installed_targets', return_value=[target] if target else []), patch('management_web.run_gui') as gui, patch('deploy.run_wizard') as deploy:
                self.assertEqual(rdp_manager.main(['launch', '--no-open']), 0)
                if target:
                    gui.assert_called_once_with(target, 18124, False, view='open', fallback_port=True)
                    deploy.assert_not_called()
                else:
                    deploy.assert_called_once_with(True, 18124, True)
                    gui.assert_not_called()


if __name__ == '__main__':
    unittest.main()
