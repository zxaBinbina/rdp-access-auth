"""Unprivileged Qt WebEngine window, native tray and authenticated local bridge."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

from management import ManagementError, atomic_private_write
from desktop.control import Client, SOCKET_PATH


def run(view='open', background=False, socket_path=SOCKET_PATH):
    if os.geteuid() == 0:
        raise ManagementError('请以普通桌面用户启动软件；需要管理员权限时会由系统单独询问。')
    try:
        from PySide6.QtCore import QObject, Signal, Slot, QUrl, QBuffer, QIODevice, QTimer, QSettings, Qt
        from PySide6.QtGui import QIcon, QAction, QDesktopServices
        from PySide6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWebChannel import QWebChannel
        from PySide6.QtWebEngineCore import QWebEngineUrlScheme, QWebEngineUrlSchemeHandler, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        try:
            from PyQt6.QtCore import QObject, pyqtSignal as Signal, pyqtSlot as Slot, QUrl, QBuffer, QIODevice, QTimer, QSettings, Qt
            from PyQt6.QtGui import QIcon, QAction, QDesktopServices
            from PyQt6.QtWidgets import QApplication, QMainWindow, QSystemTrayIcon, QMenu
            from PyQt6.QtNetwork import QLocalServer, QLocalSocket
            from PyQt6.QtWebChannel import QWebChannel
            from PyQt6.QtWebEngineCore import QWebEngineUrlScheme, QWebEngineUrlSchemeHandler, QWebEnginePage, QWebEngineProfile, QWebEngineSettings
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            raise ManagementError('缺少 Qt WebEngine 桌面组件，请通过发行版软件管理器重新安装本软件包及其依赖。') from None

    assets = Path(__file__).resolve().parents[1] / 'desktop_ui'
    scheme = QWebEngineUrlScheme(b'rdpapp')
    scheme.setSyntax(QWebEngineUrlScheme.Syntax.Host)
    scheme.setFlags(QWebEngineUrlScheme.Flag.SecureScheme | QWebEngineUrlScheme.Flag.LocalScheme | QWebEngineUrlScheme.Flag.LocalAccessAllowed)
    QWebEngineUrlScheme.registerScheme(scheme)
    app = QApplication(['rdp-access-auth'])
    app.setApplicationName('RDP Access Auth')
    app.setOrganizationName('rdp-access-auth')
    app.setDesktopFileName('rdp-access-auth')
    app.setQuitOnLastWindowClosed(False)
    settings = QSettings()
    icon = QIcon(str(assets / 'logo.png'))
    app.setWindowIcon(icon)
    client = Client(socket_path)
    try:
        initial = client.request('status')
    except ManagementError:
        initial = {'initialized': False, 'configured': False, 'preferences': {'show_tray': True, 'theme': 'system', 'palette': 'fufu-blue'}}
    if background and (not initial.get('initialized') or initial.get('preferences', {}).get('startup') == 'manual'):
        return 0

    # One desktop instance per login user. Subsequent launcher invocations focus it.
    instance_name = 'rdp-access-auth-desktop-' + str(os.getuid())
    if socket_path != SOCKET_PATH:
        import hashlib
        instance_name += '-' + hashlib.sha256(str(socket_path).encode()).hexdigest()[:8]
    probe = QLocalSocket()
    probe.connectToServer(instance_name)
    if probe.waitForConnected(300):
        probe.write(json.dumps({'view': view, 'show': not background}).encode())
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0
    QLocalServer.removeServer(instance_name)
    instance = QLocalServer()
    instance.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not instance.listen(instance_name):
        raise ManagementError('无法创建本机窗口连接，请检查桌面会话。')

    class Assets(QWebEngineUrlSchemeHandler):
        def requestStarted(self, job):
            url = job.requestUrl()
            name = url.path().lstrip('/') or 'index.html'
            allowed = {'index.html': 'text/html', 'app.js': 'text/javascript', 'theme.js': 'text/javascript',
                       'app.css': 'text/css', 'theme.css': 'text/css', 'icons.svg': 'image/svg+xml', 'logo.png': 'image/png'}
            if url.host() != 'ui' or name not in allowed or job.requestMethod() != b'GET':
                job.fail(type(job).Error.UrlNotFound)
                return
            try:
                buffer = QBuffer(job)
                buffer.setData((assets / name).read_bytes())
                buffer.open(QIODevice.OpenModeFlag.ReadOnly)
                job.reply(allowed[name].encode(), buffer)
            except OSError:
                job.fail(type(job).Error.UrlNotFound)

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, nav_type, main_frame):
            if url.scheme() == 'rdpapp' and url.host() == 'ui':
                return True
            if url.scheme() == 'https' and nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
                QDesktopServices.openUrl(url)
            return False

    class Bridge(QObject):
        response = Signal(str)
        event = Signal(str)
        preferences_changed = Signal(object)
        quit_requested = Signal()
        ready = Signal()
        copy_requested = Signal(str)
        open_requested = Signal(str)
        def __init__(self):
            super().__init__()
            self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='rdp-window')
            self.session = None
            self.polling = False
            self.closed = False
            self.session_lock = threading.RLock()
            self.generation = 0

        @Slot(str)
        def request(self, raw):
            try:
                request = json.loads(raw)
                if not isinstance(request, dict) or len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
            except ValueError:
                return
            self.pool.submit(self.handle, request)

        def handle(self, request):
            request_id, method, params = request.get('id'), request.get('method'), request.get('params') or {}
            try:
                if method == 'ui.ready':
                    self.ready.emit()
                    value = initial
                elif method == 'ui.bootstrap':
                    payload = json.dumps({'password': params.get('password')})
                    process = subprocess.run(['pkexec', '/usr/bin/rdp-auth', 'desktop-bootstrap'], input=payload,
                                             capture_output=True, text=True, timeout=180)
                    if process.returncode:
                        raise ManagementError('系统初始化未完成。请确认管理员认证，并检查软件包是否安装完整。', 403)
                    value = client.request('status')
                elif method == 'security.login':
                    generation = self.generation
                    result = client.request(method, params)
                    with self.session_lock:
                        if generation != self.generation:
                            client.request('security.logout', session=result['session'])
                            raise ManagementError('窗口已关闭，请重新解锁。', 401)
                        self.session = result['session']
                    value = {'unlocked': True}
                elif method == 'security.logout':
                    self.lock_window()
                    value = {'locked': True}
                elif method == 'ui.open_link':
                    url = QUrl(params.get('url', ''))
                    if url.scheme() != 'https' or not url.host() or url.userInfo():
                        raise ManagementError('仅允许打开正常的 HTTPS 链接。')
                    self.open_requested.emit(url.toString())
                    value = {'opened': True}
                elif method == 'ui.copy':
                    value = params.get('text')
                    if not isinstance(value, str) or len(value) > 65536:
                        raise ManagementError('复制内容无效。')
                    self.copy_requested.emit(value)
                    value = {'copied': True}
                elif method == 'ui.export_agent':
                    self.require_session()
                    identity = params.get('identity', {})
                    if not re.fullmatch('[a-f0-9]{24}', identity.get('id', '')) or not re.fullmatch('[A-Za-z0-9_-]{40,80}', identity.get('key', '')):
                        raise ManagementError('身份数据无效，请重新登记 Agent。')
                    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'rdp-access-auth/agents'
                    config.mkdir(parents=True, exist_ok=True, mode=0o700)
                    config.chmod(0o700)
                    path = config / (identity['id'] + '.json')
                    atomic_private_write(path, json.dumps(identity).encode())
                    value = {'identity_path': str(path), 'configuration': {'mcpServers': {'rdp-access-auth': {'command': '/usr/bin/rdp-auth', 'args': ['mcp', '--identity', str(path)]}}}}
                elif method == 'ui.quit':
                    self.lock_window()
                    self.quit_requested.emit()
                    value = {'exiting': True}
                else:
                    if not isinstance(method, str) or method.startswith('ui.'):
                        raise ManagementError('不支持此界面操作。')
                    value = client.request(method, params, session=self.session, confirmation=request.get('confirmation'))
                    if method == 'preferences.save':
                        self.preferences_changed.emit(value)
                    if method == 'security.password':
                        self.session = None
                self.response.emit(json.dumps({'id': request_id, 'result': value}, ensure_ascii=False))
            except ManagementError as exc:
                self.response.emit(json.dumps({'id': request_id, 'error': {'message': str(exc), 'code': exc.status}}, ensure_ascii=False))
            except Exception:
                self.response.emit(json.dumps({'id': request_id, 'error': {'message': '操作未完成，请检查输入后重试。', 'code': 500}}, ensure_ascii=False))

        def require_session(self):
            if self.session is None:
                raise ManagementError('请先解锁设置。', 401)

        def lock_window(self):
            with self.session_lock:
                self.generation += 1
                token, self.session = self.session, None
            if token:
                def revoke():
                    try:
                        client.request('security.logout', session=token)
                    except ManagementError:
                        pass
                self.pool.submit(revoke)
            self.event.emit(json.dumps({'type': 'locked'}))

        def poll(self):
            if self.polling or self.closed:
                return
            self.polling = True
            def work():
                try:
                    state = client.request('status')
                    self.preferences_changed.emit(state['preferences'])
                    self.event.emit(json.dumps({'type': 'status', 'data': state}, ensure_ascii=False))
                except ManagementError:
                    pass
                finally:
                    self.polling = False
            self.pool.submit(work)

    bridge = Bridge()
    bridge.copy_requested.connect(lambda value: app.clipboard().setText(value))
    bridge.open_requested.connect(lambda value: QDesktopServices.openUrl(QUrl(value)))

    class Window(QMainWindow):
        def closeEvent(self, event):
            settings.setValue('geometry', self.saveGeometry())
            self.hide()
            bridge.lock_window()
            event.ignore()

    window = Window()
    window.setWindowTitle('RDP Access Auth')
    window.setWindowIcon(icon)
    window.resize(1040, 760)
    window.setMinimumSize(760, 560)
    geometry = settings.value('geometry')
    if geometry:
        window.restoreGeometry(geometry)
    webview = QWebEngineView(window)
    profile = QWebEngineProfile(window)  # Off-the-record; no shared browser cookies or storage.
    handler = Assets(profile)
    profile.installUrlSchemeHandler(b'rdpapp', handler)
    page = Page(profile, webview)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    channel = QWebChannel(page)
    channel.registerObject('bridge', bridge)
    page.setWebChannel(channel)
    webview.setPage(page)
    window.setCentralWidget(webview)
    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip('RDP Access Auth')
    menu = QMenu()
    actions = {}
    for key, label in [('settings', '打开设置'), ('pause', '暂停服务'), ('restart', '重启服务'), ('exit', '退出')]:
        if key == 'exit':
            menu.addSeparator()
        action = QAction(label, menu)
        menu.addAction(action)
        actions[key] = action
    tray.setContextMenu(menu)

    def show_window(action=None):
        window.showNormal()
        window.raise_()
        window.activateWindow()
        if action:
            bridge.event.emit(json.dumps({'type': 'navigate' if action == 'settings' else 'action', 'action': action}))

    for key, action in actions.items():
        action.triggered.connect(lambda checked=False, name=key: show_window(name))
    tray.activated.connect(lambda reason: show_window() if reason in (QSystemTrayIcon.ActivationReason.Trigger, QSystemTrayIcon.ActivationReason.DoubleClick) else None)
    seen_pending = 0

    def status_event(raw):
        nonlocal seen_pending
        event = json.loads(raw)
        if event.get('type') == 'status':
            value = event['data']
            count = value.get('pending_count', 0)
            if count > seen_pending:
                show_window()
                bridge.event.emit(json.dumps({'type': 'navigate', 'action': 'agents'}))
            seen_pending = count
            tray.setToolTip('RDP Access Auth · ' + ('已暂停' if value['preferences'].get('paused') else '运行管理'))

    bridge.event.connect(status_event)
    def apply_preferences(prefs):
        tray.setVisible(bool(prefs.get('show_tray', True)))
        actions['pause'].setText('继续服务' if prefs.get('paused') else '暂停服务')
        settings.setValue('preferences', json.dumps(prefs))
    bridge.preferences_changed.connect(apply_preferences)
    apply_preferences(initial['preferences'])

    def system_theme():
        return 'light' if app.palette().window().color().lightness() > 128 else 'dark'
    prefs = initial.get('preferences', {})
    from urllib.parse import urlencode
    query = urlencode({'view': view, 'theme': prefs.get('theme', 'system'), 'palette': prefs.get('palette', 'fufu-blue'), 'system': system_theme()})
    webview.setUrl(QUrl('rdpapp://ui/index.html?' + query))
    app.paletteChanged.connect(lambda *_: bridge.event.emit(json.dumps({'type': 'system-theme', 'theme': system_theme()})))
    timer = QTimer(app)
    timer.setInterval(2500)
    timer.timeout.connect(bridge.poll)
    timer.start()
    bridge.ready.connect(bridge.poll)

    def receive_instance():
        connection = instance.nextPendingConnection()
        def ready():
            try:
                incoming = json.loads(bytes(connection.readAll()))
                if incoming.get('show'):
                    show_window()
                    bridge.event.emit(json.dumps({'type': 'navigate', 'action': incoming.get('view', 'overview')}))
            except ValueError:
                pass
            connection.disconnectFromServer()
            connection.deleteLater()
        connection.readyRead.connect(ready)
        if connection.bytesAvailable():
            ready()
    instance.newConnection.connect(receive_instance)

    def quit_app():
        bridge.closed = True
        timer.stop()
        tray.hide()
        instance.close()
        app.quit()
    bridge.quit_requested.connect(quit_app)
    if background:
        bridge.pool.submit(lambda: client.request('lifecycle.login') if initial.get('configured') else None)
    else:
        window.show()
    code = app.exec()
    bridge.closed = True
    bridge.lock_window()
    bridge.pool.shutdown(wait=False, cancel_futures=True)
    return code
