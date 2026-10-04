"""First-install wizard. Downloads and validation finish before any system write."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

from management import (ROOT, ManagementError, Target, atomic_private_write, check_wordlist,
                        read_config, save_config)
from tools.build_wordlist import build_wordlist

UNITS = ('rdp-access-auth.service', 'cloudflared-rdp-access.service')


class Layout:
    def __init__(self, root=Path('/')):
        self.root = Path(root)
        self.config_dir = self.path('/etc/rdp-access-auth')
        self.config = self.config_dir / 'portal-settings.json'
        self.words = self.config_dir / 'objects.json'
        self.token = self.config_dir / 'cloudflare-token'
        self.receipt = self.config_dir / 'deployment.json'
        self.connector_dir = self.path('/usr/local/libexec/rdp-access-auth')
        self.connector = self.connector_dir / 'cloudflared'
        self.units = self.path('/etc/systemd/system')

    def path(self, absolute):
        return self.root / absolute.lstrip('/')

    def conflicts(self):
        candidates = [self.config_dir, self.connector_dir, self.path('/etc/rdp-auth'),
                      self.path('/opt/rdp-access-auth'), self.path('/var/lib/rdp-access-auth'),
                      self.path('/var/lib/private/rdp-access-auth'),
                      self.units / 'rdp-auth.service', *(self.units / name for name in UNITS)]
        candidates += [self.path(directory) / name for directory in ('/usr/lib/systemd/system', '/lib/systemd/system')
                       for name in (*UNITS, 'rdp-auth.service')]
        return [str(path) for path in candidates if os.path.lexists(path)]


def deployment_plan(layout=None, port=18089):
    layout = layout or Layout()
    return dict(config=str(layout.config), wordlist=str(layout.words), connector=str(layout.connector),
                units=[str(layout.units / name) for name in UNITS], port=port,
                conflicts=layout.conflicts(),
                prerequisites=['已安装 RPM/DEB', 'Linux systemd', '可出站访问词库来源和 GitHub',
                               '已配置 SakuraFrp 准入规则', 'Cloudflare Tunnel 已添加认证域名的 HTTP 路由'])


def preflight(layout, port, check_host=True):
    conflicts = layout.conflicts()
    if conflicts:
        raise ManagementError('检测到已有部署，向导不会覆盖。请通过管理功能维护原部署。冲突位置：' + '、'.join(conflicts), 409)
    if check_host:
        if not Path('/run/systemd/system').is_dir() or not shutil.which('systemctl'):
            raise ManagementError('部署向导需要正在运行 systemd 的 Linux 主机。')
        if not Path('/usr/bin/rdp-auth').is_file():
            raise ManagementError('请先安装 RPM/DEB 软件包，再运行部署向导。源码可使用 deploy --dry-run 查看部署计划。')
        try:
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', port))
        except OSError:
            raise ManagementError(f'本机端口 {port} 已被占用，请在向导中更换端口，并同步 Cloudflare 路由。') from None


def download_connector(destination, progress):
    manifest = json.loads((ROOT / 'deployment/cloudflared-downloads.json').read_text())
    asset = manifest.get(platform.machine())
    if not isinstance(asset, dict):
        raise ManagementError('自动部署目前支持 x86_64 和 aarch64。')
    progress('下载 Cloudflare 连接器 ' + manifest['version'])
    request = urllib.request.Request(asset['url'], headers={'User-Agent': 'rdp-access-auth-installer'})
    digest = hashlib.sha256()
    total = 0
    with urllib.request.urlopen(request, timeout=30) as response, destination.open('wb') as output:
        while chunk := response.read(1024 * 1024):
            total += len(chunk)
            if total > 100 * 1024 * 1024:
                raise ManagementError('Cloudflare 连接器超过大小限制。')
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != asset['sha256']:
        raise ManagementError('Cloudflare 连接器校验失败，部署已停止。')
    destination.chmod(0o755)


def unit_files(port):
    portal = f'''[Unit]
Description=RDP Access Auth
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
DynamicUser=yes
WorkingDirectory=/usr/lib/rdp-access-auth
StateDirectory=rdp-access-auth
StateDirectoryMode=0700
LoadCredential=settings.json:/etc/rdp-access-auth/portal-settings.json
LoadCredential=words.json:/etc/rdp-access-auth/objects.json
Environment=RDP_AUTH_WORDLIST=%d/words.json
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/usr/bin/rdp-auth --config %d/settings.json --state /var/lib/rdp-access-auth/state.sqlite3 serve --port {port}
Restart=on-failure
RestartSec=5s
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
MemoryMax=384M
TasksMax=64

[Install]
WantedBy=multi-user.target
'''
    connector = '''[Unit]
Description=Cloudflare Tunnel for RDP Access Auth
Wants=network-online.target rdp-access-auth.service
After=network-online.target rdp-access-auth.service

[Service]
Type=simple
DynamicUser=yes
LoadCredential=token:/etc/rdp-access-auth/cloudflare-token
ExecStart=/usr/local/libexec/rdp-access-auth/cloudflared --no-autoupdate tunnel run --token-file %d/token
Restart=on-failure
RestartSec=5s
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
    return {UNITS[0]: portal, UNITS[1]: connector}


def run_system(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise ManagementError('系统服务操作失败或超时，请查看 systemd 日志。') from None
    if result.returncode:
        raise ManagementError('系统服务操作失败，请查看 systemd 日志。')


def health_check(hostname, port):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        request = urllib.request.Request(f'http://127.0.0.1:{port}/healthz', headers={'Host': hostname})
        try:
            with opener.open(request, timeout=2) as response:
                if response.status == 200 and response.read(64).strip() == b'ok':
                    return
        except (OSError, urllib.error.URLError):
            pass
        time.sleep(0.5)
    raise ManagementError('认证服务未通过本机健康检查。')


class Deployment:
    def __init__(self, layout=None, check_host=True, runner=run_system, health=health_check,
                 downloader=download_connector, word_builder=build_wordlist):
        self.layout = layout or Layout()
        self.check_host, self.runner, self.health = check_host, runner, health
        self.downloader, self.word_builder = downloader, word_builder
        self.workspace = tempfile.TemporaryDirectory(prefix='rdp-auth-deploy-')
        self.stage = Path(self.workspace.name)
        self.lock = threading.RLock()
        self.worker = None
        self.phase, self.message, self.error = 'idle', '', ''
        self.events = []
        self.port = 18089
        self.settings = None
        self.artifacts = {}

    def progress(self, message):
        with self.lock:
            self.message = message
            self.events.append(message)

    def status(self):
        with self.lock:
            return dict(phase=self.phase, message=self.message, error=self.error, events=self.events[-30:],
                        plan=deployment_plan(self.layout, self.port),
                        configuration={name: self.settings.get(name) for name in ('hostname', 'rdp_address', 'tunnel_id')}
                                      if self.settings else {},
                        hostname=self.settings.get('hostname', '') if self.settings else '')

    def _run(self, task):
        try:
            task()
        except Exception as exc:
            with self.lock:
                self.phase = 'failed'
                self.error = str(exc) if isinstance(exc, ManagementError) else '准备或部署失败，请检查网络、系统权限及磁盘空间。'

    def prepare(self, changes, tunnel_token, port=18089):
        with self.lock:
            if self.phase not in ('idle', 'failed'):
                raise ManagementError('已有部署任务，请等待完成或重新打开向导。', 409)
            if type(port) is not int or not 1024 <= port <= 65535:
                raise ManagementError('认证端口必须为 1024～65535。')
            if (not isinstance(tunnel_token, str) or not 16 <= len(tunnel_token) <= 16384 or
                    not re.fullmatch(r'[A-Za-z0-9_+/=-]+', tunnel_token)):
                raise ManagementError('Cloudflare Tunnel Token 格式不正确，请只粘贴 Token，不包含命令。')
            preflight(self.layout, port, self.check_host)
            target = Target(self.stage / 'settings.json', self.stage / 'state.sqlite3', UNITS[0], self.stage)
            # Retrying preparation preserves the freshly generated session key in this workspace.
            exists = target.config.exists()
            save_config(target, changes, create=not exists)
            self.settings, _ = read_config(target.config)
            atomic_private_write(self.stage / 'token', tunnel_token.encode())
            self.port, self.phase, self.error = port, 'preparing', ''
            self.worker = threading.Thread(target=lambda: self._run(self._prepare), daemon=False)
            self.worker.start()
        return self.status()

    def _prepare(self):
        self.progress('校验配置与安装目标')
        words = self.settings.get('wordlist_path')
        if words:
            target = Target(self.stage / 'settings.json', self.stage / 'state.sqlite3', UNITS[0], self.stage)
            if not check_wordlist(target, self.settings)['ok']:
                raise ManagementError('指定的中文词库无效。')
            shutil.copyfile(words, self.stage / 'objects.json')
        else:
            try:
                output = self.word_builder(self.stage, download=True, progress=self.progress)
            except ValueError as exc:
                raise ManagementError(str(exc)) from None
            shutil.copyfile(output, self.stage / 'objects.json')
        self.downloader(self.stage / 'cloudflared', self.progress)
        self.settings['wordlist_path'] = '/etc/rdp-access-auth/objects.json'
        atomic_private_write(self.stage / 'settings.json', (json.dumps(self.settings, ensure_ascii=False, indent=2) + '\n').encode())
        self.artifacts = {name: hashlib.sha256((self.stage / name).read_bytes()).hexdigest()
                          for name in ('settings.json', 'objects.json', 'cloudflared', 'token')}
        self.progress('准备完成，尚未写入系统。请核对部署计划后确认。')
        with self.lock:
            self.phase = 'ready'

    def apply(self):
        with self.lock:
            if self.phase != 'ready':
                raise ManagementError('请先完成部署准备。', 409)
            if self.check_host and os.geteuid() != 0:
                raise ManagementError('系统部署需要管理员权限，请使用 sudo rdp-auth deploy。', 403)
            self.phase = 'installing'
            self.worker = threading.Thread(target=lambda: self._run(self._apply), daemon=False)
            self.worker.start()
        return self.status()

    def _apply(self):
        layout = self.layout
        # A host-wide lock prevents two independent wizard processes installing concurrently.
        lock_path = layout.path('/run/lock/rdp-access-auth-deploy.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            preflight(layout, self.port, self.check_host)
            for name, digest in self.artifacts.items():
                if hashlib.sha256((self.stage / name).read_bytes()).hexdigest() != digest:
                    raise ManagementError('待部署文件已变化，请重新运行向导。')
            self.progress('写入私密配置和已校验的运行文件')
            layout.config_dir.mkdir(mode=0o700, parents=True)
            layout.connector_dir.mkdir(mode=0o755, parents=True)
            for name, target in [('settings.json', layout.config), ('objects.json', layout.words), ('token', layout.token)]:
                atomic_private_write(target, (self.stage / name).read_bytes())
            shutil.copyfile(self.stage / 'cloudflared', layout.connector)
            layout.connector.chmod(0o755)
            layout.units.mkdir(parents=True, exist_ok=True)
            for name, content in unit_files(self.port).items():
                with (layout.units / name).open('x') as unit:
                    unit.write(content)
                (layout.units / name).chmod(0o644)
            receipt = dict(version=(ROOT / 'VERSION').read_text().strip(), port=self.port,
                           status='installed', units=list(UNITS), created=int(time.time()))
            atomic_private_write(layout.receipt, json.dumps(receipt).encode())
            try:
                restorecon = shutil.which('restorecon') if self.check_host else None
                if restorecon:
                    self.runner([restorecon, '-RF', str(layout.config_dir), str(layout.connector_dir),
                                 *(str(layout.units / name) for name in UNITS)])
                self.runner(['systemctl', 'daemon-reload'])
                self.progress('启动认证服务并执行健康检查')
                self.runner(['systemctl', 'enable', '--now', UNITS[0]])
                self.health(self.settings['hostname'], self.port)
                self.progress('启动 Cloudflare Tunnel 连接器')
                self.runner(['systemctl', 'enable', '--now', UNITS[1]])
                self.runner(['systemctl', 'is-active', '--quiet', *UNITS])
            except Exception:
                for unit in reversed(UNITS):
                    try:
                        self.runner(['systemctl', 'disable', '--now', unit])
                    except Exception:
                        pass
                receipt['status'] = 'needs-attention'
                atomic_private_write(layout.receipt, json.dumps(receipt).encode())
                raise ManagementError('部署文件已保留，但服务启动或健康检查失败。已尝试停止本次服务，请用 rdp-auth --profile system gui 检查配置与日志后再启动。') from None
            receipt['status'] = 'running'
            atomic_private_write(layout.receipt, json.dumps(receipt).encode())
            self.progress('本机部署完成。请通过认证域名验证 HTTPS 路由与实际登录。')
            with self.lock:
                self.phase = 'complete'

    def close(self):
        if self.worker:
            self.worker.join()
        self.workspace.cleanup()


def elevate_if_needed(arguments):
    if os.geteuid() == 0:
        return False
    if not shutil.which('sudo'):
        raise ManagementError('系统未安装 sudo。请使用管理员账户运行 rdp-auth deploy。')
    if not Path('/usr/bin/rdp-auth').is_file():
        raise ManagementError('请先安装 RPM/DEB，再运行部署向导。')
    print('部署需要管理员权限，将由 sudo 请求系统密码。', flush=True)
    os.execvp('sudo', ['sudo', '--', '/usr/bin/rdp-auth', *arguments])


def run_wizard(gui=False, port=18124, no_open=False):
    if not gui and not os.isatty(0):
        raise ManagementError('终端部署向导需要交互终端；图形模式请使用 deploy --gui。')
    elevation = ['deploy'] + (['--gui', '--port', str(port)] if gui else []) + (['--no-open'] if no_open else [])
    elevate_if_needed(elevation)
    deployment = Deployment()
    try:
        if gui:
            from management_web import run_gui
            target = Target(deployment.stage / 'draft.json', deployment.stage / 'state.sqlite3', UNITS[0], deployment.stage)
            run_gui(target, port, not no_open, deployment=deployment)
            return
        from rdp_manager import new_password, secret
        # Stop before collecting secrets if installation cannot proceed on this host.
        conflicts = deployment.layout.conflicts()
        if conflicts:
            raise ManagementError('检测到已有部署，请使用管理功能维护原部署。冲突位置：' + '、'.join(conflicts), 409)
        print('RDP Access Auth 首次部署向导。已有远程桌面、SakuraFrp 隧道和 Cloudflare 路由应已准备好。')
        changes = dict(hostname=input('认证域名：').strip(), rdp_address=input('RDP 地址（域名:端口）：').strip())
        try:
            changes['tunnel_id'] = int(input('SakuraFrp 隧道 ID：').strip())
            listen_port = int(input('本机认证端口 [18089]：').strip() or '18089')
        except ValueError:
            raise ManagementError('隧道 ID 和端口必须为整数。') from None
        changes['password'] = new_password()
        changes['sakura_token'] = secret('SakuraFrp API Token：').strip()
        token = secret('Cloudflare Tunnel Token（只粘贴 Token）：').strip()
        site = input('Turnstile Site key（可留空）：').strip()
        if site:
            changes.update(turnstile_site_key=site, turnstile_secret_key=secret('Turnstile Secret key：').strip())
        words = input('已有中文词库绝对路径（留空自动下载）：').strip()
        if words:
            changes['wordlist_path'] = words
        deployment.prepare(changes, token, listen_port)
        seen = 0
        while deployment.worker.is_alive():
            events = deployment.status()['events']
            for event in events[seen:]:
                print(event, flush=True)
            seen = len(events)
            deployment.worker.join(timeout=0.5)
        status = deployment.status()
        if status['phase'] != 'ready':
            raise ManagementError(status['error'])
        print(json.dumps(status['plan'], ensure_ascii=False, indent=2))
        print(f'请确认 Cloudflare 路由：{changes["hostname"]} → http://127.0.0.1:{listen_port}')
        if input('安装上述配置并启用两个系统服务？[y/N]：').lower() not in ('y', 'yes'):
            print('已取消，未写入系统。')
            return
        deployment.apply()
        while deployment.worker.is_alive():
            deployment.worker.join(timeout=0.5)
        status = deployment.status()
        if status['phase'] != 'complete':
            raise ManagementError(status['error'])
        print(status['message'] + '\nhttps://' + changes['hostname'])
    finally:
        deployment.close()
