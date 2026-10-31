"""Narrow clients for the documented SakuraFrp v4 and Cloudflare v4 APIs."""
import configparser
import io
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode

from management import ManagementError, hostname_valid

BASES = {'sakura': 'https://api.natfrp.com/v4', 'cloudflare': 'https://api.cloudflare.com/client/v4'}
FRPC_VERSION = '0.51.0-sakura-15.1'
EXTRA = {'auth_mode': 'server', 'auth_time': '6h', 'proxy_protocol_version': 'v2'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(provider, method, path, token, data=None, text=False):
    if provider not in BASES or not path.startswith('/') or '\r' in path or '\n' in path:
        raise ManagementError('平台请求路径无效。')
    if not isinstance(token, str) or not token or len(token) > 16384 or any(ch.isspace() or ord(ch) < 32 for ch in token):
        raise ManagementError('请填写有效的平台 API 密钥。')
    headers = {'Authorization': 'Bearer ' + token, 'User-Agent': 'rdp-access-auth/2.0', 'Accept': 'application/json'}
    payload = json.dumps(data).encode() if data is not None else None
    if payload is not None:
        headers['Content-Type'] = 'application/json'
    req = urllib.request.Request(BASES[provider] + path, data=payload, headers=headers, method=method)
    label = 'SakuraFrp' if provider == 'sakura' else 'Cloudflare'
    try:
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=25) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise ManagementError(label + ' 返回的数据过大，请缩小查询范围。')
        if text:
            return raw.decode('utf-8')
        value = json.loads(raw)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ManagementError(label + ' 拒绝访问，请检查密钥、权限范围和账户状态。', 422) from None
        if exc.code == 429:
            raise ManagementError(label + ' 请求过于频繁，请稍后重试。', 429) from None
        raise ManagementError(label + ' 请求未完成（HTTP ' + str(exc.code) + '），请检查平台状态和填写的选项。', 502) from None
    except (OSError, urllib.error.URLError, ValueError):
        raise ManagementError('无法连接 ' + label + ' 或返回格式不正确，请检查网络后重试。', 502) from None
    if provider == 'cloudflare':
        if not isinstance(value, dict) or value.get('success') is not True:
            raise ManagementError('Cloudflare 未接受此操作，请检查密钥权限和配置。', 422)
        return value
    return value


def extra_config(previous=''):
    lines = [line for line in previous.splitlines() if line.split('=', 1)[0].strip() not in EXTRA]
    return '\n'.join(lines + [key + ' = ' + value for key, value in EXTRA.items()])


def validate_frpc_config(content, admission_port):
    """Never launch an arbitrary plugin or an unprotected RDP target from a config."""
    if not isinstance(content, str) or len(content) > 512 * 1024:
        raise ManagementError('平台返回的 frpc 配置无效。')
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(content)
        tunnels = [section for section in parser.sections() if section != 'common']
        if len(tunnels) != 1 or not parser.has_section('common'):
            raise ValueError()
        tunnel = parser[tunnels[0]]
        if (tunnel.get('type') != 'tcp' or tunnel.get('local_ip') != '127.0.0.1'
                or tunnel.getint('local_port') != admission_port or tunnel.get('auth_mode') != 'server'
                or tunnel.get('proxy_protocol_version') != 'v2' or tunnel.get('auth_time') != '6h'):
            raise ValueError()
        for section in parser.values():
            if any(key.startswith(('plugin', 'start', 'includes')) for key in section):
                raise ValueError()
    except (configparser.Error, ValueError, KeyError):
        raise ManagementError('frpc 配置未通过检查：必须只包含一个 TCP 隧道，指向本机准入端口，并启用 server 认证、6h 有效期和 PROXY v2。') from None
    return content


class Sakura:
    def __init__(self, token, transport=request, checkpoint=lambda: None):
        self.token, self.transport, self.checkpoint = token, transport, checkpoint

    def call(self, method, path, data=None, text=False):
        if method != 'GET':
            self.checkpoint()
        return self.transport('sakura', method, path, self.token, data, text)

    def inventory(self):
        user = self.call('GET', '/user/info')
        if not isinstance(user, dict) or user.get('ban'):
            raise ManagementError('SakuraFrp 账户状态异常，请先在平台处理。')
        nodes = self.call('GET', '/nodes')
        tunnels = self.call('GET', '/tunnels')
        if not isinstance(nodes, dict) or not isinstance(tunnels, list):
            raise ManagementError('SakuraFrp 节点或隧道数据格式无效。')
        level = (user.get('group') or {}).get('level', 0)
        return {'user': {'name': str(user.get('name', '')), 'realname': user.get('realname', 0)},
                'nodes': [{'id': int(key), 'name': str(node.get('name', key)), 'host': node.get('host', ''),
                           'available': bool(node.get('flag', 0) & 4) and not node.get('flag', 0) & 512 and node.get('vip', 0) <= level}
                          for key, node in nodes.items() if str(key).isdigit()],
                'tunnels': [{key: tunnel.get(key) for key in ('id', 'name', 'node', 'type', 'remote', 'local_ip', 'local_port', 'online')}
                            for tunnel in tunnels if tunnel.get('type') == 'tcp']}

    def tunnel(self, tunnel_id):
        tunnels = self.call('GET', '/tunnels')
        match = next((item for item in tunnels if item.get('id') == tunnel_id), None)
        if not match or match.get('type') != 'tcp':
            raise ManagementError('未找到所选的 SakuraFrp TCP 隧道，请重新选择。')
        return match

    def create(self, name, node, port, remote=''):
        data = {'name': name, 'type': 'tcp', 'node': node, 'local_ip': '127.0.0.1', 'local_port': port,
                'note': 'RDP Access Auth 2', 'extra': extra_config()}
        if remote:
            data['remote'] = str(remote)
        result = self.call('POST', '/tunnels', data)
        if not isinstance(result, dict) or type(result.get('id')) is not int:
            raise ManagementError('平台没有返回创建后的隧道 ID，请检查平台后再重试。')
        return result

    def secure_existing(self, tunnel_id, port):
        old = self.tunnel(tunnel_id)
        self.call('POST', '/tunnel/edit', {'id': tunnel_id, 'local_ip': '127.0.0.1', 'local_port': port,
                                          'extra': extra_config(old.get('extra') or '')})
        return old

    def config(self, tunnel_id, port):
        content = self.call('POST', '/tunnel/config', {'query': str(tunnel_id), 'frpc': FRPC_VERSION}, text=True)
        return validate_frpc_config(content, port)

    def address(self, tunnel_id):
        tunnel = self.tunnel(tunnel_id)
        nodes = self.call('GET', '/nodes')
        node = nodes.get(str(tunnel['node']))
        remote = str(tunnel.get('remote', ''))
        if not node or not hostname_valid(node.get('host')) or not remote.isdecimal() or not 1 <= int(remote) <= 65535:
            raise ManagementError('无法确定公网 RDP 地址，请核对节点地址和端口。')
        return node['host'] + ':' + remote


def cf_id(value, uuid=False):
    expression = r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}' if uuid else r'[0-9a-fA-F]{32}'
    if not isinstance(value, str) or not re.fullmatch(expression, value):
        raise ManagementError('Cloudflare 账户、域名或隧道 ID 无效。')
    return value


class Cloudflare:
    def __init__(self, token, transport=request, checkpoint=lambda: None):
        self.token, self.transport, self.checkpoint = token, transport, checkpoint

    def call(self, method, path, data=None):
        if method != 'GET':
            self.checkpoint()
        return self.transport('cloudflare', method, path, self.token, data, False)

    def all(self, path):
        result = []
        for page in range(1, 21):
            separator = '&' if '?' in path else '?'
            reply = self.call('GET', path + separator + urlencode({'page': page, 'per_page': 50}))
            if not isinstance(reply.get('result'), list):
                raise ManagementError('Cloudflare 列表格式不正确。')
            result.extend(reply['result'])
            if page >= reply.get('result_info', {}).get('total_pages', 1):
                return result
        raise ManagementError('Cloudflare 账户资源过多，请缩小 API 密钥可访问的域名范围。')

    def inventory(self):
        zones = self.all('/zones?status=active')
        accounts = {}
        for zone in zones:
            account = zone.get('account') or {}
            if account.get('id'):
                accounts[account['id']] = {'id': account['id'], 'name': account.get('name', account['id'])}
        return {'accounts': list(accounts.values()),
                'zones': [{'id': zone['id'], 'name': zone['name'], 'account_id': (zone.get('account') or {}).get('id')}
                          for zone in zones]}

    def tunnels(self, account):
        return [{key: item.get(key) for key in ('id', 'name', 'status', 'config_src')}
                for item in self.all('/accounts/' + cf_id(account) + '/cfd_tunnel?is_deleted=false')]

    def create(self, account, name):
        import base64
        import secrets
        return self.call('POST', '/accounts/' + cf_id(account) + '/cfd_tunnel',
                         {'name': name, 'config_src': 'cloudflare', 'tunnel_secret': base64.b64encode(secrets.token_bytes(32)).decode()})['result']

    def route(self, account, zone, tunnel, hostname, port):
        cf_id(account); cf_id(zone); cf_id(tunnel, uuid=True)
        if not hostname_valid(hostname) or type(port) is not int or not 1024 <= port <= 65535:
            raise ManagementError('认证域名或本机端口无效。')
        path = '/accounts/' + account + '/cfd_tunnel/' + tunnel
        config_reply = self.call('GET', path + '/configurations')['result'] or {}
        config = dict(config_reply.get('config') or {})
        ingress = list(config.get('ingress') or [])
        wanted = {'hostname': hostname, 'service': 'http://127.0.0.1:' + str(port)}
        existing = next((entry for entry in ingress if entry.get('hostname') == hostname), None)
        if existing and existing.get('service') != wanted['service']:
            raise ManagementError('该认证域名已指向其他服务，请更换子域名或先在 Cloudflare 中确认原配置。', 409)
        dns_path = '/zones/' + zone + '/dns_records'
        records = self.all(dns_path + '?' + urlencode({'name': hostname}))
        content = tunnel + '.cfargotunnel.com'
        if records:
            if len(records) != 1 or not all(record.get('type') == 'CNAME' and record.get('content') == content and record.get('proxied') for record in records):
                raise ManagementError('该认证域名已有其他 DNS 记录，请更换子域名，避免覆盖原服务。', 409)
            record = records[0]
        else:
            record = self.call('POST', dns_path, {'type': 'CNAME', 'name': hostname, 'content': content, 'proxied': True, 'ttl': 1})['result']
        if not existing:
            catchall = [entry for entry in ingress if not entry.get('hostname') and not entry.get('path')]
            config['ingress'] = [entry for entry in ingress if entry not in catchall] + [wanted] + (catchall or [{'service': 'http_status:404'}])
            self.call('PUT', path + '/configurations', {'config': config})
        token = self.call('GET', path + '/token')['result']
        if not isinstance(token, str) or not token:
            raise ManagementError('Cloudflare 未返回有效的隧道运行凭据。')
        return {'token': token, 'dns_record_id': record['id']}

    def turnstile(self, account, hostname):
        path = '/accounts/' + cf_id(account) + '/challenges/widgets'
        name = 'RDP Access Auth ' + hostname
        found = next((widget for widget in self.all(path) if widget.get('name') == name and widget.get('domains') == [hostname]), None)
        result = self.call('GET', path + '/' + found['sitekey'])['result'] if found else self.call('POST', path,
                           {'name': 'RDP Access Auth ' + hostname, 'domains': [hostname], 'mode': 'managed'})['result']
        return {'turnstile_site_key': result['sitekey'], 'turnstile_secret_key': result['secret']}
