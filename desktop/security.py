"""Controller-owned authorization, independent of either UI or MCP transport."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import threading
import time

from management import ManagementError, atomic_private_write

GRANT_SECONDS = 3600
DANGEROUS = frozenset({'security.password', 'agents.register', 'agents.remove', 'agents.revoke', 'agents.policy', 'configuration.reset'})


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_record(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ManagementError('管理密码需要 12～128 个字符，请使用独立于远程访问密码的密码。')
    salt = secrets.token_hex(16)
    return {'salt': salt, 'hash': hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()}


def matches_password(password, record):
    if not isinstance(password, str) or len(password) > 128:
        return False
    computed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(record['salt']), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(computed, record['hash'])


def action_digest(method, params):
    return digest(json.dumps([method, params], ensure_ascii=False, sort_keys=True, separators=(',', ':')))


class Authority:
    def __init__(self, directory, clock=time.time):
        self.directory = Path(directory)
        self.path = self.directory / 'authority.json'
        self.clock = clock
        self.lock = threading.RLock()
        self.sessions = {}
        self.confirmations = {}
        self.pending = {}
        self.failures = {}
        self.data = json.loads(self.path.read_text()) if self.path.exists() else None

    def initialize(self, uid, password):
        with self.lock:
            if self.data is not None or self.path.exists():
                raise ManagementError('管理账户已经初始化，请解锁已有账户。', 409)
            if type(uid) is not int or uid <= 0:
                raise ManagementError('请选择实际使用软件的 Linux 桌面用户。')
            record = password_record(password)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.chmod(0o700)
            self.data = {'version': 2, 'owner_uid': uid, 'password': record, 'agents': {}, 'audit': []}
            self.save()

    def save(self):
        atomic_private_write(self.path, (json.dumps(self.data, ensure_ascii=False, indent=2) + '\n').encode())

    def owner(self, peer):
        if self.data is None:
            raise ManagementError('请先打开软件完成管理账户初始化。', 428)
        if peer['uid'] != self.data['owner_uid']:
            raise ManagementError('当前 Linux 用户没有管理此实例的权限。', 403)

    def check_password(self, peer, password):
        self.owner(peer)
        now = self.clock()
        failure = self.failures.get(peer['uid'], {'count': 0, 'until': 0})
        if failure['until'] > now:
            raise ManagementError('管理密码连续错误，请在 5 分钟后重试。', 429)
        if not matches_password(password, self.data['password']):
            count = failure['count'] + 1 if failure['until'] == 0 else 1
            self.failures[peer['uid']] = {'count': count, 'until': now + 300 if count >= 5 else 0}
            raise ManagementError('管理密码不正确。', 401)
        self.failures.pop(peer['uid'], None)

    def login(self, peer, password):
        with self.lock:
            self.check_password(peer, password)
            token = secrets.token_urlsafe(32)
            # A desktop session is bound to the actual Linux process, not a claimed UI role.
            self.sessions[digest(token)] = dict(peer)
            return token

    def human(self, peer, token):
        self.owner(peer)
        if not isinstance(token, str) or self.sessions.get(digest(token)) != peer:
            raise ManagementError('请先输入管理密码解锁当前窗口。', 401)

    def logout(self, peer, token):
        with self.lock:
            self.human(peer, token)
            self.sessions.pop(digest(token), None)

    def agent(self, peer, identity):
        self.owner(peer)
        if not isinstance(identity, dict):
            raise ManagementError('请从软件的 Agent 页面登记并配置 MCP 身份。', 401)
        agent_id, key = identity.get('id'), identity.get('key')
        agent = self.data['agents'].get(agent_id) if isinstance(agent_id, str) else None
        if (not agent or agent.get('disabled') or not isinstance(key, str) or
                not hmac.compare_digest(digest(key), agent['key_hash'])):
            raise ManagementError('Agent 身份无效或已被撤销。', 401)
        return agent_id, agent

    def record(self, actor, operation, outcome='ok', fields=()):
        with self.lock:
            self.data['audit'].append({'time': int(self.clock()), 'actor': actor, 'operation': operation,
                                       'outcome': outcome, 'fields': list(fields)})
            self.data['audit'] = self.data['audit'][-1000:]
            self.save()

    def request_grant(self, peer, identity, reason=''):
        with self.lock:
            self.prune()
            agent_id, agent = self.agent(peer, identity)
            now = self.clock()
            if agent.get('granted_at', 0) <= now < agent.get('expires_at', 0):
                return {'approved': True, 'expires_at': agent['expires_at'], 'remaining_seconds': int(agent['expires_at'] - now)}
            reason = str(reason)[:400]
            for request_id, pending in self.pending.items():
                if pending['agent_id'] == agent_id and pending['kind'] == 'grant' and pending['expires_at'] > now:
                    return {'approved': False, 'request_id': request_id, 'message': '等待用户在窗口或独立终端批准。'}
            request_id = secrets.token_urlsafe(16)
            self.pending[request_id] = {'kind': 'grant', 'agent_id': agent_id, 'name': agent['name'],
                                        'reason': reason, 'created': now, 'expires_at': now + 900}
            return {'approved': False, 'request_id': request_id, 'message': '请用户在软件中批准，或在独立终端运行 rdp-auth approve ' + request_id}

    def approve(self, peer, session, request_id, allow, password=None):
        with self.lock:
            self.human(peer, session)
            request = self.pending.get(request_id)
            if not request or request['expires_at'] <= self.clock():
                raise ManagementError('授权申请已过期，请让 Agent 重新申请。', 410)
            agent = self.data['agents'].get(request['agent_id'])
            if not agent or agent.get('disabled'):
                raise ManagementError('该 Agent 已被移除。', 410)
            if type(allow) is not bool:
                raise ManagementError('授权选择无效。')
            if allow and request['kind'] == 'dangerous':
                self.check_password(peer, password)
            self.pending.pop(request_id, None)
            if allow and request['kind'] == 'grant':
                agent.update(granted_at=int(self.clock()), expires_at=int(self.clock()) + GRANT_SECONDS)
            elif allow:
                # One-use approval is bound to the Agent and exact dangerous operation.
                self.confirmations[(request['agent_id'], request['fingerprint'])] = self.clock() + 120
            self.record('user', 'approve.' + request['kind'], 'allowed' if allow else 'denied')
            return {'approved': allow, 'expires_at': agent.get('expires_at', 0)}

    def confirm(self, peer, session, password, method, params):
        with self.lock:
            self.human(peer, session)
            self.check_password(peer, password)
            if method not in DANGEROUS:
                raise ManagementError('此操作不需要危险操作确认。')
            token = secrets.token_urlsafe(24)
            self.confirmations[digest(token)] = {'peer': dict(peer), 'session': digest(session),
                'fingerprint': action_digest(method, params), 'expires_at': self.clock() + 120}
            return token

    def authorize(self, peer, method, params, session=None, identity=None, confirmation=None):
        with self.lock:
            self.prune()
            if identity is not None:
                agent_id, agent = self.agent(peer, identity)
                now = self.clock()
                if method in DANGEROUS:
                    fingerprint = action_digest(method, params)
                    if self.confirmations.pop((agent_id, fingerprint), 0) > now:
                        return 'agent:' + agent_id
                    for request_id, pending in self.pending.items():
                        if (pending['agent_id'] == agent_id and pending.get('fingerprint') == fingerprint
                                and pending['expires_at'] > now):
                            raise ManagementError('危险操作需要单独批准。申请编号：' + request_id, 428)
                    request_id = secrets.token_urlsafe(16)
                    if len(self.pending) >= 64:
                        raise ManagementError('待确认操作过多，请先处理已有申请。', 429)
                    self.pending[request_id] = {'kind': 'dangerous', 'agent_id': agent_id, 'name': agent['name'],
                        'reason': method, 'fields': list(params), 'fingerprint': fingerprint,
                        'details': {key: value for key, value in params.items() if key in ('id', 'disabled', 'name') and isinstance(value, (str, bool))},
                        'created': now, 'expires_at': now + 900}
                    raise ManagementError('危险操作需要单独批准。申请编号：' + request_id, 428)
                if not agent.get('granted_at', 0) <= now < agent.get('expires_at', 0):
                    request = self.request_grant(peer, identity, method)
                    raise ManagementError(request['message'] + '，申请编号：' + request['request_id'], 428)
                return 'agent:' + agent_id
            self.human(peer, session)
            if method in DANGEROUS:
                approved = self.confirmations.pop(digest(confirmation), None) if isinstance(confirmation, str) else None
                if (not isinstance(approved, dict) or approved['peer'] != peer or approved['session'] != digest(session)
                        or approved['fingerprint'] != action_digest(method, params) or approved['expires_at'] <= self.clock()):
                    raise ManagementError('危险操作需要单独确认，确认只对当前操作有效。', 428)
            return 'user'

    def register(self, name):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(ch) < 32 for ch in name):
            raise ManagementError('Agent 名称需要 1～80 个可见字符。')
        if len(self.data['agents']) >= 32:
            raise ManagementError('最多登记 32 个 Agent，请先移除不再使用的身份。')
        agent_id, key = secrets.token_hex(12), secrets.token_urlsafe(32)
        self.data['agents'][agent_id] = {'name': name.strip(), 'key_hash': digest(key),
            'created': int(self.clock()), 'granted_at': 0, 'expires_at': 0, 'disabled': False}
        self.save()
        return {'id': agent_id, 'key': key, 'name': name.strip()}

    def revoke(self, agent_id, remove=False):
        agent = self.data['agents'].get(agent_id)
        if agent is None:
            raise ManagementError('Agent 不存在。', 404)
        if remove:
            del self.data['agents'][agent_id]
        else:
            agent.update(granted_at=0, expires_at=0)
        self.pending = {key: val for key, val in self.pending.items() if val['agent_id'] != agent_id}
        self.confirmations = {key: val for key, val in self.confirmations.items()
                              if not (isinstance(key, tuple) and key[0] == agent_id)}
        self.save()

    def change_password(self, password):
        record = password_record(password)
        self.data['password'] = record
        self.sessions.clear()
        self.confirmations.clear()
        self.pending.clear()
        for agent in self.data['agents'].values():
            agent.update(granted_at=0, expires_at=0)
        self.save()

    def summary(self):
        with self.lock:
            self.prune()
            now = self.clock()
            return {'agents': [{'id': identity, **{key: agent[key] for key in ('name', 'created', 'expires_at', 'disabled')},
                                 'remaining_seconds': max(0, int(agent['expires_at'] - now))}
                                for identity, agent in self.data['agents'].items()],
                    'pending': [{'id': request_id, **{key: value for key, value in pending.items() if key != 'fingerprint'}}
                                for request_id, pending in self.pending.items() if pending['expires_at'] > now],
                    'audit': self.data['audit'][-100:], 'grant_seconds': GRANT_SECONDS}

    def prune(self):
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if v['expires_at'] > now}
        self.confirmations = {k: v for k, v in self.confirmations.items()
                              if (v['expires_at'] if isinstance(v, dict) else v) > now}
