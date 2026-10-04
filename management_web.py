"""Loopback-only management server, independent of the public authentication app."""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pwd
import secrets
import socket
import threading
from urllib.parse import urlsplit
import webbrowser

from management import (ROOT, ManagementError, save_config, service_operation,
                        snapshot, state_operation)
from sakura_config import inspect_config

ASSETS = {'/': ('index.html', 'text/html; charset=utf-8'),
          '/deploy.js': ('deploy.js', 'text/javascript; charset=utf-8'),
          '/deploy.css': ('deploy.css', 'text/css; charset=utf-8'),
          '/app.css': ('app.css', 'text/css; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8')}

ASSETS['/rdp-access-auth.png'] = ('rdp-access-auth.png', 'image/png')
ASSETS['/icons.svg'] = ('icons.svg', 'image/svg+xml')
ASSETS['/management.css'] = ('management.css', 'text/css; charset=utf-8')
ASSETS['/theme.js'] = ('theme.js', 'text/javascript; charset=utf-8')


def create_server(target, port=18124, deployment=None, migration=None):
    token = secrets.token_urlsafe(32)
    if migration is None and deployment is None and target.profile in ('legacy', 'system'):
        from migration import Migration
        migration = Migration(target)

    class Handler(BaseHTTPRequestHandler):
        # The server deliberately logs neither URLs nor credentials.
        def log_message(self, format, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def reply(self, status, body, content_type='application/json; charset=utf-8'):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def check_request(self, api=False):
            expected_host = f'127.0.0.1:{self.server.server_port}'
            if self.headers.get('Host') != expected_host:
                raise ManagementError('管理入口仅接受本机地址。', 403)
            origin = self.headers.get('Origin')
            if ((origin is not None and origin != 'http://' + expected_host) or
                    self.headers.get('Sec-Fetch-Site') == 'cross-site'):
                raise ManagementError('提交来源不匹配，请使用终端输出的管理链接。', 403)
            if api and not hmac.compare_digest(self.headers.get('Authorization', '').encode(), ('Bearer ' + token).encode()):
                raise ManagementError('管理连接已失效，请重新打开终端输出的完整管理链接。', 401)

        def do_GET(self):
            try:
                path = urlsplit(self.path).path
                self.check_request(api=path.startswith('/api/'))
                if path in ASSETS:
                    name, mime = ASSETS[path]
                    if path == '/' and deployment:
                        name = 'deploy.html'
                    self.reply(200, (ROOT / 'admin_ui' / name).read_bytes(), mime)
                elif path == '/api/deployment' and deployment:
                    self.reply(200, deployment.status())
                elif path == '/api/migration':
                    self.reply(200, migration.status() if migration else dict(phase='idle', plan=dict(available=False)))
                elif path == '/api/status':
                    self.reply(200, snapshot(target))
                elif path == '/api/config':
                    self.reply(200, snapshot(target, include_credentials=True))
                elif path == '/api/logs':
                    self.reply(200, service_operation(target, 'logs'))
                else:
                    self.reply(404, dict(error='页面不存在。'))
            except ManagementError as exc:
                self.reply(exc.status, dict(error=str(exc)))
            except (OSError, ValueError):
                self.reply(500, dict(error='读取管理页面失败，请确认项目文件完整。'))

        def do_POST(self):
            try:
                self.check_request(api=True)
                if self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding'):
                    raise ManagementError('请求必须使用 JSON。', 415)
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                except ValueError:
                    raise ManagementError('请求长度无效。') from None
                if not 0 < length <= 32768:
                    raise ManagementError('请求大小超出限制。', 413)
                try:
                    data = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError, socket.timeout):
                    raise ManagementError('请求内容无效或接收超时。') from None
                if not isinstance(data, dict):
                    raise ManagementError('请求必须为 JSON 对象。')
                if migration and (migration.phase == 'running' or migration.pending()) and self.path != '/api/migration/recover':
                    raise ManagementError('迁移正在进行或等待恢复，请完成后再修改配置或服务。', 409)
                if self.path == '/api/deploy/prepare' and deployment:
                    result = deployment.prepare(data.get('changes'), data.get('tunnel_token'), data.get('port', 18089),
                                                data.get('sakura_config_path'), data.get('sakura_proxy'))
                elif self.path == '/api/deploy/sakura/inspect' and deployment:
                    result = inspect_config(data.get('path'))
                    result.pop('credential', None)
                elif self.path == '/api/migration/apply' and migration:
                    result = migration.start(data.get('revision'))
                elif self.path == '/api/migration/recover' and migration:
                    result = migration.start(recover=True)
                elif self.path == '/api/deploy/apply' and deployment:
                    result = deployment.apply()
                elif self.path == '/api/config':
                    revision = data.get('revision')
                    if not isinstance(revision, str) or not revision:
                        raise ManagementError('缺少配置版本，请重新载入页面。')
                    result = save_config(target, data.get('changes'), revision=revision, create=revision == 'missing')
                elif self.path == '/api/unlock':
                    result = dict(state=state_operation(target, unlock=True), message='已解除认证封禁，密码和通行密钥保持不变。')
                elif self.path == '/api/service':
                    action = data.get('action')
                    if not isinstance(action, str) or action not in ('start', 'stop', 'restart'):
                        raise ManagementError('不支持的服务操作。')
                    result = service_operation(target, action)
                else:
                    raise ManagementError('接口不存在。', 404)
                self.reply(200, result)
            except ManagementError as exc:
                self.reply(exc.status, dict(error=str(exc)))
            except Exception:
                # Do not include configuration values or filesystem contents in error responses.
                self.reply(500, dict(error='操作失败，请检查文件权限和服务状态后重试。'))

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    server.management_url = f'http://127.0.0.1:{server.server_port}/#token={token}'
    server.migration = migration
    return server


def open_user_browser(url):
    uid = os.environ.get('SUDO_UID') or os.environ.get('PKEXEC_UID')
    if os.geteuid() == 0 and uid and uid.isdecimal() and int(uid) > 0:
        # A root installer must open the browser as the invoking desktop user.
        user = pwd.getpwuid(int(uid))
        child = os.fork()
        if child == 0:
            try:
                os.initgroups(user.pw_name, user.pw_gid)
                os.setgid(user.pw_gid)
                os.setuid(user.pw_uid)
                os.environ.update(HOME=user.pw_dir, USER=user.pw_name, LOGNAME=user.pw_name,
                                  XDG_RUNTIME_DIR=f'/run/user/{user.pw_uid}',
                                  DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{user.pw_uid}/bus')
                webbrowser.open(url)
            finally:
                os._exit(0)
        # Do not block the HTTP server on a browser launcher.
        threading.Thread(target=os.waitpid, args=(child, 0), daemon=True).start()
        return True
    if os.geteuid() == 0:
        return False
    return webbrowser.open(url)


def run_gui(target, port=18124, open_browser=True, deployment=None, view='open', fallback_port=False):
    try:
        server = create_server(target, port, deployment)
    except OSError:
        if not fallback_port:
            raise ManagementError(f'无法监听本机端口 {port}，请用 gui --port 指定其他端口。') from None
        server = create_server(target, 0, deployment)
    if view in ('manage', 'deploy', 'migrate', 'logs'):
        server.management_url = server.management_url.replace('/#token=', f'/?view={view}#token=')
    print('浏览器管理已启动（仅本机可访问，Ctrl+C 退出）。', flush=True)
    print('配置文件：' + str(target.config), flush=True)
    print('打开完整链接：' + server.management_url, flush=True)
    if open_browser:
        try:
            if not open_user_browser(server.management_url):
                print('未能自动打开浏览器，请复制上方完整链接。', flush=True)
        except webbrowser.Error:
            print('未能自动打开浏览器，请复制上方完整链接。', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n浏览器管理已关闭。')
    finally:
        server.server_close()
        if server.migration:
            server.migration.close()
