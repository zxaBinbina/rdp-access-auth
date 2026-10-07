"""Private RDP admission portal; served only behind our Cloudflare Tunnel."""
from contextlib import closing
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlencode

from flask import Flask, abort, g, redirect, render_template_string, request, session, jsonify
from auth_guard import AuthGuard
from auth_credentials import TemporaryPasswords, Passkeys, password_hash
from admission import Admissions, configuration as admission_configuration, start_gateway

PAGE = Path(__file__).with_name('portal.html').read_text()

def create_app(settings, state_path, authorize_callback=None):
    sitekey = settings.get('turnstile_site_key', '')
    turnstile_secret = settings.get('turnstile_secret_key', '')
    if bool(sitekey) != bool(turnstile_secret) or any(
            not isinstance(v, str) or (v and (v != v.strip() or any(c.isspace() for c in v)))
            for v in (sitekey, turnstile_secret)):
        raise ValueError('Turnstile requires both a site key and a secret key')
    app = Flask(__name__)
    app.config.update(SECRET_KEY=settings['session_key'], MAX_CONTENT_LENGTH=65536,
                      SESSION_COOKIE_NAME='__Host-rdp-auth', SESSION_COOKIE_SECURE=True,
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
                      PERMANENT_SESSION_LIFETIME=900, TRUSTED_HOSTS=[settings['hostname']])
    Path(state_path).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(state_path)) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS auth_management (digest TEXT PRIMARY KEY, expires INTEGER NOT NULL)')
    guard = AuthGuard(state_path)
    temporary = TemporaryPasswords(state_path, settings.get('wordlist_path', str(Path(__file__).parent / 'wordlists/objects.json')), settings['session_key'])
    passkeys = Passkeys(state_path, settings['hostname'], settings['session_key'])
    app.extensions.update(auth_guard=guard, temporary_passwords=temporary, passkeys=passkeys)
    admission_config = admission_configuration(settings)
    admissions = Admissions(state_path, admission_config['duration_seconds']) if admission_config else None
    app.extensions.update(admissions=admissions, admission_config=admission_config)

    def client_ip():
        try:
            if not ipaddress.ip_address(request.remote_addr).is_loopback:
                abort(403)
            value = ipaddress.ip_address(request.headers.get('CF-Connecting-IP', ''))
            if not value.is_global:
                abort(403)
            return str(value)
        except ValueError:
            abort(403)

    def authorize(ip):
        if authorize_callback:
            return authorize_callback(ip)
        payload = json.dumps({'id': settings['tunnel_id'], 'ip': ip}).encode()
        req = urllib.request.Request('https://api.natfrp.com/v4/tunnel/auth', data=payload,
              headers={'Authorization': 'Bearer ' + settings['sakura_token'],
                       'Content-Type': 'application/json', 'User-Agent': 'private-rdp-auth/1.0'})
        # Explicitly avoid inheriting proxy settings which could change API handling.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for attempt in range(2):
            try:
                with opener.open(req, timeout=8) as response:
                    result = json.load(response)
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt or isinstance(exc, urllib.error.HTTPError) and exc.code < 500:
                    raise
                time.sleep(0.3)
        if not isinstance(result, str) or ipaddress.ip_address(result) != ipaddress.ip_address(ip):
            raise RuntimeError('Unexpected authorization response')

    @app.before_request
    def checks():
        g.nonce = secrets.token_urlsafe(18)
        if request.path == '/healthz':
            if request.headers.get('CF-Connecting-IP') or not ipaddress.ip_address(request.remote_addr).is_loopback:
                abort(404)
            return None
        g.ip = client_ip()
        # Only our loopback Cloudflare connector can supply these headers.
        scheme = request.headers.get('X-Forwarded-Proto', request.scheme)
        if scheme != 'https':
            if request.method in ('GET', 'HEAD'):
                return redirect('https://' + settings['hostname'] + '/', code=303)
            abort(403, description='请通过 HTTPS 重新打开认证页面。')
        if request.method == 'POST':
            origin = request.headers.get('Origin')
            # Privacy policies and some browsers omit Origin or send "null".
            # Signed session CSRF is still mandatory in login() in every case.
            if origin not in (None, 'null'):
                try:
                    parsed = urlsplit(origin)
                    valid = (parsed.scheme == 'https' and parsed.hostname == settings['hostname']
                             and parsed.port in (None, 443) and not parsed.username
                             and not parsed.password and not parsed.path
                             and not parsed.query and not parsed.fragment)
                except ValueError:
                    valid = False
                if not valid:
                    abort(403, description='提交来源不匹配，请重新打开认证页面。')
            if request.headers.get('Sec-Fetch-Site') == 'cross-site':
                abort(403, description='请直接打开认证页面后再提交。')

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store, max-age=0'
        response.headers['Content-Security-Policy'] = (
            "default-src 'none'; img-src data:; style-src 'nonce-" + g.get('nonce', '') + "'; "
            "script-src 'nonce-" + g.get('nonce', '') + "' https://challenges.cloudflare.com; connect-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
        return response

    def page(message='', success=False, status=200, **extra):
        method = request.values.get('method', 'password')
        if method not in ('password', 'temporary', 'passkey'):
            method = 'password'
        extra.setdefault('local_admission', bool(admissions))
        extra.setdefault('has_management', has_management())
        extra.setdefault('revoke_ip', g.get('authorized_ips', [''])[0] if g.get('authorized_ips') else '')
        extra.setdefault('can_revoke', may_revoke(extra['revoke_ip']))
        return render_template_string(PAGE, nonce=g.nonce, message=message, success=success,
                 method=method, turnstile_site_key=sitekey, **extra,
                 client_ip=g.get('ip', ''), authorized_ips=g.get('authorized_ips', []),
                 ipv6=':' in g.get('ip', ''), csrf=session.get('csrf', ''), rdp=settings['rdp_address']), status

    @app.errorhandler(403)
    def forbidden(exc):
        app.logger.warning('Request rejected: method=%s reason=%s', request.method,
                           exc.description if exc.description.startswith('请') or exc.description.startswith('提交')
                           else 'untrusted connector or client address')
        return error('请求未通过安全校验。请重新打开 https://' + settings['hostname'] + ' 后再试。', 403)

    @app.get('/healthz')
    def health():
        if admissions and not admissions.ready():
            return 'admission gateway unavailable\n', 503
        return 'ok\n'

    @app.get('/')
    def index():
        session.permanent = True
        if not session.get('csrf'):
            session['csrf'] = secrets.token_urlsafe(32)
        target = public_ipv4(g.ip)
        if admissions and target and request.args.get('reauth') != '1' and admissions.ready() and admissions.get(target):
            # frpc loses its upstream cache on restart. A still-valid LOCAL grant
            # may refresh that cache without extending the local grant lifetime.
            message = ''
            if guard.options_allowed(g.ip):
                try:
                    authorize(target)
                except Exception:
                    message = '本机授权仍有效，但隧道授权刷新失败，请稍后重试。'
            else:
                message = '隧道授权刷新过于频繁，请稍后重试。'
            g.authorized_ips = [target]
            return page(message, success=True)
        return page()

    def error(message, status=400, wait=0):
        response = app.make_response((jsonify(error=message), status) if request.is_json else page(message, status=status))
        if wait:
            response.headers['Retry-After'] = str(wait)
        return response

    def fields():
        value = request.get_json(silent=True) if request.is_json else request.form
        return value if isinstance(value, dict) or hasattr(value, 'getlist') else {}

    def csrf_check():
        value = fields().get('csrf', '')
        expected = session.get('csrf', '')
        if not isinstance(value, str) or not value or not hmac.compare_digest(value.encode(), expected.encode()):
            session.permanent = True
            session['csrf'] = secrets.token_urlsafe(32)
            return error('认证会话已更新，请重新打开页面后再提交，并允许 Cookie。', 403)

    @app.before_request
    def protect_forms():
        if request.method == 'POST':
            return csrf_check()

    def locked_response(wait, scope):
        minutes = max(1, (wait + 59) // 60)
        message = (f'短时间内多个 IP 认证失败，系统已临时锁定，请约 {minutes} 分钟后再试。' if scope == 'global'
                   else f'此 IP 连续认证失败，已临时封禁，请约 {minutes} 分钟后再试。' if scope == 'ip'
                   else '正在处理其他认证请求，请稍后再试。')
        return error(message, 429, wait)

    def begin_attempt():
        token, wait, scope = guard.begin(g.ip)
        return token, locked_response(wait, scope) if wait else None

    def bad_credentials(token, message='认证凭据不正确。'):
        guard.finish(token, False)
        wait, scope = guard.locked(g.ip)
        return locked_response(wait, scope) if wait else error(message, 401)

    def check_turnstile(action):
        if not sitekey:
            return None
        token = fields().get('cf-turnstile-response', '')
        if not isinstance(token, str) or not token.strip() or len(token) > 2048:
            return error('请先完成人机验证，再提交认证。', 403)
        payload = urlencode({'secret': turnstile_secret, 'response': token, 'remoteip': g.ip}).encode()
        req = urllib.request.Request('https://challenges.cloudflare.com/turnstile/v0/siteverify',
                data=payload, headers={'Content-Type': 'application/x-www-form-urlencoded'})
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as response:
                if response.status != 200:
                    raise ValueError('Unexpected Siteverify status')
                result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError('Unexpected Siteverify response')
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return error('人机验证服务暂时不可用，请稍后重试。', 503)
        if (result.get('success') is not True or result.get('action') != action
                or result.get('hostname') != settings['hostname']):
            return error('人机验证未通过或已过期，请重新验证后重试。', 403)
        return None

    def ipv4_target():
        # Only the trusted loopback connector determines the admission target.
        # Old clients may still submit ipv4, but cannot select another address.
        target = public_ipv4(g.ip)
        raw = fields().get('ipv4')
        if raw is not None and (not isinstance(raw, str) or public_ipv4(raw.strip()) != target):
            return None
        return target

    def public_ipv4(raw):
        try:
            value = ipaddress.ip_address(raw)
            return str(value) if value.version == 4 and value.is_global else None
        except (ValueError, TypeError):
            return None

    def may_revoke(ip):
        return bool(ip and ip == g.get('ip'))

    @app.post('/admission/status')
    def admission_status():
        if not admissions:
            return jsonify(enabled=False, authorized=False)
        target = ipv4_target()
        if not target:
            return error('仅支持当前连接的公网 IPv4；请通过 IPv4 网络打开认证页面，不能指定其他 IP。', 400)
        if not guard.options_allowed(g.ip):
            return error('查询过于频繁，请稍后重试。', 429, 300)
        if not admissions.ready():
            return error('本机准入网关未就绪，请稍后重试。', 503)
        grant = admissions.get(target)
        return jsonify(enabled=True, authorized=bool(grant), ipv4=target)

    @app.post('/admission/revoke')
    def revoke_admission():
        if not admissions:
            return error('本机准入控制尚未启用。', 404)
        target = ipv4_target()
        if not target or not may_revoke(target):
            return error('只能取消当前连接 IP 的认证，不能指定其他 IP。', 403)
        admissions.revoke(target)
        # Also invalidate this browser's credential-management capability.
        if session.get('authorized_ip') == target:
            token = session.get('management', '')
            with closing(sqlite3.connect(state_path)) as db, db:
                db.execute('DELETE FROM auth_management WHERE digest=?', (hashlib.sha256(token.encode()).hexdigest(),))
            session.clear()
            session['csrf'] = secrets.token_urlsafe(32)
        if request.is_json:
            return jsonify(ok=True, redirect='/')
        return redirect('/', code=303)

    def session_hash():
        return hashlib.sha256(session['csrf'].encode()).hexdigest()

    def start_management(ip, method):
        session.clear()
        session.permanent = True
        session['csrf'] = secrets.token_urlsafe(32)
        session['management'] = secrets.token_urlsafe(32)
        session['authorized_ip'] = ip
        session['authorized_at'] = int(time.time())
        session['auth_method'] = method
        digest = hashlib.sha256(session['management'].encode()).hexdigest()
        with closing(sqlite3.connect(state_path)) as db, db:
            db.execute('DELETE FROM auth_management WHERE expires <= ?', (int(time.time()),))
            db.execute('INSERT INTO auth_management VALUES (?,?)', (digest, int(time.time()) + 600))

    def has_management():
        token = session.get('management')
        if not token:
            return False
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(sqlite3.connect(state_path)) as db:
            return db.execute('SELECT 1 FROM auth_management WHERE digest=? AND expires>?', (digest, int(time.time()))).fetchone() is not None

    def can_bind():
        return has_management() and session.get('auth_method') == 'password'

    def admit(target, attempt, temporary_token=None, method='passkey'):
        try:
            wait, scope = guard.locked(g.ip)
            if wait:
                return locked_response(wait, scope)
            if admissions and not admissions.ready():
                return error('本机准入网关未就绪，尚未授权，请稍后重试。', 503)
            try:
                authorize(target)
                if admissions:
                    admissions.grant(target)
            except Exception as exc:
                app.logger.warning('SakuraFrp authorization failed: type=%s', type(exc).__name__)
                return error('授权服务暂时不可用，请稍后重试。临时密码未更换。' if temporary_token else '授权服务暂时不可用，请稍后重试。', 502)
            next_phrase = temporary.rotate(temporary_token) if temporary_token else None
            guard.finish(attempt, True)
            start_management(target, method)
            g.authorized_ips = [target]
            if request.is_json:
                return jsonify(ok=True, redirect='/authorized')
            return page(success=True, next_temporary=next_phrase)
        finally:
            guard.finish(attempt, None)
            if temporary_token:
                temporary.release(temporary_token)

    @app.post('/authorize')
    def login():
        data = fields()
        method = data.get('method', 'password')
        if method not in ('password', 'temporary'):
            return error('请选择固定密码、临时密码或通行密钥。', 400)
        attempt, blocked = begin_attempt()
        if blocked is not None:
            return blocked
        reservation = None
        try:
            rejected = check_turnstile('login_' + method)
            if rejected is not None:
                return rejected
            if method == 'password':
                password = data.get('password', '')
                valid = isinstance(password, str) and 1 <= len(password) <= 128 and hmac.compare_digest(
                    password_hash(password, settings['password_salt']), settings['password_hash'])
                if not valid:
                    return bad_credentials(attempt)
            else:
                try:
                    if any(f'temporary_{i}' in data for i in range(1, 4)):
                        parts = [data.get(f'temporary_{i}') for i in range(1, 4)]
                        value = '-'.join(part.strip() for part in parts) if all(
                            isinstance(part, str) and part.strip() and len(part) <= 80 for part in parts
                        ) else ''
                    else:
                        value = data.get('temporary', '')
                    reservation = temporary.reserve(value)
                except RuntimeError:
                    return error('这条临时密码正在被使用，请等待该次认证完成。', 409)
                if not reservation:
                    return bad_credentials(attempt, '临时密码不正确或已作废。')
            target = ipv4_target()
            if not target:
                return error('仅支持当前连接的公网 IPv4；请通过 IPv4 网络打开认证页面，不能指定其他 IP。', 400)
            return admit(target, attempt, reservation, method)
        finally:
            guard.finish(attempt, None)
            if reservation:
                temporary.release(reservation)

    @app.get('/authorized')
    def authorized():
        if not has_management() or session.get('authorized_ip') != g.ip:
            return redirect('/')
        if admissions and (not admissions.ready() or not admissions.get(session['authorized_ip'])):
            return redirect('/')
        g.authorized_ips = [session['authorized_ip']]
        return page(success=True, next_temporary=temporary.current() if session.get('auth_method') == 'temporary' else None)

    @app.post('/temporary/regenerate')
    def regenerate_temporary():
        if not has_management() or session.get('auth_method') != 'temporary':
            return error('请先使用临时密码认证，再重新生成下一条密码。', 403)
        try:
            phrase = temporary.regenerate(fields().get('current_temporary'))
        except RuntimeError:
            return error('临时密码已更新或正在认证中，请刷新页面后重试。', 409)
        if request.is_json:
            return jsonify(ok=True, temporary=phrase)
        return redirect('/authorized', code=303)

    @app.get('/credentials')
    def credentials():
        if not has_management():
            return page('请先通过任一种方式认证，再在成功页管理通行密钥或查看临时密码。', status=403)
        return page(manage=True, can_bind=can_bind(), keys=passkeys.list(), current_temporary=temporary.current())

    @app.post('/passkeys/register/options')
    def registration_options():
        if not can_bind():
            return error('请先使用固定密码认证，再绑定通行密钥。', 403)
        try:
            return jsonify(passkeys.options('register', session_hash()))
        except ValueError:
            return error('最多可绑定 20 个通行密钥，请先移除不用的密钥。', 400)

    @app.post('/passkeys/register/verify')
    def registration_verify():
        if not can_bind():
            return error('请先使用固定密码认证，再绑定通行密钥。', 403)
        data = fields()
        try:
            passkeys.register(data.get('challenge_id', ''), session_hash(), data.get('credential'), data.get('name', ''))
        except Exception as exc:
            app.logger.warning('Passkey registration rejected: type=%s', type(exc).__name__)
            return error('通行密钥未能绑定。请重新发起绑定，并在设备上完成身份确认。', 400)
        return jsonify(ok=True, redirect='/credentials')

    @app.post('/passkeys/delete')
    def delete_passkey():
        if not has_management():
            return error('管理会话已过期，请重新认证。', 403)
        passkeys.delete(fields().get('key_id', ''))
        return redirect('/credentials', code=303)

    @app.post('/passkeys/auth/options')
    def authentication_options():
        wait, scope = guard.locked(g.ip)
        if wait:
            return locked_response(wait, scope)
        if not guard.options_allowed(g.ip):
            return error('通行密钥请求过于频繁，请五分钟后再试，或使用密码登录。', 429, 300)
        # One pending challenge per signed browser session, valid for two minutes.
        return jsonify(passkeys.options('authenticate', session_hash()))

    @app.post('/passkeys/auth/verify')
    def authentication_verify():
        attempt, blocked = begin_attempt()
        if blocked is not None:
            return blocked
        try:
            rejected = check_turnstile('login_passkey')
            if rejected is not None:
                return rejected
            target = ipv4_target()
            if not target:
                return error('仅支持当前连接的公网 IPv4；请通过 IPv4 网络打开认证页面，不能指定其他 IP。', 400)
            data = fields()
            try:
                passkeys.authenticate(data.get('challenge_id', ''), session_hash(), data.get('credential'))
            except Exception as exc:
                app.logger.warning('Passkey authentication rejected: type=%s', type(exc).__name__)
                return bad_credentials(attempt, '通行密钥未通过验证，请重试或选择密码登录。')
            return admit(target, attempt)
        finally:
            guard.finish(attempt, None)


    return app

if os.environ.get('RDP_AUTH_CONFIG'):
    runtime_settings = json.loads(Path(os.environ['RDP_AUTH_CONFIG']).read_text())
    if os.environ.get('RDP_AUTH_WORDLIST'):
        runtime_settings['wordlist_path'] = os.environ['RDP_AUTH_WORDLIST']
    app = create_app(runtime_settings,
                     os.environ.get('RDP_AUTH_STATE', '/var/lib/rdp-auth/state.sqlite3'))
    if app.extensions['admissions']:
        app.extensions['admission_thread'] = start_gateway(app.extensions['admissions'], app.extensions['admission_config'])
