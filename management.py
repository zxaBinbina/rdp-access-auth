"""Shared local management operations. Never import or initialize the public portal."""
from contextlib import closing, contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
PACKAGED = (ROOT / 'PACKAGED.json').is_file()
DATA_ROOT = (Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'rdp-access-auth'
             if PACKAGED else ROOT)
PUBLIC_FIELDS = ('hostname', 'rdp_address', 'tunnel_id', 'turnstile_site_key', 'wordlist_path')
EDITABLE_FIELDS = set(PUBLIC_FIELDS) | {'password', 'sakura_token', 'turnstile_secret_key', 'disable_turnstile'}


class ManagementError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    config: Path
    state: Path
    service: str
    runtime: Path
    profile: str = 'local'
    scope: str = 'system'

    def describe(self):
        return dict(config=str(self.config), state=str(self.state), service=self.service,
                    runtime=str(self.runtime), profile=self.profile, scope=self.scope)


def select_target(profile='local', config=None, state=None, service=None, scope='system'):
    profiles = {
        'local': (DATA_ROOT / 'private/portal-settings.json', DATA_ROOT / 'private/state.sqlite3', 'rdp-access-auth.service', DATA_ROOT),
        'system': (Path('/etc/rdp-access-auth/portal-settings.json'), Path('/var/lib/rdp-access-auth/state.sqlite3'),
                   'rdp-access-auth.service', Path('/opt/rdp-access-auth')),
        'legacy': (Path('/etc/rdp-auth/portal-settings.json'), Path('/var/lib/rdp-auth/state.sqlite3'),
                   'rdp-auth.service', Path('/usr/local/lib/rdp-auth')),
    }
    cfg, db, unit, runtime = profiles[profile]
    unit = service or unit
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*\.service', unit):
        raise ManagementError('服务名必须是有效的 .service 单元名称。')
    return Target(Path(config).expanduser().absolute() if config else cfg,
                  Path(state).expanduser().absolute() if state else db, unit, runtime, profile, scope)


def io_error(exc):
    if isinstance(exc, PermissionError):
        return ManagementError('没有访问权限。系统部署请在终端使用 sudo ./rdp-auth 执行同一命令；本地配置可使用 --profile local。', 403)
    return ManagementError('文件或数据库操作失败，请检查路径、文件权限和可用磁盘空间。', 500)


def read_config(path, missing_ok=False):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if missing_ok:
            return {}, 'missing'
        raise ManagementError('尚未创建配置。请使用 config init 或在浏览器中填写并保存配置。', 404)
    except OSError as exc:
        raise io_error(exc) from exc
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ManagementError('配置文件不是有效的 UTF-8 JSON；请修复原文件后重试。') from None
    if not isinstance(value, dict):
        raise ManagementError('配置文件必须是 JSON 对象。')
    return value, hashlib.sha256(raw).hexdigest()


def public_config(value):
    return {**{k: value.get(k, '') for k in PUBLIC_FIELDS},
            'has_password': bool(value.get('password_hash')),
            'has_sakura_token': bool(value.get('sakura_token')),
            'has_turnstile_secret': bool(value.get('turnstile_secret_key'))}


def hostname_valid(value):
    return (isinstance(value, str) and len(value) <= 253 and '.' in value and
            all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in value.split('.')))


def rdp_valid(value):
    if not isinstance(value, str) or len(value) > 320:
        return False
    match = re.fullmatch(r'(\[[0-9a-fA-F:]+\]|[^:\s/]+):([0-9]{1,5})', value)
    if not match or not 1 <= int(match[2]) <= 65535:
        return False
    host = match[1]
    try:
        ipaddress.ip_address(host.strip('[]'))
        return True
    except ValueError:
        return hostname_valid(host)


def validate_config(value):
    if not hostname_valid(value.get('hostname')):
        raise ManagementError('认证域名应为小写完整域名，不含协议、路径或端口。')
    if not rdp_valid(value.get('rdp_address')):
        raise ManagementError('RDP 地址应为 域名:端口、IPv4:端口 或 [IPv6]:端口，端口范围 1～65535。')
    if type(value.get('tunnel_id')) is not int or not 0 < value['tunnel_id'] <= 2147483647:
        raise ManagementError('SakuraFrp 隧道 ID 必须为 1～2147483647 的整数。')
    for key, label in [('sakura_token', 'SakuraFrp API Token'), ('turnstile_site_key', 'Turnstile Site key'),
                       ('turnstile_secret_key', 'Turnstile Secret key')]:
        text = value.get(key, '')
        if not isinstance(text, str) or len(text) > 4096 or any(c.isspace() or ord(c) < 32 for c in text):
            raise ManagementError(label + ' 格式不正确。')
    if not value.get('sakura_token'):
        raise ManagementError('SakuraFrp API Token 不能为空。')
    if bool(value.get('turnstile_site_key')) != bool(value.get('turnstile_secret_key')):
        raise ManagementError('启用 Turnstile 需要同时设置 Site key 和 Secret key。')
    for key, length in [('password_salt', 32), ('password_hash', 128)]:
        if not isinstance(value.get(key), str) or not re.fullmatch('[a-fA-F0-9]{' + str(length) + '}', value[key]):
            raise ManagementError('密码哈希或盐无效，请通过“更换固定密码”重新设置。')
    if not isinstance(value.get('session_key'), str) or len(value['session_key']) < 32:
        raise ManagementError('原会话密钥无效，请从备份恢复；不可重新初始化已有凭据。')
    if 'wordlist_path' in value and (not isinstance(value['wordlist_path'], str) or
                                   not value['wordlist_path'] or '\x00' in value['wordlist_path'] or
                                   not Path(value['wordlist_path']).is_absolute()):
        raise ManagementError('自定义词库路径必须是绝对路径；留空使用默认词库。')


def hash_password(password):
    if (not isinstance(password, str) or not 16 <= len(password) <= 128 or
            not all(re.search(r, password) for r in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^A-Za-z0-9\s]'))):
        raise ManagementError('固定密码需为 16～128 位，包含大写字母、小写字母、数字和特殊符号。')
    salt = secrets.token_hex(16)
    return salt, hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


@contextmanager
def config_lock(path):
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
    except OSError as exc:
        raise io_error(exc) from exc


def atomic_private_write(path, data, owner=None):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            os.fchmod(f.fileno(), 0o600)
            if owner and os.geteuid() == 0:
                os.fchown(f.fileno(), *owner)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_config(target, changes, revision=None, create=False):
    if not isinstance(changes, dict) or set(changes) - EDITABLE_FIELDS:
        raise ManagementError('包含未知或不可修改的配置字段。')
    for key in ('password', 'sakura_token', 'turnstile_secret_key'):
        if key in changes and not isinstance(changes[key], str):
            raise ManagementError('密码和密钥必须是字符串。')
    with config_lock(target.config):
        if target.config.is_symlink():
            raise ManagementError('配置文件是符号链接，请显式指定真实配置路径。')
        previous, current_revision = read_config(target.config, missing_ok=True)
        if create and current_revision != 'missing':
            raise ManagementError('配置已经存在。请使用 config set 修改，初始化不会覆盖文件。', 409)
        if not create and current_revision == 'missing':
            raise ManagementError('配置不存在，请先创建配置。', 404)
        if revision is not None and revision != current_revision:
            raise ManagementError('配置已被其他窗口或命令修改。请重新载入后再保存。', 409)
        value = previous.copy()
        if create:
            value.update(session_key=secrets.token_hex(32), turnstile_site_key='', turnstile_secret_key='')
        for key in PUBLIC_FIELDS:
            if key in changes:
                v = changes[key]
                if key == 'wordlist_path' and v == '':
                    value.pop(key, None)
                else:
                    value[key] = v
        for key in ('sakura_token', 'turnstile_secret_key'):
            if key in changes and changes[key] != '':
                value[key] = changes[key]
        if changes.get('password'):
            value['password_salt'], value['password_hash'] = hash_password(changes['password'])
        if 'disable_turnstile' in changes and type(changes['disable_turnstile']) is not bool:
            raise ManagementError('Turnstile 开关必须为布尔值。')
        if changes.get('disable_turnstile'):
            value.update(turnstile_site_key='', turnstile_secret_key='')
        validate_config(value)
        data = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode()
        owner = None
        if previous:
            stat = target.config.stat()
            owner = (stat.st_uid, stat.st_gid)
            atomic_private_write(Path(str(target.config) + '.bak'), target.config.read_bytes(), owner)
        atomic_private_write(target.config, data, owner)
    return dict(config=public_config(value), revision=hashlib.sha256(data).hexdigest(),
                message='配置已保存。运行中的认证服务需要重启后生效。' if previous else '配置已创建。可继续准备词库并启动认证服务。',
                hostname_changed=bool(previous and previous.get('hostname') != value['hostname']))


def check_wordlist(target, value):
    path = target.runtime / 'wordlists/objects.json'
    try:
        if value.get('wordlist_path'):
            if not isinstance(value['wordlist_path'], str):
                raise ValueError()
            path = Path(value['wordlist_path'])
        words = json.loads(path.read_bytes())
        if (not isinstance(words, list) or any(not isinstance(w, str) or not re.fullmatch(r'[\u4e00-\u9fff]{2,}', w) for w in words)
                or len(set(words)) < 2048):
            raise ValueError()
        return dict(ok=True, path=str(path), count=len(set(words)))
    except (OSError, ValueError, TypeError):
        return dict(ok=False, path=str(path), message='词库缺失、不可读或不足 2048 个有效中文词。请运行 ./rdp-auth wordlist --download，或配置已有词库的绝对路径。')


def state_operation(target, unlock=False):
    try:
        if not target.state.is_file():
            raise ManagementError('状态数据库尚未生成，或当前用户无权访问其目录。请核对路径和权限；首次启动认证服务后会生成数据库。', 404)
        uri = target.state.as_uri() + ('?mode=rw' if unlock else '?mode=ro')
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as db, db:
            if unlock:
                db.execute('BEGIN IMMEDIATE')
                db.execute('UPDATE guard_global SET until=0 WHERE id=1')
                for table in ('guard_ips', 'guard_bans', 'guard_requests', 'guard_options'):
                    db.execute('DELETE FROM ' + table)
            now = int(time.time())
            return dict(passkeys=db.execute('SELECT count(*) FROM passkeys').fetchone()[0],
                        temporary_generation=db.execute('SELECT generation FROM temporary_password WHERE id=1').fetchone()[0],
                        global_lock_seconds=max(0, db.execute('SELECT until FROM guard_global WHERE id=1').fetchone()[0] - now),
                        banned_ips=db.execute('SELECT count(*) FROM guard_ips WHERE until>?', (now,)).fetchone()[0])
    except OSError as exc:
        raise io_error(exc) from exc
    except (sqlite3.Error, TypeError):
        raise ManagementError('无法读取状态数据库。请核对路径、权限以及认证服务是否已完成初始化。') from None


def service_operation(target, action='status'):
    if action not in ('status', 'start', 'stop', 'restart', 'logs'):
        raise ManagementError('不支持的服务操作。')
    if target.profile == 'local':
        if action == 'status':
            return dict(LoadState='local', ActiveState='local', SubState='local')
        raise ManagementError('本地模式不控制已部署服务。请使用 ./rdp-auth serve 运行本项目；管理 systemd 需显式选择 --profile system 或 --profile legacy。')
    scope = ['--user'] if target.scope == 'user' else []
    if action == 'logs':
        command = ['journalctl', *scope, '--unit', target.service, '--lines', '100', '--no-pager', '--output', 'short-iso']
    elif action == 'status':
        command = ['systemctl', *scope, '--no-ask-password', 'show', target.service,
                   '--property=LoadState,ActiveState,SubState,WorkingDirectory', '--no-pager']
    else:
        command = ['systemctl', *scope, '--no-ask-password', action, target.service]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=8 if action in ('status', 'logs') else 45)
    except FileNotFoundError:
        raise ManagementError('未找到 systemd 命令；当前系统可使用 serve 前台运行认证服务。', 503) from None
    except subprocess.TimeoutExpired:
        raise ManagementError('服务操作超时，请刷新状态确认实际结果。', 504) from None
    if result.returncode:
        raise ManagementError('systemd 操作失败。请确认服务已安装、名称正确，且当前用户具备权限；系统服务管理可使用 sudo ./rdp-auth。', 503)
    if action == 'status':
        return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    return dict(message='已执行服务操作：' + action, output=result.stdout if action == 'logs' else '')


def snapshot(target, *, include_credentials=False):
    result = dict(target=target.describe())
    try:
        value, revision = read_config(target.config, missing_ok=True)
        result.update(config=public_config(value), revision=revision, exists=revision != 'missing',
                      wordlist=check_wordlist(target, value))
        if include_credentials:
            # Only the authenticated local editor opts in; status and CLI stay redacted.
            for key in ('sakura_token', 'turnstile_secret_key'):
                result['config'][key] = value.get(key, '') if isinstance(value.get(key, ''), str) else ''
        if value:
            try:
                validate_config(value)
            except ManagementError as exc:
                result['validation_error'] = str(exc)
    except ManagementError as exc:
        result['config_error'] = str(exc)
    for name, operation in [('state', state_operation), ('service', service_operation)]:
        try:
            result[name] = operation(target)
        except ManagementError as exc:
            result[name + '_error'] = str(exc)
    return result
