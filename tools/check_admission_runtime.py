"""Isolated real gunicorn + gateway smoke test; never uses installed services."""
from contextlib import closing
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
import re
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from admission import Admissions
from auth_credentials import password_hash
from test_admission import header


def free_port():
    with closing(socket.socket()) as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def main():
    class Echo(socketserver.BaseRequestHandler):
        def handle(self):
            while data := self.request.recv(65536):
                self.request.sendall(data)

    with tempfile.TemporaryDirectory(prefix='rdp-admission-runtime-') as directory:
        root = Path(directory)
        backend = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Echo)
        backend.daemon_threads = True
        thread = threading.Thread(target=backend.serve_forever, daemon=True)
        thread.start()
        port, gateway_port = free_port(), free_port()
        while port == gateway_port:
            gateway_port = free_port()
        words = root / 'words.json'
        words.write_text(json.dumps(['测试'+chr(0x4e00+i) for i in range(2048)]))
        settings = dict(hostname='auth.example.test', rdp_address='desktop.example.test:3389',
            session_key='test-only-'*8, password_salt='aa'*16,
            password_hash=password_hash('Test-password-1234', 'aa'*16), sakura_token='unused', tunnel_id=1,
            wordlist_path=str(words), local_admission=dict(listen_port=gateway_port, target_port=backend.server_address[1]))
        config, state = root / 'settings.json', root / 'state.sqlite3'
        config.write_text(json.dumps(settings))
        environment = dict(os.environ)
        for key in ('RDP_AUTH_CONFIG', 'RDP_AUTH_STATE', 'RDP_AUTH_WORDLIST'):
            environment.pop(key, None)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        cookie = ''

        def request(path, data=None, public=True):
            headers = {'Host':'auth.example.test', 'X-Forwarded-Proto':'https'}
            if public:
                headers.update({'CF-Connecting-IP':'1.1.1.1', 'Origin':'https://auth.example.test', 'Cookie':cookie})
            if data is not None:
                headers['Content-Type'] = 'application/json'
            return opener.open(urllib.request.Request(f'http://127.0.0.1:{port}'+path,
                data=json.dumps(data).encode() if data is not None else None, headers=headers), timeout=3)

        with (root / 'runtime.log').open('w+') as log:
            process = subprocess.Popen([sys.executable, str(ROOT/'rdp_manager.py'), '--config', str(config),
                '--state', str(state), 'serve', '--port', str(port)], cwd=ROOT, env=environment,
                start_new_session=True, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise AssertionError('Runtime exited before health check')
                    try:
                        with request('/healthz', public=False) as response:
                            assert response.read() == b'ok\n'
                        break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise AssertionError('Runtime health check timed out')
                with socket.create_connection(('127.0.0.1', gateway_port), timeout=3) as stream:
                    stream.sendall(header('1.1.1.1', 2)+b'not-admitted')
                    assert stream.recv(1024) == b''
                store = Admissions(str(state))
                store.grant('1.1.1.1')
                with socket.create_connection(('127.0.0.1', gateway_port), timeout=3) as stream:
                    stream.sendall(header('1.1.1.1', 2)+b'RDP-test')
                    assert stream.recv(1024) == b'RDP-test'
                    with request('/?reauth=1') as response:
                        cookies = SimpleCookie(response.headers['Set-Cookie'])
                        cookie = '; '.join(f'{key}={value.value}' for key, value in cookies.items())
                        csrf = re.search(r'name="csrf" value="([^"]+)"', response.read().decode()).group(1)
                    data = dict(csrf=csrf, ipv4='1.1.1.1')
                    with request('/admission/status', data) as response:
                        assert json.load(response)['authorized'] is True
                    with request('/admission/revoke', data) as response:
                        assert json.load(response)['ok'] is True
                    assert stream.recv(1024) == b''
                assert store.get('1.1.1.1') is None
                print('Runtime passed: CLI → gunicorn → PROXY v2 gateway → TCP echo; web revocation disconnects stream.')
            except Exception:
                log.flush()
                log.seek(0)
                print(log.read(), file=sys.stderr)
                raise
            finally:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
                backend.shutdown()
                backend.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    main()
