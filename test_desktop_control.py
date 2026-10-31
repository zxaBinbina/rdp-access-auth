"""Exercise the real Unix transport and provisioning against bounded API fixtures."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from desktop.control import Controller, Client, make_server
from desktop.mcp import MCP, read_identity
from desktop.providers import Cloudflare, validate_frpc_config, EXTRA
from desktop.provisioning import Provisioner, write_json
from management import ManagementError

PASSWORD = ' Local-management-123! '
ACCOUNT, ZONE, TUNNEL = 'a' * 32, 'b' * 32, '12345678-1234-1234-1234-123456789abc'
FRPC = '[common]\nserver_addr = node.example.test\nserver_port = 7000\ntoken = fixture-secret\n[rdp]\ntype = tcp\nlocal_ip = 127.0.0.1\nlocal_port = 13389\nauth_mode = server\nauth_time = 6h\nproxy_protocol_version = v2\n'


class ApiFixture:
    def __init__(self):
        self.calls, self.sakura, self.tunnels, self.dns = [], [], [], []
        self.config = None
        self.fail_config = False

    def __call__(self, provider, method, path, token, data=None, text=False):
        self.calls.append((provider, method, path, data))
        if provider == 'sakura':
            if path == '/user/info': return {'name': 'Fixture', 'token': 'DO-NOT-LEAK', 'group': {'level': 0}}
            if path == '/nodes': return {'1': {'name': 'Node', 'host': 'node.example.test', 'flag': 4}}
            if path == '/tunnels':
                if method == 'POST':
                    self.sakura.append({**data, 'id': 101, 'remote': 34567})
                    return self.sakura[-1]
                return self.sakura
            if path == '/tunnel/edit':
                self.sakura[0].update(data)
                return True
            if path == '/tunnel/config': return FRPC
        else:
            value = None
            if path.startswith('/zones?'): value = [{'id': ZONE, 'name': 'example.test', 'account': {'id': ACCOUNT, 'name': 'Fixture'}}]
            elif '/dns_records' in path:
                if method == 'POST':
                    self.dns.append({**data, 'id': 'dns-id'})
                    value = self.dns[-1]
                else: value = self.dns
            elif path.endswith('/configurations'):
                if method == 'PUT':
                    if self.fail_config:
                        self.fail_config = False
                        raise ManagementError('模拟平台暂时不可用。', 503)
                    self.config = data
                value = self.config
            elif path.endswith('/token'): value = 'cloudflare-runtime-token-fixture'
            elif '/cfd_tunnel' in path:
                if method == 'POST':
                    self.tunnels.append({'id': TUNNEL, 'name': data['name']})
                    value = self.tunnels[-1]
                else: value = self.tunnels
            else: raise AssertionError((method, path))
            return {'success': True, 'result': value, 'result_info': {'total_pages': 1}}
        raise AssertionError((provider, method, path))


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.api = ApiFixture()
        self.commands = []
        self.now = 10000
        def download(path, progress): path.write_bytes(b'fixture executable, never run')
        def words(path, **kwargs):
            file = path / 'source-words.json'
            file.write_text(json.dumps(['测试' + chr(0x4e00 + n) for n in range(2048)]))
            return file
        self.controller = Controller(self.root, lambda args: self.commands.append(args) or '', clock=lambda: self.now,
            provisioner_factory=lambda c: Provisioner(c, self.api, download, download, words, lambda *args: None))
        self.uid = os.getuid() or 1000
        self.peer = {'uid': self.uid, 'pid': os.getpid()}
        self.controller.authority.initialize(self.uid, PASSWORD)
        self.session = self.controller.authority.login(self.peer, PASSWORD)

    def call(self, method, params=None, identity=None, session=True):
        return self.controller.execute(self.peer, {'method': method, 'params': params or {}, 'identity': identity,
            'session': self.session if session else None})

    def setup_plan(self):
        self.call('onboarding.inspect', {'sakura_api_token': 'sakura-fixture-token', 'cloudflare_api_token': 'cf-fixture-token'})
        self.call('onboarding.prepare', {'mode': 'automatic', 'hostname': 'auth.example.test',
            'cf_account_id': ACCOUNT, 'cf_zone_id': ZONE, 'sakura_node_id': 1, 'password': 'Remote-password-1234!'})
        self.controller.provisioner.worker.join(10)
        state = self.call('onboarding.status')
        self.assertEqual(state['phase'], 'ready', state)
        return state

    def test_locked_owner_can_read_but_cannot_mutate_or_read_secrets(self):
        self.assertTrue(self.call('status', session=False)['initialized'])
        for method, params in [('preferences.save', {'changes': {'show_tray': False}}), ('settings.secrets', {}), ('services.pause', {})]:
            with self.subTest(method=method), self.assertRaises(ManagementError):
                self.call(method, params, session=False)
        with self.assertRaises(ManagementError):
            self.controller.execute({'uid': self.uid + 1, 'pid': self.peer['pid']}, {'method': 'status'})

    @unittest.skipIf(os.getuid() == 0, 'real peer UID test requires an ordinary user')
    def test_unix_peer_pid_prevents_stolen_session_reuse(self):
        path = self.root / 'control.sock'
        server = make_server(self.controller, path)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = Client(path)
            session = client.request('security.login', {'password': PASSWORD})['session']
            client.request('preferences.save', {'changes': {'theme': 'light'}}, session=session)
            script = 'import json,sys; from desktop.control import Client; from management import ManagementError\ntry: Client(sys.argv[1]).request("preferences.save",{"changes":{"theme":"dark"}},session=sys.stdin.read())\nexcept ManagementError as e: print(e.status)'
            result = subprocess.run([sys.executable, '-c', script, str(path)], input=session, text=True, capture_output=True)
            self.assertEqual(result.stdout.strip(), '401', result.stderr)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_mcp_grant_and_dangerous_policy_are_enforced_by_controller(self):
        identity = self.controller.authority.register('Fixture Agent')
        parent = self
        class Transport:
            def request(self, method, params, identity): return parent.call(method, params, identity, session=False)
        mcp = MCP(Transport(), identity)
        def invoke(name, args=None):
            return mcp.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}})['result']
        mcp.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertTrue(invoke('update_preferences', {'changes': {'theme': 'dark'}})['isError'])
        request = self.controller.authority.summary()['pending'][0]
        self.call('agents.approve', {'request_id': request['id'], 'allow': True})
        self.assertFalse(invoke('update_preferences', {'changes': {'theme': 'light'}})['isError'])
        self.assertTrue(invoke('set_agent_policy', {'id': identity['id'], 'disabled': True})['isError'])
        self.assertFalse(self.controller.authority.data['agents'][identity['id']]['disabled'])
        self.now += 3600
        self.assertTrue(invoke('update_preferences', {'changes': {'theme': 'dark'}})['isError'])
        self.assertFalse(any(t['method'] in ('security.login', 'agents.approve') for t in __import__('desktop.mcp', fromlist=['TOOLS']).TOOLS))

    def test_provision_retry_reuses_resources_and_keeps_secrets_private(self):
        ready = self.setup_plan()
        self.api.fail_config = True
        self.call('onboarding.apply', {'revision': ready['revision']})
        self.controller.provisioner.worker.join(10)
        self.assertEqual(self.call('onboarding.status')['phase'], 'failed')
        self.call('onboarding.apply', {'revision': ready['revision']})
        self.controller.provisioner.worker.join(10)
        status = self.call('status')
        self.assertEqual(status['deployment']['phase'], 'complete', status)
        self.assertEqual(len(self.api.sakura), 1)
        self.assertEqual(len(self.api.tunnels), 1)
        self.assertEqual(len(self.api.dns), 1)
        public = json.dumps(status)
        for secret in ('DO-NOT-LEAK', 'fixture-secret', 'sakura-fixture-token', 'cf-fixture-token', 'Remote-password-1234!'):
            self.assertNotIn(secret, public)
        for name in ('frpc.ini', 'cloudflare-token', 'portal-settings.json'):
            self.assertEqual((self.root / 'etc/rdp-access-auth' / name).stat().st_mode & 0o777, 0o600)
        self.call('services.pause')
        restarted = Controller(self.root, lambda args: '')
        self.assertTrue(restarted.services.prefs['paused'])
        for unit in (self.root / 'etc/systemd/system').glob('*.service'):
            self.assertIn('ConditionPathExists=!/etc/rdp-access-auth-control/paused', unit.read_text())

    def test_conflicting_local_install_prevents_remote_mutations(self):
        ready = self.setup_plan()
        directory = self.root / 'etc/rdp-access-auth'; directory.mkdir()
        self.api.calls.clear()
        with self.assertRaises(ManagementError): self.call('onboarding.apply', {'revision': ready['revision']})
        self.assertEqual(self.api.calls, [])

    def test_rejected_api_key_keeps_previously_working_credentials(self):
        self.call('onboarding.inspect', {'sakura_api_token': 'working-fixture'})
        def reject(*args, **kwargs):
            raise ManagementError('平台拒绝凭据。', 422)
        self.controller.provisioner.transport = reject
        with self.assertRaises(ManagementError):
            self.call('onboarding.inspect', {'sakura_api_token': 'bad-fixture'})
        self.assertEqual(self.controller.provisioner.vault['sakura_api_token'], 'working-fixture')
        self.assertNotIn('bad-fixture', self.controller.provisioner.vault_path.read_text())

    def test_revoked_agent_cannot_continue_background_deployment(self):
        ready = self.setup_plan()
        identity = self.controller.authority.register('Fixture')
        request = self.controller.authority.request_grant(self.peer, identity)
        self.call('agents.approve', {'request_id': request['request_id'], 'allow': True})
        self.now += 3600
        with self.assertRaises(ManagementError):
            self.call('onboarding.apply', {'revision': ready['revision']}, identity, session=False)
        self.assertEqual(self.api.sakura, [])


class ProviderTests(unittest.TestCase):
    def test_frpc_rejects_unprotected_target_and_plugins(self):
        self.assertEqual(validate_frpc_config(FRPC, 13389), FRPC)
        for unsafe in (FRPC.replace('13389', '3389'), FRPC.replace('auth_mode = server', 'auth_mode = none'), FRPC + 'plugin = exec\n', FRPC + '[extra]\ntype=tcp\n'):
            with self.assertRaises(ManagementError): validate_frpc_config(unsafe, 13389)

    def test_dns_conflict_detected_before_route_mutation(self):
        fixture = ApiFixture(); fixture.dns = [{'id': 'old', 'type': 'A', 'content': '1.2.3.4'}]
        with self.assertRaises(ManagementError): Cloudflare('key', fixture).route(ACCOUNT, ZONE, TUNNEL, 'auth.example.test', 18089)
        self.assertFalse(any(call[1] != 'GET' for call in fixture.calls))

    def test_each_remote_write_rechecks_grant(self):
        fixture = ApiFixture(); checkpoints = []
        def checkpoint():
            checkpoints.append(1)
            if len(checkpoints) > 1: raise ManagementError('grant expired', 428)
        with self.assertRaises(ManagementError): Cloudflare('key', fixture, checkpoint).route(ACCOUNT, ZONE, TUNNEL, 'auth.example.test', 18089)
        self.assertEqual(len(fixture.dns), 1)
        self.assertIsNone(fixture.config)

    def test_identity_rejects_symlinks_and_world_readable_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'identity.json'
            write_json(path, {'id': 'a', 'key': 'secret'})
            self.assertEqual(read_identity(path)['id'], 'a')
            path.chmod(0o644)
            with self.assertRaises(ManagementError): read_identity(path)
            link = path.with_name('link'); link.symlink_to(path)
            with self.assertRaises(ManagementError): read_identity(link)
            path.chmod(0o600); path.write_text('[]')
            with self.assertRaises(ManagementError): read_identity(path)
