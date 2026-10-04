"""Extract, never install, a native package; exercise its bundled runtime with system Python."""
import argparse
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from management import Target, save_config


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='rdp-package-smoke-') as directory:
        root = Path(directory)
        if args.package.suffix == '.deb':
            subprocess.run(['dpkg-deb', '-x', str(args.package), str(root)], check=True)
        else:
            archive = subprocess.run(['rpm2cpio', str(args.package)], check=True, capture_output=True)
            subprocess.run(['cpio', '-idm', '--quiet', '--no-absolute-filenames'], input=archive.stdout, cwd=root, check=True)
        app = root / 'usr/lib/rdp-access-auth'
        desktop = (root / 'usr/share/applications/rdp-access-auth.desktop').read_text()
        assert 'Exec=rdp-auth launch %u\n' in desktop
        assert 'MimeType=x-scheme-handler/rdp-auth;\n' in desktop
        assert 'Name[zh_CN]=RDP Access Auth\n' in desktop
        assert (root / 'usr/share/icons/hicolor/512x512/apps/rdp-access-auth.png').is_file()
        assert (app / 'admin_ui/rdp-access-auth.png').is_file()
        for asset in ('icons.svg', 'management.css', 'theme.js'):
            assert (app / 'admin_ui' / asset).is_file()
        assert (root / 'usr/share/licenses/rdp-access-auth/Lucide-ISC').is_file()
        assert (root / 'usr/share/licenses/rdp-access-auth/LICENSE').is_file()
        assert (root / 'usr/share/doc/rdp-access-auth/AUTHORS').is_file()
        assert (root / 'usr/share/metainfo/cc.cd.zxabinbina.RDPAccessAuth.metainfo.xml').is_file()
        marker = json.loads((app / 'PACKAGED.json').read_text())
        assert marker['python'] == f'{sys.version_info.major}.{sys.version_info.minor}', marker
        assert not (root / 'etc').exists(), 'The package must not ship user configuration'
        env = dict(os.environ, XDG_DATA_HOME=str(root / 'data'), PYTHONPATH='', PYTHONNOUSERSITE='1')
        env.pop('RDP_AUTH_CONFIG', None)
        env.pop('RDP_AUTH_STATE', None)
        env.pop('RDP_AUTH_WORDLIST', None)
        command = ['/usr/bin/python3', str(app / 'rdp_manager.py')]
        for arguments in (['--help'], ['setup'], ['deploy', '--dry-run'], ['launch', '--help'], ['migrate', '--help']):
            result = subprocess.run([*command, *arguments], env=env, capture_output=True, text=True)
            assert result.returncode == 0, result.stderr
        words = root / 'words.json'
        words.write_text(json.dumps(['测试' + chr(0x4e00 + i) for i in range(2048)]))
        target = Target(root / 'config.json', root / 'state.sqlite3', 'rdp-test.service', root)
        save_config(target, dict(hostname='auth.example.test', rdp_address='desktop.example.test:3389',
            tunnel_id=1, password='Fixture-package-password-1234!', sakura_token='unused', wordlist_path=str(words)), create=True)
        port = free_port()
        log = (root / 'server.log').open('w+')
        process = subprocess.Popen([*command, '--config', str(target.config), '--state', str(target.state),
                                    'serve', '--port', str(port)], env=env, stdout=log, stderr=log, start_new_session=True)
        try:
            for attempt in range(80):
                try:
                    with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=1)) as client:
                        client.request('GET', '/healthz', headers={'Host': 'auth.example.test'})
                        response = client.getresponse()
                        assert response.status == 200
                        assert response.read().strip() == b'ok'
                    break
                except OSError:
                    if process.poll() is not None or attempt == 79:
                        log.seek(0)
                        raise AssertionError(log.read())
                    time.sleep(0.1)
            assert target.state.exists()
            with closing(http.client.HTTPConnection('127.0.0.1', port, timeout=3)) as client:
                client.request('GET', '/', headers={'Host': 'auth.example.test', 'X-Forwarded-Proto': 'https', 'CF-Connecting-IP': '1.1.1.1'})
                response = client.getresponse()
                assert response.status == 200
                assert b'csrf' in response.read()
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=10)
            log.close()
        # Test the actual bundled runtime's migration compatibility probe.
        result = subprocess.run(['/usr/bin/python3', '-c',
            'import package_bootstrap; import sys; from pathlib import Path; from migration import validate_runtime; '
            'validate_runtime(*(Path(p) for p in sys.argv[1:]))', str(target.config), str(target.state), str(words)],
            cwd=app, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    print('安装包解包验证通过：系统 Python 加载内置依赖、CLI、部署预览、Gunicorn 启动、数据库创建、健康检查和认证页面。未安装或修改系统服务。')


if __name__ == '__main__':
    main()
