"""Fixed-scope service operations and durable pause/startup preferences."""
import json
import os
from pathlib import Path
import subprocess

from management import ManagementError, atomic_private_write

RUNTIME_UNITS = ('rdp-access-auth.service', 'rdp-access-auth-frpc.service', 'cloudflared-rdp-access.service')
DEFAULTS = {'startup': 'boot', 'paused': False, 'show_tray': True, 'palette': 'fufu-blue', 'theme': 'system'}


def run(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=50)
    except (OSError, subprocess.TimeoutExpired):
        raise ManagementError('服务操作未完成，请检查系统日志后重试。', 503) from None
    if result.returncode:
        raise ManagementError('系统未能完成服务操作：' + command[1] + '。请在运行维护中查看日志。', 503)
    return result.stdout


class Services:
    def __init__(self, root=Path('/'), runner=run):
        self.root, self.runner = Path(root), runner
        self.directory = self.root / 'etc/rdp-access-auth-control'
        self.path = self.directory / 'preferences.json'
        self.pause_marker = self.directory / 'paused'
        self.prefs = {**DEFAULTS, **(json.loads(self.path.read_text()) if self.path.exists() else {})}

    def units(self):
        if (self.root / 'etc/rdp-auth/portal-settings.json').is_file() and not (self.root / 'etc/rdp-access-auth/portal-settings.json').exists():
            return ('rdp-auth.service', 'frpc.service', 'cloudflared-rdp-auth.service')
        return RUNTIME_UNITS

    def installed(self):
        return any((self.root / p).exists() for p in ['etc/rdp-access-auth/portal-settings.json', 'etc/rdp-auth/portal-settings.json'])

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.prefs['paused']:
            atomic_private_write(self.pause_marker, b'Paused by user; resume through RDP Access Auth.\n')
        atomic_private_write(self.path, (json.dumps(self.prefs, indent=2) + '\n').encode())
        if not self.prefs['paused']:
            self.pause_marker.unlink(missing_ok=True)

    def sync_startup(self):
        if self.installed():
            enabled = self.prefs['startup'] == 'boot' and not self.prefs['paused']
            self.runner(['systemctl', 'enable' if enabled else 'disable', *self.units()])

    def preferences(self, changes):
        if not isinstance(changes, dict) or set(changes) - set(DEFAULTS) or 'paused' in changes:
            raise ManagementError('包含未知设置；暂停状态请使用暂停 / 继续操作修改。')
        choices = {'startup': ('boot', 'login', 'manual'), 'palette': ('fufu-blue', 'hutao-red'), 'theme': ('system', 'light', 'dark')}
        for key, allowed in choices.items():
            if key in changes and changes[key] not in allowed:
                raise ManagementError('设置选项无效：' + key)
        if 'show_tray' in changes and type(changes['show_tray']) is not bool:
            raise ManagementError('托盘图标开关必须为布尔值。')
        self.prefs.update(changes)
        self.save()
        if 'startup' in changes:
            self.sync_startup()
        return dict(self.prefs)

    def action(self, action, disable_autostart=False):
        if action not in ('pause', 'resume', 'restart', 'exit', 'start', 'login'):
            raise ManagementError('不支持此服务操作。')
        if type(disable_autostart) is not bool:
            raise ManagementError('自启动选择无效。')
        if not self.installed():
            if action == 'exit':
                self.prefs['startup'] = 'manual' if disable_autostart else 'boot'
                self.save()
                return {'message': '尚未部署服务，可以关闭程序。', 'preferences': dict(self.prefs)}
            raise ManagementError('请先完成首次配置向导。', 409)
        if action == 'login' and (self.prefs['startup'] != 'login' or self.prefs['paused']):
            return {'message': '按照当前启动设置保持服务状态。', 'preferences': dict(self.prefs)}
        if action == 'pause':
            self.prefs['paused'] = True
            self.save()  # Stop intent survives a daemon crash or system reboot.
            self.sync_startup()
            self.runner(['systemctl', 'stop', *reversed(self.units())])
        elif action == 'exit':
            self.prefs['startup'] = 'manual' if disable_autostart else 'boot'
            self.save()
            self.sync_startup()
            self.runner(['systemctl', 'stop', *reversed(self.units())])
        else:
            if self.prefs['paused'] and action != 'resume':
                raise ManagementError('服务处于暂停状态，请先点击“继续服务”。', 409)
            if action == 'resume':
                self.prefs['paused'] = False
                self.save()
            self.sync_startup()
            try:
                self.runner(['systemctl', 'restart' if action == 'restart' else 'start', *self.units()])
                self.runner(['systemctl', 'is-active', '--quiet', *self.units()])
            except ManagementError:
                # Keep failed services visible; never falsely report that a start succeeded.
                raise
        return {'message': {'pause': '全部服务已暂停，将保持暂停直到手动继续。', 'resume': '全部服务已恢复。',
                            'restart': '全部服务已重启。', 'exit': '全部服务已停止，可以退出程序。',
                            'start': '全部服务已启动。', 'login': '已按登录启动设置启动服务。'}[action],
                'preferences': dict(self.prefs)}

    def status(self):
        result = []
        for unit in self.units():
            try:
                raw = self.runner(['systemctl', 'show', unit, '--property=LoadState,ActiveState,SubState', '--no-pager'])
                values = dict(line.split('=', 1) for line in (raw or '').splitlines() if '=' in line)
                result.append({'unit': unit, **values})
            except ManagementError as exc:
                result.append({'unit': unit, 'ActiveState': 'unknown', 'error': str(exc)})
        return result

    def logs(self, name, secrets=()):
        aliases = dict(zip(('portal', 'frpc', 'cloudflare'), self.units()))
        if name not in aliases:
            raise ManagementError('请选择认证、frp 或 Cloudflare 服务。')
        text = self.runner(['journalctl', '-u', aliases[name], '-n', '100', '--no-pager', '--output=short-iso'])
        for secret in secrets:
            if isinstance(secret, str) and len(secret) >= 4:
                text = text.replace(secret, '[已隐藏]')
        return {'output': text[-65536:]}
