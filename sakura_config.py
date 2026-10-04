"""Safe inspection of SakuraFrp/frpc TOML configuration files."""
from pathlib import Path
import ipaddress
import re

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - supported package runs on Python 3.11+
    tomllib = None

from management import ManagementError, hostname_valid, rdp_valid


def _text(value):
    return value.strip() if isinstance(value, str) else ''


def _endpoint(host, port):
    host = _text(host)
    if not host or type(port) is not int or not 1 <= port <= 65535:
        return ''
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not hostname_valid(host):
            return ''
    return f'[{host}]:{port}' if ':' in host else f'{host}:{port}'


def _read(path):
    if tomllib is None:
        raise ManagementError('当前 Python 缺少 TOML 解析器，请使用 Python 3.11 或更新版本。')
    if not isinstance(path, str) or not path or '\x00' in path:
        raise ManagementError('SakuraFrp 配置路径无效。')
    file = Path(path).expanduser()
    try:
        if file.is_symlink() or not file.is_file():
            raise ManagementError('SakuraFrp 配置必须是普通文件，不能是目录或符号链接。')
        if file.stat().st_size > 1024 * 1024:
            raise ManagementError('SakuraFrp 配置文件超过 1 MiB。')
        data = tomllib.loads(file.read_text(encoding='utf-8'))
    except ManagementError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ManagementError('SakuraFrp 配置无法读取或不是有效的 UTF-8 TOML，请检查路径、权限和语法。') from None
    if not isinstance(data, dict):
        raise ManagementError('SakuraFrp TOML 顶层必须是对象。')
    return file, data


def inspect_config(path):
    file, data = _read(path)
    auth = data.get('auth') if isinstance(data.get('auth'), dict) else {}
    common = data.get('common') if isinstance(data.get('common'), dict) else {}
    token = _text(auth.get('token')) or _text(data.get('token')) or _text(common.get('token'))
    server = _endpoint(data.get('serverAddr') or common.get('server_addr'), data.get('serverPort') or common.get('server_port'))
    proxies = data.get('proxies') if isinstance(data.get('proxies'), list) else []
    if not proxies and isinstance(data.get('rdp'), dict):
        proxies = [dict(data['rdp'], name='rdp')]
    candidates = []
    for item in proxies:
        if not isinstance(item, dict) or _text(item.get('type')).lower() not in ('tcp', 'stcp', ''):
            continue
        local = _endpoint(item.get('localIP') or item.get('local_ip'), item.get('localPort') or item.get('local_port'))
        remote = item.get('remotePort') or item.get('remote_port')
        if local:
            candidates.append(dict(name=_text(item.get('name')) or '未命名代理', type=_text(item.get('type')) or 'tcp',
                                   local_address=local, remote_port=remote if type(remote) is int else None,
                                   selected=False))
    if not candidates:
        raise ManagementError('配置中未找到可用的 TCP 代理，请确认已保存 RDP 隧道。')
    for candidate in candidates:
        candidate['selected'] = bool(re.search(r'rdp|remote|3389', candidate['name'], re.I))
    selected = next((item for item in candidates if item['selected']), candidates[0])
    return dict(path=str(file), valid=True, server=server, credential_present=bool(token),
                credential=token, proxies=candidates, suggested=selected['name'],
                warnings=[] if server else ['未检测到 SakuraFrp 服务端地址。'])


def config_changes(path, proxy_name=None):
    result = inspect_config(path)
    selected = next((item for item in result['proxies'] if item['name'] == proxy_name), None) if proxy_name else None
    selected = selected or next((item for item in result['proxies'] if item['name'] == result['suggested']), result['proxies'][0])
    if not result['credential']:
        raise ManagementError('配置中未找到 SakuraFrp Token。')
    return dict(sakura_token=result['credential'], rdp_address=selected['local_address'],
                sakura_proxy=selected['name'], sakura_config_path=result['path'])
