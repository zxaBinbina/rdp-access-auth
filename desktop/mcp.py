"""Local stdio MCP server. User approval is never available as an MCP tool."""
import json
import os
from pathlib import Path
import stat
import sys

from desktop.control import Client, MAX_MESSAGE
from management import ManagementError


def schema(properties=None, required=()):
    return {'type': 'object', 'properties': properties or {}, 'required': list(required), 'additionalProperties': False}


STRING = {'type': 'string'}
INTEGER = {'type': 'integer'}
BOOLEAN = {'type': 'boolean'}
SETUP_FIELDS = {name: STRING for name in ('hostname', 'rdp_address', 'cf_account_id', 'cf_zone_id', 'cf_tunnel_id',
                                         'password', 'cloudflare_tunnel_token', 'turnstile_site_key', 'turnstile_secret_key', 'wordlist_path')}
SETUP_FIELDS.update({name: INTEGER for name in ('sakura_node_id', 'sakura_tunnel_id', 'rdp_port', 'admission_port', 'portal_port', 'remote_port')})
SETUP_FIELDS.update(mode={'enum': ['automatic', 'manual']}, create_turnstile=BOOLEAN)


def tool(name, method, description, input_schema=None, readonly=False, destructive=False):
    return {'name': name, 'method': method, 'description': description, 'inputSchema': input_schema or schema(),
            'annotations': {'readOnlyHint': readonly, 'destructiveHint': destructive, 'openWorldHint': method.startswith('onboarding.') and not readonly}}


TOOLS = [
    tool('get_status', 'status', '读取服务、启动策略、暂停状态及部署进度，不返回密钥。', readonly=True),
    tool('read_configuration', 'settings.read', '读取已脱敏配置及 revision，修改前必须先读取。', readonly=True),
    tool('get_setup_guide', 'onboarding.guide', '获取新用户部署步骤、平台入口、密钥权限及人工准备事项。', readonly=True),
    tool('get_environment', 'diagnostics.environment', '检查 systemd、RDP 监听和适用的 KDE 修复。', readonly=True),
    tool('request_authorization', 'agents.request', '申请此已登记 Agent 的一小时修改权限。等待用户在软件或独立终端批准；禁止索取管理密码或代替用户确认。', schema({'reason': STRING})),
    tool('inspect_platforms', 'onboarding.inspect', '保存用户提供的 API 凭据并读取 SakuraFrp 节点 / 隧道与 Cloudflare 账户 / 域名；需要授权。',
         schema({name: STRING for name in ('sakura_api_token', 'cloudflare_api_token', 'cloudflare_tunnel_token', 'cf_account_id')})),
    tool('prepare_deployment', 'onboarding.prepare', '校验部署选项并下载校验运行组件，不创建远程隧道；轮询 deployment_status 等待 ready。',
         schema(SETUP_FIELDS, ('mode', 'hostname', 'password'))),
    tool('apply_deployment', 'onboarding.apply', '应用用户批准后的部署计划，必须传入 ready 状态的 revision；创建平台资源并安装本机服务。', schema({'revision': STRING}, ('revision',))),
    tool('deployment_status', 'onboarding.status', '读取准备 / 部署进度、错误、资源 ID 与校验结果。', readonly=True),
    tool('sync_connectors', 'connectors.sync', '将已保存的认证域名、隧道和准入端口同步到连接器，并重启服务；暂停时保持暂停。'),
    tool('update_configuration', 'settings.save', '修改认证配置；保留未提供的密钥。保存后使用 restart 应用到服务。',
         schema({'revision': STRING, 'changes': {'type': 'object'}}, ('revision', 'changes'))),
    tool('update_preferences', 'preferences.save', '修改启动模式、托盘显示和主题。startup 为 boot/login/manual；theme 为 system/light/dark；palette 为 fufu-blue/hutao-red。',
         schema({'changes': schema({'startup': {'enum': ['boot', 'login', 'manual']}, 'show_tray': BOOLEAN,
                                   'theme': {'enum': ['system', 'light', 'dark']}, 'palette': {'enum': ['fufu-blue', 'hutao-red']}})}, ('changes',))),
    tool('control_services', 'services', '控制全部认证、frp 与 Cloudflare 服务。pause 跨重启保持；resume 才解除暂停。exit 可设置 disable_autostart。',
         schema({'action': {'enum': ['pause', 'resume', 'restart', 'start', 'exit']}, 'disable_autostart': BOOLEAN}, ('action',))),
    tool('read_logs', 'logs.read', '读取脱敏后的服务日志。', schema({'service': {'enum': ['portal', 'frpc', 'cloudflare']}})),
    tool('clear_access_bans', 'credentials.unlock', '解除远程访问认证封禁，保留已登记凭据。'),
    tool('remove_passkey', 'credentials.delete_passkey', '解绑指定远程访问通行密钥。', schema({'id': STRING}, ('id',)), destructive=True),
    tool('change_management_password', 'security.password', '危险操作：即使已有一小时授权，也必须请求用户单独批准；批准绑定这一次具体修改。修改后所有会话和临时授权失效。',
         schema({'password': STRING}, ('password',)), destructive=True),
    tool('set_agent_policy', 'agents.policy', '危险操作：启用或禁用已登记 Agent，始终需要用户单独批准。', schema({'id': STRING, 'disabled': BOOLEAN}, ('id', 'disabled')), destructive=True),
    tool('migration_plan', 'migration.plan', '检查旧部署迁移条件并生成计划。'),
    tool('migrate_legacy_deployment', 'migration.start', '按 revision 迁移既有部署；recover 为 true 时恢复未完成迁移。', schema({'revision': STRING, 'recover': BOOLEAN})),
    tool('migration_status', 'migration.status', '读取迁移进度及恢复结果。'),
]


def read_identity(path):
    try:
        fd = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077 or metadata.st_size > 16384:
                raise ValueError()
            value = json.load(stream)
        if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not isinstance(value.get('key'), str):
            raise ValueError()
        return {'id': value['id'], 'key': value['key']}
    except (OSError, ValueError, KeyError):
        raise ManagementError('MCP 身份文件无效。请从软件导出，并将文件权限设为仅本人读写（0600）。') from None


class MCP:
    def __init__(self, client, identity):
        self.client, self.identity, self.initialized = client, identity, False

    def dispatch(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
        request_id = message.get('id')
        method, params = message.get('method'), message.get('params') or {}
        if 'id' not in message:
            return None
        def reply(value):
            return {'jsonrpc': '2.0', 'id': request_id, 'result': value}
        if not isinstance(params, dict):
            return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'Invalid params'}}
        if method == 'initialize':
            self.initialized = True
            version = params.get('protocolVersion')
            return reply({'protocolVersion': version if version in ('2025-11-25', '2025-06-18', '2025-03-26', '2024-11-05') else '2025-11-25',
                          'capabilities': {'tools': {'listChanged': False}}, 'serverInfo': {'name': 'rdp-access-auth', 'version': '2.0.0'},
                          'instructions': '只管理本机 RDP Access Auth。先读取状态，修改前申请已登记身份的一小时授权。不要向用户索取管理密码；授权由用户在本机窗口或独立终端完成。危险操作需要单独批准。'} )
        if method == 'ping':
            return reply({})
        if not self.initialized:
            return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32000, 'message': 'Initialize first'}}
        if method == 'tools/list':
            return reply({'tools': [{key: value for key, value in item.items() if key != 'method'} for item in TOOLS]})
        if method == 'tools/call':
            selected = next((item for item in TOOLS if item['name'] == params.get('name')), None)
            if selected is None:
                return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32602, 'message': 'Unknown tool'}}
            arguments = params.get('arguments') or {}
            try:
                if not isinstance(arguments, dict):
                    raise ManagementError('工具参数必须为对象。')
                properties = selected['inputSchema']['properties']
                if set(arguments) - set(properties) or set(selected['inputSchema']['required']) - set(arguments):
                    raise ManagementError('工具参数缺失或包含未知字段。')
                rpc_method = selected['method']
                if rpc_method == 'services':
                    action = arguments.get('action')
                    if action not in ('pause', 'resume', 'restart', 'start', 'exit'):
                        raise ManagementError('无效的服务操作。')
                    rpc_method += '.' + action
                    arguments = {key: value for key, value in arguments.items() if key != 'action'}
                value = self.client.request(rpc_method, arguments, identity=self.identity)
                return reply({'content': [{'type': 'text', 'text': json.dumps(value, ensure_ascii=False)}], 'isError': False})
            except ManagementError as exc:
                return reply({'content': [{'type': 'text', 'text': json.dumps({'error': str(exc), 'code': exc.status}, ensure_ascii=False)}], 'isError': True})
        return {'jsonrpc': '2.0', 'id': request_id, 'error': {'code': -32601, 'message': 'Method not found'}}


def serve(identity_path, input_stream=None, output_stream=None, client=None):
    server = MCP(client or Client(), read_identity(identity_path))
    source, destination = input_stream or sys.stdin, output_stream or sys.stdout
    while True:
        line = source.readline(MAX_MESSAGE + 1)
        if not line:
            return 0
        if len(line.encode()) > MAX_MESSAGE or not line.endswith('\n'):
            return 1
        try:
            response = server.dispatch(json.loads(line))
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        except Exception:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32603, 'message': 'Internal error'}}
        if response is not None:
            destination.write(json.dumps(response, ensure_ascii=False) + '\n')
            destination.flush()
