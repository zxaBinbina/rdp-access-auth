"""In-memory desktop demonstration. Never connects to a controller or provider."""
from copy import deepcopy
import json
from pathlib import Path
import random
import secrets
import threading
import time

from management import ManagementError
from desktop.security import DANGEROUS, action_digest


class DemoClient:
    def __init__(self):
        self.lock = threading.RLock()
        self.reset()

    def reset(self):
        sample = random.SystemRandom()
        domain = sample.choice(('maple', 'cloud', 'willow', 'river')) + '-' + secrets.token_hex(4) + '.example.test'
        password = 'Demo-' + secrets.token_urlsafe(10) + '-Aa9!'
        tunnel_id = sample.randrange(100000, 999999)
        rdp_port = sample.choice((3389, 3390, 3391))
        self.words = sample.sample(('星河', '银杏', '纸鸢', '晨光', '山茶', '白鹭', '海棠', '青竹', '风铃'), 6)
        self.preferences = {'startup': 'boot', 'paused': False, 'show_tray': True,
                            'palette': 'fufu-blue', 'theme': 'system'}
        self.config = {'hostname': 'auth.' + domain, 'rdp_address': 'desktop.' + domain + ':3389',
                       'allow_passkey_registration': False,
                       'tunnel_id': tunnel_id, 'has_sakura_token': True, 'has_turnstile_secret': True,
                       'turnstile_site_key': 'demo-' + secrets.token_hex(8), 'wordlist_path': '',
                       'local_admission': {'listen_port': 13389, 'target_port': rdp_port, 'duration_seconds': 21600}}
        self.credentials = {'passkey_list': [{'id': 'demo-laptop', 'name': sample.choice(('书房笔记本', '工作电脑', '旅行笔记本'))},
                                            {'id': 'demo-phone', 'name': sample.choice(('随身手机', '备用手机', '平板电脑'))}],
                            'banned_ips': sample.randrange(1, 5), 'global_lock_seconds': 0}
        now = int(time.time())
        self.agents = {'agents': [{'id': 'demo-agent', 'name': '部署助手（示例）',
                                  'remaining_seconds': 3600, 'expires_at': now + 3600}],
                       'pending': [{'id': 'demo-approval', 'agent_id': 'demo-agent', 'name': '部署助手（示例）',
                                    'kind': 'ordinary', 'reason': '申请 1 小时配置权限'},
                                   {'id': 'demo-danger', 'agent_id': 'demo-agent', 'name': '部署助手（示例）',
                                    'kind': 'dangerous', 'reason': '演示更换管理密码的单独确认', 'fields': ['password']}],
                       'audit': [{'time': now, 'actor': '部署助手（示例）', 'operation': 'settings.save', 'outcome': 'success'}]}
        agent_name = sample.choice(('星河助手', '云端管家', '远程部署助手')) + '（示例）'
        self.agents['agents'][0]['name'] = agent_name
        for request in self.agents['pending']:
            request['name'] = agent_name
        self.agents['audit'][0]['actor'] = agent_name
        self.inventory = {'sakura': {'nodes': [{'id': 1, 'name': '上海节点（示例）', 'available': True},
                                             {'id': 2, 'name': '北京节点（示例）', 'available': True}],
                                    'tunnels': [{'id': tunnel_id, 'name': '远程桌面（示例）'}]},
                          'cloudflare': {'accounts': [{'id': 'demo-account', 'name': '我的账户（示例）'}],
                                         'zones': [{'id': 'demo-zone', 'name': domain, 'account_id': 'demo-account'}]}}
        self.plan = {'mode': 'automatic', 'hostname': self.config['hostname'], 'rdp_port': rdp_port,
                     'admission_port': 13389, 'portal_port': 18089}
        self.fields = {'password': password, 'repeat_password': password, 'name': agent_name,
                       'sakura_api_token': 'demo-' + secrets.token_hex(12), 'sakura_token': 'demo-' + secrets.token_hex(12),
                       'cloudflare_api_token': 'demo-' + secrets.token_hex(12),
                       'cloudflare_tunnel_token': 'demo-' + secrets.token_hex(16),
                       'turnstile_site_key': self.config['turnstile_site_key'], 'turnstile_secret_key': 'demo-' + secrets.token_hex(12),
                       'hostname': self.config['hostname'], 'rdp_address': self.config['rdp_address'], 'sakura_tunnel_id': tunnel_id}
        self.saved_secrets = {key: self.fields[key] for key in ('sakura_token', 'turnstile_secret_key')}
        self.deployment = {'phase': 'idle'}
        self.management_password = password
        self.sessions = set()
        self.confirmations = {}
        self.rotation = 0
        self.stopped = False
        self.revision = 1

    def status(self):
        return {'demo': True, 'initialized': True, 'configured': True, 'preferences': self.preferences,
                'demo_data': {'fields': self.fields, 'management_password': self.management_password, 'inventory': self.inventory,
                              'draft': {**self.plan, 'sakura_node_id': 1, 'cf_account_id': 'demo-account', 'cf_zone_id': 'demo-zone'}},
                'config': self.config, 'revision': str(self.revision), 'deployment': self.deployment,
                # Sample approval cards must not steal focus from the page being previewed.
                'pending_count': 0, 'services': [{'ActiveState': 'inactive' if self.stopped else 'active'} for _ in range(3)]}

    def request(self, method, params=None, *, session=None, confirmation=None):
        with self.lock:
            return deepcopy(self.execute(method, params or {}, session, confirmation))

    def check_password(self, password):
        if not isinstance(password, str) or not secrets.compare_digest(password.encode(), self.management_password.encode()):
            raise ManagementError('管理密码不正确。', 401)

    def portal_preview(self, screen):
        # The public demo renders synthetic credentials independently of the
        # management session; privileged RPCs still require an unlocked window.
        with self.lock:
            offset = self.rotation % 2 * 3
            return render_portal(screen, self.status(), self.credentials, '-'.join(self.words[offset:offset + 3]))

    def execute(self, method, params, session, confirmation):
        if method == 'demo.reset':
            self.reset()
            return self.status()
        if method in ('status', 'ui.bootstrap'):
            return self.status()
        if method == 'security.login':
            self.check_password(params.get('password'))
            token = secrets.token_urlsafe(32)
            self.sessions.add(token)
            return {'session': token}
        if method == 'settings.read':
            return {'config': self.config, 'state': self.credentials, 'revision': str(self.revision)}
        if method == 'ui.open_link':
            return {'message': '演示模式下仅预览界面。'}
        if method == 'onboarding.guide':
            return {'demo': True}
        if method == 'onboarding.status':
            return self.deployment
        if not isinstance(session, str) or session not in self.sessions:
            raise ManagementError('请先输入管理密码解锁当前窗口。', 401)
        if method == 'security.logout':
            self.sessions.remove(session)
            self.confirmations = {key: value for key, value in self.confirmations.items() if value['session'] != session}
            return {'locked': True}
        if method == 'security.confirm':
            self.check_password(params.get('password'))
            token = secrets.token_urlsafe(24)
            self.confirmations[token] = {'session': session, 'fingerprint': action_digest(params.get('method'), params.get('params', {})),
                                         'expires_at': time.time() + 120}
            return {'confirmation': token}
        if method in DANGEROUS:
            approved = self.confirmations.pop(confirmation, None) if isinstance(confirmation, str) else None
            if (not approved or approved['session'] != session or approved['fingerprint'] != action_digest(method, params)
                    or approved['expires_at'] <= time.time()):
                raise ManagementError('危险操作需要单独确认，确认只对当前操作有效。', 428)
        if method == 'security.password':
            password = params.get('password')
            if not isinstance(password, str) or not 12 <= len(password) <= 128:
                raise ManagementError('管理密码需要 12～128 个字符。')
            self.management_password = password
            self.sessions.clear()
            self.confirmations.clear()
            for agent in self.agents['agents']:
                agent.update(remaining_seconds=0, expires_at=0)
            return {'message': '已演示更换管理密码。', 'locked': True}
        if method == 'settings.secrets':
            config = dict(self.config)
            config.update(self.saved_secrets)
            return {'config': config, 'state': self.credentials, 'revision': str(self.revision)}
        if method == 'settings.save':
            changes = params.get('changes', {})
            if params.get('revision', str(self.revision)) != str(self.revision):
                raise ManagementError('配置已被其他窗口或命令修改。请重新载入后再保存。', 409)
            if 'allow_passkey_registration' in changes and type(changes['allow_passkey_registration']) is not bool:
                raise ManagementError('允许添加通行密钥必须为布尔值。')
            self.saved_secrets.update({k: v for k, v in changes.items() if k in self.saved_secrets and isinstance(v, str) and v})
            self.config.update({k: v for k, v in changes.items()
                                if k in self.config and v is not None})
            if changes.get('disable_turnstile'):
                self.config.update(turnstile_site_key='', has_turnstile_secret=False)
                self.saved_secrets['turnstile_secret_key'] = ''
            self.revision += 1
            return {'message': '演示配置已更新。'}
        if method == 'preferences.save':
            self.preferences.update({k: v for k, v in params.get('changes', {}).items() if k in self.preferences})
            return self.preferences
        if method in ('services.pause', 'services.resume', 'services.restart', 'services.exit'):
            self.stopped = method in ('services.pause', 'services.exit') or self.preferences['paused'] and method == 'services.restart'
            if method in ('services.pause', 'services.resume'):
                self.preferences['paused'] = method == 'services.pause'
            if method == 'services.exit':
                self.preferences['startup'] = 'manual' if params.get('disable_autostart') else 'boot'
            return {'message': '演示服务状态已更新。'}
        if method == 'logs.read':
            return {'output': '[演示日志] ' + str(params.get('service', 'portal')) + '\n10:00:00 配置校验通过\n10:00:01 服务已就绪\n10:02:15 示例网络 203.0.113.10 完成认证\n凭据：[已隐藏]'}
        if method == 'connectors.sync':
            return {'message': '演示隧道已同步。'}
        if method in ('credentials.temporary', 'credentials.regenerate'):
            self.rotation += method == 'credentials.regenerate'
            offset = self.rotation % 2 * 3
            return {'temporary': '-'.join(self.words[offset:offset + 3])}
        if method == 'credentials.delete_passkey':
            self.credentials['passkey_list'] = [k for k in self.credentials['passkey_list'] if k['id'] != params.get('id')]
            return {'message': '示例通行密钥已移除。'}
        if method == 'credentials.unlock':
            self.credentials.update(banned_ips=0, global_lock_seconds=0)
            return {'message': '示例封禁已解除。'}
        if method == 'agents.list':
            return self.agents
        if method == 'agents.approve':
            if params.get('allow') and any(p['id'] == params.get('request_id') and p['kind'] == 'dangerous' for p in self.agents['pending']):
                self.check_password(params.get('password'))
            self.agents['pending'] = [p for p in self.agents['pending'] if p['id'] != params.get('request_id')]
            return {'message': '演示申请已处理。'}
        if method == 'agents.register':
            agent = {'id': 'demo-agent-' + str(self.revision), 'name': params.get('name') or '新的助手（示例）',
                     'remaining_seconds': 0, 'expires_at': 0}
            self.revision += 1
            self.agents['agents'].append(agent)
            return {**agent, 'key': 'demo-identity-not-valid'}
        if method in ('agents.remove', 'agents.revoke'):
            if method == 'agents.remove':
                self.agents['agents'] = [a for a in self.agents['agents'] if a['id'] != params.get('id')]
            else:
                for agent in self.agents['agents']:
                    if agent['id'] == params.get('id'):
                        agent.update(remaining_seconds=0, expires_at=0)
            return {'message': '演示身份已更新。'}
        if method == 'ui.export_agent':
            return {'configuration': {'mcpServers': {'rdp-access-auth-demo': {'command': 'rdp-auth',
                    'args': ['mcp', '--identity', '/演示路径/未创建身份文件.json']}}}}
        if method == 'onboarding.inspect':
            return self.inventory
        if method in ('onboarding.prepare', 'demo.wizard'):
            self.plan.update({k: v for k, v in params.items() if k in self.plan and v not in ('', None)})
            phase = 'complete' if method == 'demo.wizard' and params.get('step') == 8 else 'ready'
            self.deployment = {'phase': phase, 'plan': dict(self.plan), 'revision': 'demo-plan',
                               'events': ['示例平台连接正常', '示例运行文件检查通过', '演示部署完成' if phase == 'complete' else '演示计划已准备，可以继续']}
            return self.deployment
        if method == 'onboarding.apply':
            self.deployment.update(phase='complete', events=['示例平台连接正常', '示例隧道已配置', '演示部署完成'])
            return self.deployment
        if method == 'migration.plan':
            return {'available': True, 'revision': 'demo-migration', 'steps': ['备份原配置和凭据', '切换到当前软件包', '检查服务状态']}
        if method == 'migration.start':
            return {'phase': 'complete', 'message': '演示迁移完成。'}
        raise ManagementError('演示模式尚不支持此操作。', 400)


def render_portal(screen, status, credentials, temporary):
    """Render the real public template with fixtures and local-only interactions."""
    from jinja2 import Environment
    template = Path(__file__).resolve().parents[1] / 'portal.html'
    allowed = status['config'].get('allow_passkey_registration') is True
    current_key = next(iter(credentials['passkey_list']), {}).get('id') if screen == 'credentials-passkey' else None
    body = Environment(autoescape=True).from_string(template.read_text()).render(
        nonce='demo-preview', method=screen if screen in ('password', 'temporary', 'passkey') else 'password',
        manage=screen in ('credentials', 'credentials-passkey'), success=screen == 'success', csrf='demo-only',
        client_ip='2001:db8::10' if screen == 'ipv6' else '203.0.113.10', ipv6=screen == 'ipv6',
        turnstile_site_key='', keys=credentials['passkey_list'], can_bind=allowed and screen == 'credentials',
        allow_passkey_registration=allowed, current_passkey_id=current_key,
        current_temporary=temporary, next_temporary=temporary,
        authorized_ips=['203.0.113.10'], rdp=status['config']['rdp_address'],
        local_admission=True, can_revoke=True, has_management=True,
        message='密码不正确，请检查后重试。（示例）' if screen == 'error' else '')
    csp = "default-src 'none'; img-src data:; script-src 'self' 'nonce-demo-preview'; style-src 'nonce-demo-preview'; connect-src 'none'; form-action 'none'; base-uri 'none'"
    body = body.replace('<head>', '<head><meta http-equiv="Content-Security-Policy" content="' + csp + '">', 1)
    data = json.dumps({'password': status['demo_data']['fields']['password'], 'words': temporary.split('-')}, ensure_ascii=False).replace('<', '\\u003c')
    return body.replace('</body>', '<script nonce="demo-preview" type="application/json" id="demo-portal-data">' + data + '</script><script nonce="demo-preview" src="/demo-portal.js"></script></body>')
