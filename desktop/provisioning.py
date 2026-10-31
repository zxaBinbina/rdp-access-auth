"""Restartable first-run provisioning with explicit prepare/apply boundaries."""
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.request

from deploy import download_connector, health_check, unit_files
from management import ManagementError, atomic_private_write, hash_password, hostname_valid, validate_config
from desktop.providers import Cloudflare, Sakura, cf_id
from desktop.services import RUNTIME_UNITS
from tools.build_wordlist import build_wordlist

ROOT = Path(__file__).resolve().parents[1]
SENSITIVE = ('sakura_api_token', 'cloudflare_api_token', 'cloudflare_tunnel_token', 'turnstile_secret_key')


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_private_write(path, (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode())


def json_file(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def download_frpc(destination, progress):
    manifest = json.loads((ROOT / 'deployment/frpc-downloads.json').read_text())
    asset = manifest.get(platform.machine())
    if asset is None:
        raise ManagementError('此 CPU 架构尚未提供经过校验的 frpc 客户端。')
    progress('下载 SakuraFrp 客户端 ' + manifest['version'])
    request = urllib.request.Request(asset['url'], headers={'User-Agent': 'rdp-access-auth/2.0'})
    digest, size = hashlib.sha256(), 0
    with urllib.request.urlopen(request, timeout=40) as response, destination.open('wb') as out:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 50 * 1024 * 1024:
                raise ManagementError('frpc 下载内容超过大小限制。')
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != asset['sha256'] or size != asset['size']:
        raise ManagementError('frpc 下载校验失败，请重试；未安装该文件。')
    destination.chmod(0o755)


def runtime_units(port):
    units = unit_files(port)
    units[RUNTIME_UNITS[1]] = '''[Unit]
Description=SakuraFrp for RDP Access Auth
Wants=network-online.target rdp-access-auth.service
After=network-online.target rdp-access-auth.service

[Service]
Type=simple
DynamicUser=yes
LoadCredential=frpc.ini:/etc/rdp-access-auth/frpc.ini
ExecStart=/usr/local/libexec/rdp-access-auth/frpc --natfrp_tls --no_check_update --disable_log_color --log_level warn --config %d/frpc.ini
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
LockPersonality=yes
MemoryMax=256M
TasksMax=64

[Install]
WantedBy=multi-user.target
'''
    for name in units:
        units[name] = units[name].replace('[Unit]\n', '[Unit]\nConditionPathExists=!/etc/rdp-access-auth-control/paused\n', 1)
    return units


class Provisioner:
    def __init__(self, controller, transport=None, downloader=download_connector, frpc_downloader=download_frpc,
                 word_builder=build_wordlist, health=health_check):
        self.controller, self.root = controller, controller.root
        self.directory = self.root / 'etc/rdp-access-auth-control'
        self.stage = self.root / 'var/lib/rdp-access-auth-control/staging'
        self.state_path = self.directory / 'onboarding.json'
        self.vault_path = self.directory / 'providers.json'
        self.vault = json_file(self.vault_path, {})
        self.state = json_file(self.state_path, {'phase': 'idle', 'events': [], 'resources': {}})
        self.transport = transport
        self.downloader, self.frpc_downloader, self.word_builder, self.health = downloader, frpc_downloader, word_builder, health
        self.worker = None
        self.lock = threading.RLock()
        if self.state['phase'] in ('preparing', 'installing'):
            self.state.update(phase='interrupted', error='上次操作中断。已有资源记录已保留，可重新准备后继续。')

    def clients(self, checkpoint=lambda: None):
        options = {'checkpoint': checkpoint, **({'transport': self.transport} if self.transport else {})}
        return Sakura(self.vault.get('sakura_api_token', ''), **options), Cloudflare(self.vault.get('cloudflare_api_token', ''), **options)

    def persist(self):
        write_json(self.state_path, self.state)

    def progress(self, message):
        with self.lock:
            self.state['message'] = message
            self.state['events'] = (self.state.get('events', []) + [message])[-40:]
            self.persist()

    def status(self):
        with self.lock:
            result = {key: self.state.get(key) for key in ('phase', 'message', 'error', 'events', 'plan', 'revision', 'checks', 'krdp')}
            result['resources'] = {key: value for key, value in self.state.get('resources', {}).items()
                                   if key in ('sakura_tunnel_id', 'sakura_created', 'cloudflare_tunnel_id', 'cloudflare_created', 'dns_record_id', 'turnstile_site_key')}
            return json.loads(json.dumps(result))

    def secrets(self):
        import configparser
        values = [self.vault.get(key, '') for key in SENSITIVE]
        path = self.root / 'etc/rdp-access-auth/frpc.ini'
        if path.is_file():
            parser = configparser.ConfigParser(interpolation=None)
            try:
                parser.read_string(path.read_text())
                values.extend(value for section in parser.values() for key, value in section.items()
                              if 'token' in key or 'password' in key or key.endswith('_key'))
            except (OSError, configparser.Error):
                pass
        return values

    def guide(self):
        return {'steps': [
            {'title': '欢迎', 'description': '准备一台 Linux 主机、可用的远程桌面和联网环境。'},
            {'title': '保护管理设置', 'description': '设置独立的管理密码。首次初始化需要 Linux 管理员确认。'},
            {'title': '选择部署方式', 'description': '推荐 API 自动配置；也可按教程手动准备，或将 MCP 配置交给 Agent。'},
            {'title': '准备平台账户', 'description': '注册 SakuraFrp，完成平台要求的账户认证；在 Cloudflare 添加自己的域名并完成 DNS 托管。'},
            {'title': '连接平台', 'description': '填写 SakuraFrp 访问密钥；自动模式再填写 Cloudflare API Token。'},
            {'title': '选择网络', 'description': '选择可用节点、账户、域名和认证子域名；本机 RDP 默认端口为 3389。'},
            {'title': '设置认证', 'description': '设置 16～128 位固定访问密码，包含大小写、数字与特殊符号；Passkey 在首次密码认证后绑定。'},
            {'title': '检查与部署', 'description': '先准备并校验运行文件，再确认具体计划，创建所需资源并启动服务。'}],
            'links': {'sakura': 'https://www.natfrp.com/user/', 'sakura_docs': 'https://doc.natfrp.com/frpc/usage.html#get-token',
                      'cloudflare': 'https://dash.cloudflare.com/', 'cloudflare_tokens': 'https://dash.cloudflare.com/profile/api-tokens',
                      'cloudflare_docs': 'https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/'},
            'cloudflare_permissions': ['账户 / Cloudflare Tunnel / 编辑', '区域 / DNS / 编辑', '区域 / 区域 / 读取',
                                       '账户 / Turnstile / 编辑（仅自动创建人机验证时）'],
            'manual': ['在 SakuraFrp 创建 TCP 隧道，目标为 127.0.0.1:13389。',
                       '隧道自定义配置填写 auth_mode = server、auth_time = 6h、proxy_protocol_version = v2，各占一行。',
                       '在 Cloudflare 创建远程管理的 Tunnel，取得 Tunnel Token。',
                       '添加应用路由：认证域名 → http://127.0.0.1:18089，并配置对应 DNS。'],
            'mcp': {'command': '/usr/bin/rdp-auth', 'args': ['mcp', '--identity', '<软件导出的身份文件路径>'],
                    'instructions': '先登记独立 Agent 身份；调用 request_authorization，等待用户批准。之后调用 get_setup_guide、inspect_platforms、prepare_deployment、apply_deployment，并轮询 deployment_status。危险操作需单独批准。'}}

    def environment(self):
        from desktop.krdp import detect
        listening = False
        try:
            with socket.create_connection(('127.0.0.1', 3389), timeout=1):
                listening = True
        except OSError:
            pass
        return {'linux': platform.system() == 'Linux', 'architecture': platform.machine(),
                'systemd': (self.root / 'run/systemd/system').exists(), 'rdp_listening': listening,
                'krdp': detect(self.root), 'configured': self.controller.services.installed()}

    def inspect(self, params):
        if self.worker and self.worker.is_alive():
            raise ManagementError('请等待当前部署任务结束。', 409)
        allowed = set(SENSITIVE) | {'cf_account_id'}
        if set(params) - allowed:
            raise ManagementError('包含未知的平台连接字段。')
        candidate = dict(self.vault)
        for key in SENSITIVE:
            if params.get(key):
                value = params[key]
                if not isinstance(value, str) or len(value) > 16384 or any(ch.isspace() or ord(ch) < 32 for ch in value):
                    raise ManagementError('平台密钥格式无效。')
                candidate[key] = value
        options = {'transport': self.transport} if self.transport else {}
        sakura = Sakura(candidate.get('sakura_api_token', ''), **options)
        cloudflare = Cloudflare(candidate.get('cloudflare_api_token', ''), **options)
        result = {'sakura': sakura.inventory()}
        if candidate.get('cloudflare_api_token'):
            result['cloudflare'] = cloudflare.inventory()
            account = params.get('cf_account_id')
            if account:
                result['cloudflare']['tunnels'] = cloudflare.tunnels(account)
        self.vault = candidate
        write_json(self.vault_path, self.vault)
        self.state['inventory'] = result
        self.persist()
        return result

    def _start(self, phase, task):
        if self.worker and self.worker.is_alive():
            raise ManagementError('已有部署正在进行。', 409)
        self.state.update(phase=phase, error='')
        self.persist()
        def execute():
            try:
                task()
            except Exception as exc:
                with self.lock:
                    self.state.update(phase='failed', error=str(exc) if isinstance(exc, ManagementError)
                                      else '部署未完成，请检查网络和权限后重试；已创建的资源记录会保留。')
                    self.persist()
        self.worker = threading.Thread(target=execute, name='rdp-setup', daemon=True)
        self.worker.start()
        return self.status()

    def prepare(self, params):
        if self.worker and self.worker.is_alive():
            raise ManagementError('请等待当前部署任务结束。', 409)
        if self.controller.services.installed() and self.state.get('phase') == 'complete':
            raise ManagementError('已经部署，请在设置中修改配置。', 409)
        if (self.root / 'etc/rdp-auth').exists():
            raise ManagementError('发现旧部署，请先通过运行维护迁移，避免覆盖。', 409)
        allowed = {'mode', 'hostname', 'rdp_address', 'sakura_node_id', 'sakura_tunnel_id', 'remote_port',
                   'cf_account_id', 'cf_zone_id', 'cf_tunnel_id', 'password', 'rdp_port', 'admission_port',
                   'portal_port', 'turnstile_site_key', 'turnstile_secret_key', 'create_turnstile',
                   'cloudflare_tunnel_token', 'wordlist_path'}
        if not isinstance(params, dict) or set(params) - allowed:
            raise ManagementError('包含未知的部署字段。')
        if params.get('mode') not in ('automatic', 'manual') or not hostname_valid(params.get('hostname')):
            raise ManagementError('请选择部署方式，并填写完整的小写认证域名。')
        if not self.vault.get('sakura_api_token'):
            raise ManagementError('请先连接 SakuraFrp 平台。')
        plan = {key: value for key, value in params.items() if key not in ('password', 'cloudflare_tunnel_token', 'turnstile_secret_key')}
        for key, default in [('rdp_port', 3389), ('admission_port', 13389), ('portal_port', 18089)]:
            value = params.get(key, default)
            if type(value) is not int or not 1024 <= value <= 65535:
                raise ManagementError('本机端口需要为 1024～65535 的整数。')
            plan[key] = value
        if params.get('remote_port') and (type(params['remote_port']) is not int or not 1 <= params['remote_port'] <= 65535):
            raise ManagementError('公网端口必须为 1～65535 的整数。')
        if 'create_turnstile' in params and type(params['create_turnstile']) is not bool:
            raise ManagementError('人机验证选项无效。')
        if len({plan[key] for key in ('rdp_port', 'admission_port', 'portal_port')}) != 3:
            raise ManagementError('RDP、准入和认证页面端口不能相同。')
        for key in ('sakura_node_id', 'sakura_tunnel_id'):
            if params.get(key) and (type(params[key]) is not int or not 0 < params[key] <= 2147483647):
                raise ManagementError('节点或隧道编号无效。')
        if params['mode'] == 'automatic':
            if not self.vault.get('cloudflare_api_token'):
                raise ManagementError('自动配置需要 Cloudflare API Token。')
            cf_id(params.get('cf_account_id')); cf_id(params.get('cf_zone_id'))
            if params.get('cf_tunnel_id'):
                cf_id(params['cf_tunnel_id'], uuid=True)
            if not params.get('sakura_tunnel_id') and not params.get('sakura_node_id'):
                raise ManagementError('请选择 SakuraFrp 节点或已有隧道。')
        elif not params.get('sakura_tunnel_id'):
            raise ManagementError('手动部署需要填写已有 SakuraFrp 隧道 ID。')
        for key in ('cloudflare_tunnel_token', 'turnstile_secret_key'):
            if params.get(key):
                if not isinstance(params[key], str) or not re.fullmatch(r'[A-Za-z0-9_+/=-]{16,16384}', params[key]):
                    raise ManagementError('运行凭据格式无效，请只填写密钥，不包含命令。')
                self.vault[key] = params[key]
        if params['mode'] == 'manual' and not self.vault.get('cloudflare_tunnel_token'):
            raise ManagementError('手动部署需要 Cloudflare Tunnel Token。')
        salt, hashed = hash_password(params.get('password'))
        fingerprint = hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()
        if self.state.get('resources') and self.state.get('plan_fingerprint') != fingerprint:
            raise ManagementError('上次部署已创建远程资源，请先按原计划继续，避免产生重复资源。', 409)
        write_json(self.vault_path, self.vault)
        self.stage.mkdir(parents=True, exist_ok=True, mode=0o700)
        draft = {'hostname': params['hostname'], 'sakura_token': self.vault['sakura_api_token'],
                 'password_salt': salt, 'password_hash': hashed,
                 'session_key': self.state.get('session_key') or secrets.token_hex(32),
                 'turnstile_site_key': params.get('turnstile_site_key', ''),
                 'turnstile_secret_key': self.vault.get('turnstile_secret_key', '') if params.get('turnstile_site_key') else '',
                 'local_admission': {'listen_port': plan['admission_port'], 'target_port': plan['rdp_port'], 'duration_seconds': 21600}}
        # Validate user-entered fields before creating any remote resources.
        # Generated tunnel ID and public address are checked again after allocation.
        validate_config({**draft, 'tunnel_id': plan.get('sakura_tunnel_id') or 1,
                         'rdp_address': plan.get('rdp_address') or params['hostname'] + ':' + str(plan['rdp_port'])})
        write_json(self.stage / 'settings.json', draft)
        self.state.update(plan=plan, session_key=draft['session_key'], plan_fingerprint=fingerprint,
                          plan_id=self.state.get('plan_id') or secrets.token_hex(6))
        return self._start('preparing', lambda: self._prepare(plan))

    def _prepare(self, plan):
        self.progress('检查平台资源与本机端口')
        sakura, cloudflare = self.clients()
        if self.root == Path('/'):
            for port in (plan['portal_port'], plan['admission_port']):
                try:
                    with socket.socket() as listener:
                        listener.bind(('127.0.0.1', port))
                except OSError:
                    raise ManagementError('本机端口 ' + str(port) + ' 已占用，请在向导中更换。') from None
        inventory = sakura.inventory()
        if plan.get('sakura_tunnel_id'):
            sakura.tunnel(plan['sakura_tunnel_id'])
        elif not any(node['id'] == plan['sakura_node_id'] and node['available'] for node in inventory['nodes']):
            raise ManagementError('所选 SakuraFrp 节点当前不可创建隧道，请选择其他节点。')
        if plan['mode'] == 'automatic':
            zones = cloudflare.inventory()['zones']
            selected = next((zone for zone in zones if zone['id'] == plan['cf_zone_id'] and zone['account_id'] == plan['cf_account_id']), None)
            if not selected or not plan['hostname'].endswith('.' + selected['name']):
                raise ManagementError('认证地址应为所选 Cloudflare 域名下的子域名。')
        else:
            atomic_private_write(self.stage / 'frpc.ini', sakura.config(plan['sakura_tunnel_id'], plan['admission_port']).encode())
        self.progress('准备临时密码词库')
        if plan.get('wordlist_path'):
            source = Path(plan['wordlist_path'])
            words = json.loads(source.read_text())
            if not isinstance(words, list) or len(set(words)) < 2048 or any(not isinstance(word, str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,}', word) for word in words):
                raise ManagementError('词库必须包含至少 2048 个不同的有效中文词。')
            shutil.copyfile(source, self.stage / 'objects.json')
        else:
            words = self.word_builder(self.stage, download=True, progress=self.progress)
            shutil.copyfile(words, self.stage / 'objects.json')
        self.downloader(self.stage / 'cloudflared', self.progress)
        self.frpc_downloader(self.stage / 'frpc', self.progress)
        self.state['artifacts'] = {name: hashlib.sha256((self.stage / name).read_bytes()).hexdigest()
                                   for name in ('settings.json', 'objects.json', 'cloudflared', 'frpc')}
        self.state['revision'] = hashlib.sha256(json.dumps([self.state['plan'], self.state['artifacts']], sort_keys=True).encode()).hexdigest()
        self.state['phase'] = 'ready'
        self.progress('运行文件已校验。请确认计划后开始创建资源和部署。')

    def apply(self, params, checkpoint):
        if self.state['phase'] not in ('ready', 'failed', 'interrupted') or params.get('revision') != self.state.get('revision'):
            raise ManagementError('请先完成准备，并确认当前版本的部署计划。', 409)
        if not self.state.get('artifacts'):
            raise ManagementError('准备尚未完成，请重新准备。', 409)
        self.check_conflicts()
        for name, digest in self.state['artifacts'].items():
            if not (self.stage / name).is_file() or hashlib.sha256((self.stage / name).read_bytes()).hexdigest() != digest:
                raise ManagementError('待安装文件已变化，请重新准备。', 409)
        return self._start('installing', lambda: self._apply(checkpoint))

    def resource(self, key, value):
        self.state.setdefault('resources', {})[key] = value
        self.persist()

    def _apply(self, checkpoint):
        plan, resources = self.state['plan'], self.state['resources']
        sakura, cloudflare = self.clients(checkpoint)
        checkpoint()
        tunnel_id = resources.get('sakura_tunnel_id') or plan.get('sakura_tunnel_id')
        if plan['mode'] == 'automatic':
            self.progress('配置 SakuraFrp 远程桌面隧道')
            if not tunnel_id:
                name = 'rdp-' + self.state['plan_id']
                found = next((entry for entry in sakura.inventory()['tunnels'] if entry['name'] == name), None)
                created = found or sakura.create(name, plan['sakura_node_id'], plan['admission_port'], plan.get('remote_port', ''))
                tunnel_id = created['id']
                self.resource('sakura_tunnel_id', tunnel_id)
                self.resource('sakura_created', True)
            else:
                if not resources.get('sakura_previous') and not resources.get('sakura_created'):
                    self.resource('sakura_previous', {key: value for key, value in sakura.tunnel(tunnel_id).items()
                                                     if key in ('id', 'local_ip', 'local_port', 'extra')})
                checkpoint()
                sakura.secure_existing(tunnel_id, plan['admission_port'])
            self.resource('sakura_tunnel_id', tunnel_id)
            checkpoint()
            self.progress('配置 Cloudflare 认证入口')
            cf_tunnel = resources.get('cloudflare_tunnel_id') or plan.get('cf_tunnel_id')
            if not cf_tunnel:
                name = 'rdp-auth-' + self.state['plan_id']
                found = next((entry for entry in cloudflare.tunnels(plan['cf_account_id']) if entry['name'] == name), None)
                created = found or cloudflare.create(plan['cf_account_id'], name)
                cf_tunnel = created['id']
                self.resource('cloudflare_tunnel_id', cf_tunnel)
                self.resource('cloudflare_created', True)
            checkpoint()
            route = cloudflare.route(plan['cf_account_id'], plan['cf_zone_id'], cf_tunnel, plan['hostname'], plan['portal_port'])
            self.resource('cloudflare_tunnel_id', cf_tunnel)
            self.resource('dns_record_id', route['dns_record_id'])
            self.vault['cloudflare_tunnel_token'] = route['token']
            write_json(self.vault_path, self.vault)
            if plan.get('create_turnstile') and not resources.get('turnstile_site_key'):
                checkpoint()
                widget = cloudflare.turnstile(plan['cf_account_id'], plan['hostname'])
                self.vault['turnstile_secret_key'] = widget['turnstile_secret_key']
                write_json(self.vault_path, self.vault)
                self.resource('turnstile_site_key', widget['turnstile_site_key'])
        checkpoint()
        config_text = sakura.config(tunnel_id, plan['admission_port'])
        settings = json_file(self.stage / 'settings.json', {})
        settings.update(tunnel_id=tunnel_id, rdp_address=plan.get('rdp_address') or sakura.address(tunnel_id),
                        wordlist_path='/etc/rdp-access-auth/objects.json')
        if resources.get('turnstile_site_key'):
            settings.update(turnstile_site_key=resources['turnstile_site_key'], turnstile_secret_key=self.vault['turnstile_secret_key'])
        validate_config(settings)
        self.progress('安装已校验的程序、凭据和服务')
        checkpoint()
        self.check_conflicts()
        config_dir = self.root / 'etc/rdp-access-auth'
        receipt = config_dir / 'deployment.json'
        existing = json_file(receipt, {})
        if config_dir.exists() and existing.get('plan_id') != self.state['plan_id']:
            raise ManagementError('发现其他部署，已停止安装以避免覆盖。', 409)
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_json(receipt, {'version': '2.0.0', 'plan_id': self.state['plan_id'], 'port': plan['portal_port'], 'status': 'installing', 'units': list(RUNTIME_UNITS)})
        write_json(config_dir / 'portal-settings.json', settings)
        atomic_private_write(config_dir / 'objects.json', (self.stage / 'objects.json').read_bytes())
        atomic_private_write(config_dir / 'cloudflare-token', self.vault['cloudflare_tunnel_token'].encode())
        atomic_private_write(config_dir / 'frpc.ini', config_text.encode())
        binaries = self.root / 'usr/local/libexec/rdp-access-auth'
        binaries.mkdir(parents=True, exist_ok=True, mode=0o755)
        for name in ('cloudflared', 'frpc'):
            shutil.copyfile(self.stage / name, binaries / name)
            (binaries / name).chmod(0o755)
        unit_dir = self.root / 'etc/systemd/system'
        unit_dir.mkdir(parents=True, exist_ok=True)
        for name, content in runtime_units(plan['portal_port']).items():
            target = unit_dir / name
            if target.exists() and existing.get('plan_id') != self.state['plan_id']:
                raise ManagementError('发现其他同名服务，已停止安装。', 409)
            target.write_text(content)
            target.chmod(0o644)
        checkpoint()
        from desktop.krdp import install
        self.state['krdp'] = install(self.root, self.controller.services.runner, self.controller.authority.data['owner_uid'])
        self.controller.services.runner(['systemctl', 'daemon-reload'])
        self.controller.services.save()
        self.controller.services.sync_startup()
        try:
            if not self.controller.services.prefs['paused']:
                self.controller.services.action('start')
                self.health(settings['hostname'], plan['portal_port'])
        except Exception:
            self.controller.services.runner(['systemctl', 'stop', *reversed(RUNTIME_UNITS)])
            raise
        self.state['phase'] = 'complete'
        self.state['checks'] = {'local_health': not self.controller.services.prefs['paused'], 'remote_rdp': '需要用户从另一网络实际连接验证'}
        write_json(receipt, {'version': '2.0.0', 'plan_id': self.state['plan_id'], 'port': plan['portal_port'],
                             'status': 'running', 'units': list(RUNTIME_UNITS), 'resources': resources})
        self.progress('本机部署完成。请从另一网络打开认证地址并测试远程桌面连接。')

    def check_conflicts(self):
        config_dir = self.root / 'etc/rdp-access-auth'
        receipt = json_file(config_dir / 'deployment.json', {})
        owns = receipt.get('plan_id') == self.state.get('plan_id') and bool(self.state.get('plan_id'))
        if not owns and (config_dir.exists() or any((self.root / 'etc/systemd/system' / unit).exists() for unit in RUNTIME_UNITS)):
            raise ManagementError('发现其他部署或同名服务，已停止以避免覆盖。', 409)

    def sync(self, checkpoint):
        """Reconcile saved portal settings with the owned connector deployment."""
        from management import read_config
        if self.worker and self.worker.is_alive():
            raise ManagementError('请等待当前部署任务结束。', 409)
        receipt = json_file(self.root / 'etc/rdp-access-auth/deployment.json', {})
        if receipt.get('plan_id') != self.state.get('plan_id') or not receipt.get('plan_id'):
            raise ManagementError('此部署不由 2.0 向导创建，请先完成迁移或手动同步连接器。', 409)
        settings, revision = read_config(self.controller.target.config)
        plan = self.state['plan']
        admission = settings.get('local_admission')
        if not admission:
            raise ManagementError('连接器需要启用本机准入网关。')
        checkpoint()
        sakura, cloudflare = self.clients(checkpoint)
        if plan['mode'] == 'automatic':
            sakura.secure_existing(settings['tunnel_id'], admission['listen_port'])
            route = cloudflare.route(plan['cf_account_id'], plan['cf_zone_id'],
                self.state['resources']['cloudflare_tunnel_id'], settings['hostname'], plan['portal_port'])
            self.vault['cloudflare_tunnel_token'] = route['token']
            write_json(self.vault_path, self.vault)
        config_text = sakura.config(settings['tunnel_id'], admission['listen_port'])
        checkpoint()
        directory = self.root / 'etc/rdp-access-auth'
        atomic_private_write(directory / 'frpc.ini', config_text.encode())
        atomic_private_write(directory / 'cloudflare-token', self.vault['cloudflare_tunnel_token'].encode())
        if not self.controller.services.prefs['paused']:
            self.controller.services.action('restart')
        return {'message': '连接器配置已同步。' + ('当前保持暂停。' if self.controller.services.prefs['paused'] else '服务已重启。')}
