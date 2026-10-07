"""Offline management tests: temporary data only, no installed services are contacted."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from auth_guard import AuthGuard
from management import (ManagementError, Target, check_wordlist,
                        read_config, save_config, select_target, service_operation,
                        snapshot, state_operation)
from management_web import create_server

ROOT = Path(__file__).resolve().parent


class ManagementFixture(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.target = Target(self.root / 'private/settings.json', self.root / 'state.sqlite3',
                             'rdp-test.service', self.root)
        self.fields = dict(hostname='auth.example.test', rdp_address='desktop.example.test:3389',
                           tunnel_id=12345, password='Testing-password-1234!', sakura_token='test-sakura-token')

    def create(self):
        return save_config(self.target, self.fields, revision='missing', create=True)

    def make_state(self):
        guard = AuthGuard(self.target.state)
        with closing(sqlite3.connect(self.target.state)) as db, db:
            db.execute('CREATE TABLE passkeys (id TEXT PRIMARY KEY, public_key TEXT, name TEXT, created INTEGER)')
            db.execute("INSERT INTO passkeys VALUES ('key-1', 'keep-this-key', '测试密钥', 1)")
            db.execute('CREATE TABLE temporary_password (id INTEGER PRIMARY KEY, generation INTEGER, ciphertext TEXT)')
            db.execute("INSERT INTO temporary_password VALUES (1, 7, 'keep-encrypted-password')")
            db.execute('UPDATE guard_global SET until=?', (int(time.time()) + 900,))
            db.execute("INSERT INTO guard_ips VALUES ('1.1.1.1', 5, ?)", (int(time.time()) + 900,))
        return guard


class ManagementTests(ManagementFixture):
    def test_create_redacts_and_never_overwrites(self):
        result = self.create()
        raw = self.target.config.read_text()
        value, revision = read_config(self.target.config)
        self.assertEqual(stat.S_IMODE(self.target.config.stat().st_mode), 0o600)
        self.assertEqual(len(value['session_key']), 64)
        self.assertNotIn(self.fields['password'], raw)
        serialized = json.dumps(result)
        for field in ('password_hash', 'password_salt', 'session_key', 'sakura_token'):
            self.assertNotIn(value[field], serialized)
        with self.assertRaises(ManagementError) as exc:
            self.create()
        self.assertEqual(exc.exception.status, 409)
        self.assertEqual(read_config(self.target.config)[1], revision)

    def test_update_preserves_credentials_unknown_fields_and_backup(self):
        self.create()
        before, _ = read_config(self.target.config)
        before['future_setting'] = {'preserve': True}
        self.target.config.write_text(json.dumps(before))
        old_bytes = self.target.config.read_bytes()
        save_config(self.target, dict(rdp_address='new.example.test:3390', sakura_token='', password=''))
        after, _ = read_config(self.target.config)
        for key in ('password_salt', 'password_hash', 'session_key', 'sakura_token', 'future_setting'):
            self.assertEqual(after[key], before[key])
        backup = Path(str(self.target.config) + '.bak')
        self.assertEqual(backup.read_bytes(), old_bytes)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        self.assertEqual(after['rdp_address'], 'new.example.test:3390')

    def test_concurrent_updates_detect_stale_revision(self):
        revision = self.create()['revision']
        barrier = threading.Barrier(2)
        def update(i):
            barrier.wait(timeout=5)
            try:
                save_config(self.target, {'tunnel_id': i}, revision=revision)
                return 200
            except ManagementError as exc:
                return exc.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(update, [12, 13])), [200, 409])
        self.assertIn(read_config(self.target.config)[0]['tunnel_id'], (12, 13))

    def test_validation_never_changes_previous_config(self):
        self.create()
        original = self.target.config.read_bytes()
        for changes in [dict(hostname='https://auth.example.com'), dict(hostname='a..example.com'),
                        dict(rdp_address='host:99999'), dict(rdp_address='x.test:0'), dict(tunnel_id=True),
                        dict(tunnel_id='123'), dict(password='short'), dict(password=12),
                        dict(sakura_token='with whitespace'), dict(sakura_token=None),
                        dict(turnstile_site_key='only-site'), dict(wordlist_path='relative.json'),
                        dict(session_key='replace-key'), dict(disable_turnstile='true')]:
            with self.subTest(changes=changes), self.assertRaises(ManagementError):
                save_config(self.target, changes)
            self.assertEqual(self.target.config.read_bytes(), original)

    def test_turnstile_enable_retain_and_disable(self):
        self.create()
        save_config(self.target, dict(turnstile_site_key='site', turnstile_secret_key='secret'))
        save_config(self.target, dict(turnstile_site_key='site2', turnstile_secret_key=''))
        self.assertEqual(read_config(self.target.config)[0]['turnstile_secret_key'], 'secret')
        save_config(self.target, dict(disable_turnstile=True))
        value, _ = read_config(self.target.config)
        self.assertEqual(value['turnstile_secret_key'], '')
        self.assertEqual(value['turnstile_site_key'], '')

    def test_changing_password_preserves_session_key(self):
        self.create()
        before, _ = read_config(self.target.config)
        save_config(self.target, dict(password='New-password-test-4321!'))
        after, _ = read_config(self.target.config)
        self.assertNotEqual(after['password_hash'], before['password_hash'])
        self.assertEqual(after['session_key'], before['session_key'])

    def test_corrupt_and_symlink_configs_are_not_overwritten(self):
        self.target.config.parent.mkdir()
        self.target.config.write_text('{broken')
        with self.assertRaises(ManagementError):
            save_config(self.target, self.fields, create=True)
        self.assertEqual(self.target.config.read_text(), '{broken')
        self.target.config.unlink()
        destination = self.root / 'elsewhere.json'
        destination.write_text('{}')
        self.target.config.symlink_to(destination)
        with self.assertRaises(ManagementError):
            save_config(self.target, self.fields, create=True)
        self.assertEqual(destination.read_text(), '{}')

    def test_missing_status_does_not_create_database_or_config(self):
        result = snapshot(self.target)
        self.assertFalse(result['exists'])
        self.assertIn('state_error', result)
        with self.assertRaises(ManagementError):
            state_operation(self.target, unlock=True)
        self.assertFalse(self.target.state.exists())
        self.assertFalse(self.target.config.parent.exists())

    def test_unlock_preserves_credentials_and_is_transactional(self):
        self.make_state()
        before = state_operation(self.target)
        self.assertEqual(before['banned_ips'], 1)
        after = state_operation(self.target, unlock=True)
        self.assertEqual(after['banned_ips'], 0)
        self.assertEqual(after['global_lock_seconds'], 0)
        self.assertEqual(after['temporary_generation'], 7)
        self.assertEqual(after['passkeys'], 1)
        with closing(sqlite3.connect(self.target.state)) as db:
            self.assertEqual(db.execute('SELECT ciphertext FROM temporary_password').fetchone()[0], 'keep-encrypted-password')
            self.assertEqual(db.execute('SELECT public_key FROM passkeys').fetchone()[0], 'keep-this-key')

    def test_wordlist_validation(self):
        path = self.root / 'words.json'
        value = dict(wordlist_path=str(path))
        self.assertFalse(check_wordlist(self.target, value)['ok'])
        self.assertFalse(check_wordlist(self.target, {'wordlist_path': ['not-a-path']})['ok'])
        for invalid in ({'word': '词语'}, ['一个词'] * 2048, [1] * 2048):
            path.write_text(json.dumps(invalid))
            self.assertFalse(check_wordlist(self.target, value)['ok'])
        path.write_text(json.dumps(['测试' + chr(0x4e00 + i) for i in range(2048)]))
        self.assertEqual(check_wordlist(self.target, value)['count'], 2048)

    def test_local_admission_config_validation_and_cli(self):
        self.create()
        before, _ = read_config(self.target.config)
        result = subprocess.run([str(ROOT / 'rdp-auth'), '--config', str(self.target.config), 'config', 'set',
                                 '--admission-listen-port', '13389', '--admission-target-port', '3389',
                                 '--admission-duration', '3600'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        after, revision = read_config(self.target.config)
        self.assertEqual(after['local_admission'], dict(listen_port=13389, target_port=3389, duration_seconds=3600))
        self.assertEqual(after['session_key'], before['session_key'])
        self.assertEqual(after['password_hash'], before['password_hash'])
        for value in (True, {'target_port': 13389}, {'listen_port': 80}, {'duration_seconds': 0}):
            with self.assertRaises(ManagementError):
                save_config(self.target, {'local_admission': value}, revision=revision)
        self.assertEqual(read_config(self.target.config)[1], revision)

    def test_local_profile_never_contacts_systemd(self):
        with patch('management.subprocess.run') as run:
            self.assertEqual(service_operation(self.target)['LoadState'], 'local')
            for action in ('start', 'stop', 'restart', 'logs'):
                with self.assertRaises(ManagementError):
                    service_operation(self.target, action)
            snapshot(self.target)
            run.assert_not_called()
        self.assertEqual(select_target().profile, 'local')

    def test_systemd_allowlist_and_failure_reporting(self):
        target = Target(self.target.config, self.target.state, 'rdp-test.service', self.root, 'system')
        with patch('management.subprocess.run') as run:
            run.return_value = subprocess.CompletedProcess([], 0, 'LoadState=loaded\nActiveState=active\nWorkingDirectory=/usr/lib/rdp-access-auth\n')
            status = service_operation(target)
            self.assertEqual(status['ActiveState'], 'active')
            self.assertEqual(status['WorkingDirectory'], '/usr/lib/rdp-access-auth')
            self.assertIn('--property=LoadState,ActiveState,SubState,WorkingDirectory', run.call_args.args[0])
            service_operation(target, 'restart')
            self.assertEqual(run.call_args.args[0], ['systemctl', '--no-ask-password', 'restart', 'rdp-test.service'])
            with self.assertRaises(ManagementError):
                service_operation(target, 'restart; rm -rf /')
            run.return_value = subprocess.CompletedProcess([], 1, '', 'permission denied')
            with self.assertRaisesRegex(ManagementError, '权限'):
                service_operation(target, 'restart')
        with self.assertRaises(ManagementError):
            select_target(service='--bad.service')

    def test_cli_launcher_outside_project_and_redacted_output(self):
        self.create()
        result = subprocess.run([str(ROOT / 'rdp-auth'), '--config', str(self.target.config), 'config', 'show'],
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['config']['hostname'], 'auth.example.test')
        self.assertNotIn(self.fields['sakura_token'], result.stdout)
        result = subprocess.run([str(ROOT / 'rdp-auth'), '--config', str(self.target.config), 'config', 'set',
                                 '--rdp-address', 'new.example.test:3390'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(read_config(self.target.config)[0]['rdp_address'], 'new.example.test:3390')
        result = subprocess.run([str(ROOT / 'rdp-auth'), '--config', str(self.target.config), 'config', 'set', '--password'],
                                input='', capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('Traceback', result.stderr)


class ManagementHTTPTests(ManagementFixture):
    def setUp(self):
        super().setUp()
        self.server = create_server(self.target, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.port = self.server.server_port
        self.token = self.server.management_url.split('#token=')[1]
        self.origin = f'http://127.0.0.1:{self.port}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, method='GET', path='/api/status', data=None, headers=None, authenticated=True):
        all_headers = {'Authorization': 'Bearer ' + self.token} if authenticated else {}
        if data is not None:
            all_headers['Content-Type'] = 'application/json'
        all_headers.update(headers or {})
        with closing(http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)) as client:
            client.request(method, path, body=json.dumps(data) if data is not None else None, headers=all_headers)
            response = client.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())

    def test_web_transport_guards_and_assets(self):
        status, body, headers = self.request(path='/', authenticated=False)
        self.assertEqual(status, 200)
        self.assertNotIn(self.token, body)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        for headers in ({'Origin': 'https://evil.example'}, {'Origin': 'null'}, {'Host': 'evil.example'},
                        {'Host': f'127.0.0.1:{self.port + 1}'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request(headers=headers)[0], 403)
            self.assertEqual(self.request('POST', '/api/config', {}, headers)[0], 403)
        self.assertEqual(self.request(authenticated=False)[0], 401)
        self.assertEqual(self.request(headers={'Authorization': 'Bearer wrong'})[0], 401)
        self.assertEqual(self.request(path='/../management.py')[0], 404)
        self.assertEqual(self.request(headers={'Origin': self.origin})[0], 200)

    def test_web_config_save_read_and_conflict(self):
        status, body, _ = self.request('POST', '/api/config', {'revision': 'missing', 'changes': self.fields})
        self.assertEqual(status, 200, body)
        result = json.loads(body)
        self.assertNotIn(self.fields['sakura_token'], body)
        self.assertNotIn(self.fields['password'], body)
        status, body, _ = self.request()
        self.assertTrue(json.loads(body)['exists'])
        self.assertNotIn(self.fields['sakura_token'], body)
        status, _, _ = self.request('POST', '/api/config', {'revision': result['revision'], 'changes': {'tunnel_id': 77}})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('POST', '/api/config', {'revision': result['revision'], 'changes': {'tunnel_id': 78}})[0], 409)
        self.assertEqual(read_config(self.target.config)[0]['tunnel_id'], 77)

    def test_web_rejects_invalid_bodies_and_commands(self):
        for data in ([], None, 'text', {'changes': {}}, {'revision': 'missing', 'changes': {'session_key': 'bad'}}):
            self.assertGreaterEqual(self.request('POST', '/api/config', data)[0], 400)
        self.assertEqual(self.request('POST', '/api/service', {'action': ['restart']})[0], 400)
        self.assertEqual(self.request('POST', '/api/service', {'action': 'restart'})[0], 400)
        self.assertEqual(self.request('POST', '/api/config', {'large': 'x' * 40000})[0], 413)
        self.assertFalse(self.target.config.exists())

    def test_editor_reads_existing_tokens_without_password_or_session_secrets(self):
        self.create()
        save_config(self.target, dict(turnstile_site_key='site', turnstile_secret_key='saved-secret'))
        _, revision = read_config(self.target.config)
        status, body, headers = self.request(path='/api/config')
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual(result['revision'], revision)
        self.assertEqual(result['config']['sakura_token'], self.fields['sakura_token'])
        self.assertEqual(result['config']['turnstile_secret_key'], 'saved-secret')
        self.assertTrue(result['config']['has_password'])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        for key in ('password', 'password_hash', 'password_salt', 'session_key'):
            self.assertNotIn(key, result['config'])
        self.assertNotIn(self.fields['password'], body)
        self.assertEqual(self.request(path='/api/config', authenticated=False)[0], 401)
        for headers in ({'Origin': 'https://evil.example'}, {'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(self.request(path='/api/config', headers=headers)[0], 403)
        _, body, _ = self.request()
        self.assertNotIn(self.fields['sakura_token'], body)
        self.assertNotIn('saved-secret', body)

    def test_web_unbind_requires_admin_and_preserves_other_credentials(self):
        self.make_state()
        self.assertEqual(self.request('POST', '/api/passkeys/delete', {'key_id':'key-1'}, authenticated=False)[0], 401)
        self.assertEqual(self.request('POST', '/api/passkeys/delete', {'key_id':'key-1'}, {'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.request('POST', '/api/passkeys/delete', {})[0], 400)
        before = state_operation(self.target)
        self.assertEqual(before['passkey_list'], [{'id':'key-1', 'name':'测试密钥'}])
        self.assertEqual(self.request('POST', '/api/passkeys/delete', {'key_id':'key-1'})[0], 200)
        after = state_operation(self.target)
        self.assertEqual(after['passkeys'], 0)
        self.assertEqual(after['temporary_generation'], before['temporary_generation'])
        self.assertEqual(after['banned_ips'], before['banned_ips'])
        self.assertEqual(self.request('POST', '/api/passkeys/delete', {'key_id':'key-1'})[0], 404)

    def test_web_unlock_real_database(self):
        self.make_state()
        self.assertEqual(self.request('POST', '/api/unlock', {}, authenticated=False)[0], 401)
        self.assertEqual(state_operation(self.target)['banned_ips'], 1)
        status, body, _ = self.request('POST', '/api/unlock', {})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)['state']['banned_ips'], 0)
        self.assertEqual(state_operation(self.target)['temporary_generation'], 7)


if __name__ == '__main__':
    unittest.main()
