"""Credential-protected local RPC over Unix sockets; never bind a TCP port."""
import json
import os
from pathlib import Path
import socket
import socketserver
import struct
import threading

from management import (ManagementError, Target, public_config, read_config, save_config,
                        snapshot, state_operation)
from desktop.security import Authority
from desktop.services import Services, run

SOCKET_PATH = '/run/rdp-access-auth-control.sock'
MAX_MESSAGE = 2 * 1024 * 1024


class Client:
    def __init__(self, path=SOCKET_PATH, timeout=90):
        self.path, self.timeout = str(path), timeout

    def request(self, method, params=None, *, session=None, identity=None, confirmation=None):
        data = {'method': method, 'params': params or {}, 'session': session, 'identity': identity, 'confirmation': confirmation}
        payload = json.dumps(data, ensure_ascii=False).encode() + b'\n'
        if len(payload) > MAX_MESSAGE:
            raise ManagementError('提交内容超过大小限制。')
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.path)
                connection.sendall(payload)
                with connection.makefile('rb') as stream:
                    response = stream.readline(MAX_MESSAGE + 1)
            if len(response) > MAX_MESSAGE:
                raise ManagementError('后台返回内容超过大小限制。', 502)
            result = json.loads(response)
        except (OSError, ValueError):
            raise ManagementError('无法连接本机管理服务。请在欢迎向导中初始化，或检查软件包是否安装完整。', 503) from None
        if 'error' in result:
            raise ManagementError(result['error']['message'], result['error']['code'])
        return result['result']


class Controller:
    def __init__(self, root=Path('/'), runner=run, clock=None, provisioner_factory=None):
        self.root = Path(root)
        self.authority = Authority(self.root / 'etc/rdp-access-auth-control', **({'clock': clock} if clock else {}))
        self.services = Services(self.root, runner)
        self.lock = threading.RLock()
        self.provisioner_factory = provisioner_factory
        self._provisioner = None
        self._migration = None

    @property
    def target(self):
        legacy = (self.root / 'etc/rdp-auth/portal-settings.json').is_file() and not (self.root / 'etc/rdp-access-auth/portal-settings.json').is_file()
        name = 'rdp-auth' if legacy else 'rdp-access-auth'
        return Target(self.root / f'etc/{name}/portal-settings.json', self.root / f'var/lib/{name}/state.sqlite3',
                      name + '.service', self.root / ('usr/local/lib/rdp-auth' if legacy else 'usr/lib/rdp-access-auth'),
                      'legacy' if legacy else 'system')

    @property
    def provisioner(self):
        if self._provisioner is None:
            if self.provisioner_factory:
                self._provisioner = self.provisioner_factory(self)
            else:
                from desktop.provisioning import Provisioner
                self._provisioner = Provisioner(self)
        return self._provisioner

    def status(self):
        result = {'initialized': self.authority.data is not None, 'configured': self.services.installed(),
                  'version': (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip(),
                  'preferences': dict(self.services.prefs)}
        if result['initialized']:
            result['services'] = self.services.status()
            result['pending_count'] = len(self.authority.summary()['pending'])
            try:
                value, revision = read_config(self.target.config, missing_ok=True)
                result.update(config=public_config(value), revision=revision)
            except ManagementError as exc:
                result['config_error'] = str(exc)
            result['deployment'] = self.provisioner.status()
        return result

    def execute(self, peer, request):
        if not isinstance(request, dict):
            raise ManagementError('请求必须为对象。')
        method, params = request.get('method'), request.get('params', {})
        if not isinstance(method, str) or len(method) > 80 or not isinstance(params, dict):
            raise ManagementError('请求格式无效。')
        session, identity = request.get('session'), request.get('identity')
        with self.lock:
            if self.authority.data is None and self.authority.path.exists():
                self.authority.data = json.loads(self.authority.path.read_text())
            if method == 'status' and self.authority.data is None:
                return self.status()
            self.authority.owner(peer)
            if identity is not None:
                self.authority.agent(peer, identity)
            if method == 'status':
                return self.status()
            if method == 'security.login':
                if identity is not None:
                    raise ManagementError('Agent 不能代替用户输入管理密码。', 403)
                return {'session': self.authority.login(peer, params.get('password'))}
            if method == 'security.logout':
                self.authority.logout(peer, session)
                return {'locked': True}
            if method == 'security.confirm':
                return {'confirmation': self.authority.confirm(peer, session, params.get('password'),
                                                              params.get('method'), params.get('params', {}))}
            if method == 'agents.request':
                return self.authority.request_grant(peer, identity, params.get('reason', ''))
            if method == 'agents.list':
                self.authority.human(peer, session)
                return self.authority.summary()
            if method == 'agents.approve':
                return self.authority.approve(peer, session, params.get('request_id'), params.get('allow'), params.get('password'))
            if method in ('settings.read', 'onboarding.guide', 'onboarding.status', 'diagnostics.environment'):
                if method == 'settings.read':
                    return snapshot(self.target)
                if method == 'onboarding.guide':
                    return self.provisioner.guide()
                if method == 'onboarding.status':
                    return self.provisioner.status()
                return self.provisioner.environment()
            if method == 'lifecycle.login' and identity is None:
                # This can only start services when the owner explicitly selected login startup.
                return self.services.action('login') if self.services.installed() else {'message': '尚未完成部署。'}
            actor = self.authority.authorize(peer, method, params, session, identity, request.get('confirmation'))
            try:
                result = self.mutate(method, params, peer, session, identity)
            except ManagementError:
                self.authority.record(actor, method, 'failed', params.keys())
                raise
            self.authority.record(actor, method, fields=params.keys())
            return result

    def mutate(self, method, params, peer, session, identity):
        if method == 'settings.save':
            if not isinstance(params.get('revision'), str):
                raise ManagementError('请先读取当前配置版本，避免覆盖其他人的修改。', 409)
            changes = params.get('changes')
            result = save_config(self.target, changes, revision=params['revision'])
            if changes.get('sakura_token'):
                from desktop.provisioning import write_json
                self.provisioner.vault['sakura_api_token'] = changes['sakura_token']
                write_json(self.provisioner.vault_path, self.provisioner.vault)
            return result
        if method == 'settings.secrets':
            self.authority.human(peer, session)
            return snapshot(self.target, include_credentials=True)
        if method == 'preferences.save':
            return self.services.preferences(params.get('changes'))
        if method == 'connectors.sync':
            return self.provisioner.sync(lambda: self.authority.authorize(peer, method, params, session, identity))
        if method.startswith('services.'):
            return self.services.action(method.split('.', 1)[1], params.get('disable_autostart', False))
        if method == 'logs.read':
            values, _ = read_config(self.target.config, missing_ok=True)
            return self.services.logs(params.get('service', 'portal'), self.provisioner.secrets() + list(values.values()))
        if method == 'security.password':
            self.authority.change_password(params.get('password'))
            return {'message': '管理密码已更改，窗口已锁定，所有 Agent 临时授权已失效。', 'locked': True}
        if method == 'agents.register':
            return self.authority.register(params.get('name'))
        if method in ('agents.remove', 'agents.revoke'):
            self.authority.revoke(params.get('id'), remove=method == 'agents.remove')
            return {'message': 'Agent 身份已移除。' if method == 'agents.remove' else 'Agent 的临时授权已撤销。'}
        if method == 'agents.policy':
            agent = self.authority.data['agents'].get(params.get('id'))
            if not agent or type(params.get('disabled')) is not bool:
                raise ManagementError('Agent 或授权规则无效。')
            agent['disabled'] = params['disabled']
            self.authority.revoke(params['id'])
            return {'message': 'Agent 授权规则已更改。'}
        if method == 'credentials.unlock':
            return state_operation(self.target, unlock=True)
        if method == 'credentials.delete_passkey':
            return state_operation(self.target, delete_passkey=params.get('id'))
        if method in ('credentials.temporary', 'credentials.regenerate'):
            from auth_credentials import TemporaryPasswords
            value, _ = read_config(self.target.config)
            words = value.get('wordlist_path', str(self.target.runtime / 'wordlists/objects.json'))
            temporary = TemporaryPasswords(self.target.state, words, value['session_key'])
            if method == 'credentials.temporary':
                self.authority.human(peer, session)
                return {'temporary': temporary.current()}
            return {'temporary': temporary.regenerate(params.get('expected', ''))}
        if method.startswith('onboarding.'):
            action = method.split('.', 1)[1]
            if action == 'inspect':
                return self.provisioner.inspect(params)
            if action == 'prepare':
                return self.provisioner.prepare(params)
            if action == 'apply':
                return self.provisioner.apply(params, lambda: self.authority.authorize(peer, method, params, session, identity))
        if method.startswith('migration.'):
            from migration import Migration
            if self._migration is None:
                self._migration = Migration(self.target)
            if method == 'migration.status':
                return self._migration.status()
            if method == 'migration.plan':
                return self._migration.plan()
            if method == 'migration.start':
                self._migration.start(params.get('revision'), recover=params.get('recover', False))
                return self._migration.status()
        raise ManagementError('不支持此操作：' + method, 404)


def make_server(controller, path=SOCKET_PATH, inherited_socket=None):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.connection.settimeout(100)
            pid, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            raw = self.rfile.readline(MAX_MESSAGE + 1)
            try:
                if not raw.endswith(b'\n') or len(raw) > MAX_MESSAGE:
                    raise ManagementError('请求超过限制或未完整发送。')
                result = controller.execute({'uid': uid, 'pid': pid}, json.loads(raw))
                response = {'result': result}
            except ManagementError as exc:
                response = {'error': {'code': exc.status, 'message': str(exc)}}
            except (ValueError, TypeError):
                response = {'error': {'code': 400, 'message': '请求数据格式无效。'}}
            except Exception:
                # Exception details may contain credentials; do not send or print them.
                response = {'error': {'code': 500, 'message': '操作未完成，请检查配置和运行环境后重试。'}}
            try:
                self.wfile.write(json.dumps(response, ensure_ascii=False).encode() + b'\n')
            except (BrokenPipeError, ConnectionResetError):
                pass

    class Server(socketserver.ThreadingUnixStreamServer):
        daemon_threads = True
        request_queue_size = 16

    if inherited_socket is not None:
        server = Server(str(path), Handler, bind_and_activate=False)
        server.socket.close()
        server.socket = inherited_socket
        server.server_address = str(path)
    else:
        server = Server(str(path), Handler)
        os.chmod(path, 0o600)
    return server


def serve():
    if os.geteuid() != 0 or os.environ.get('LISTEN_PID') != str(os.getpid()) or os.environ.get('LISTEN_FDS') != '1':
        raise ManagementError('管理后台必须由安装包提供的 systemd socket 启动。', 403)
    controller = Controller()
    listener = socket.socket(fileno=3)
    with make_server(controller, inherited_socket=listener) as server:
        server.serve_forever()


def bootstrap(password):
    if os.geteuid() != 0:
        raise ManagementError('首次初始化需要 Linux 管理员认证。', 403)
    uid = int(os.environ.get('PKEXEC_UID') or os.environ.get('SUDO_UID') or 0)
    authority = Authority(Path('/etc/rdp-access-auth-control'))
    if authority.data is None:
        authority.initialize(uid, password)
    elif authority.data['owner_uid'] != uid:
        raise ManagementError('此软件已由其他 Linux 用户初始化。', 403)
    run(['systemctl', 'enable', '--now', 'rdp-access-auth-control.socket'])
    return {'initialized': True}
