"""Select the private KRDP repair only for the audited distribution binary."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
UNIT = 'app-org.kde.krdpserver.service'


def detect(root=Path('/')):
    root = Path(root)
    library = root / 'usr/lib64/libKRdp.so.6.7.5'
    installed = any((root / path).exists() for path in ('usr/bin/krdpserver', 'usr/libexec/krdpserver'))
    metadata = json.loads((ROOT / 'third_party/krdp/UPSTREAM.json').read_text())
    compatible = library.is_file() and hashlib.sha256(library.read_bytes()).hexdigest() == metadata['system_library_sha256']
    if compatible and root == Path('/'):
        result = subprocess.run(['rpm', '-q', '--qf', '%{VERSION}-%{RELEASE}.%{ARCH}', 'krdp'], capture_output=True, text=True)
        compatible = result.returncode == 0 and result.stdout == metadata['supported_rpm']
    available = (ROOT / 'krdp-fix/libKRdp.so.6.7.5').is_file()
    return {'installed': installed, 'compatible': bool(compatible), 'available': available,
            'message': '可应用已验证的 KDE 修复。' if installed and compatible and available else
                       '未安装 KDE 远程桌面。' if not installed else '保留现有 KDE 远程桌面；此版本暂无匹配的修复组件。'}


def install(root, runner, owner_uid):
    result = detect(root)
    if not all(result[key] for key in ('installed', 'compatible', 'available')):
        return result
    directory = Path(root) / 'etc/systemd/user' / (UNIT + '.d')
    directory.mkdir(parents=True, exist_ok=True)
    (directory / '90-rdp-access-auth-repair.conf').write_text(
        '[Service]\nExecStart=\nExecStart=/usr/bin/python3 /usr/lib/rdp-access-auth/desktop/krdp.py\n')
    result['message'] = '兼容修复已安装，将在 KDE 远程桌面下次启动时生效；已有用户级修复优先。'
    # Do not restart somebody's live desktop session as part of installing a library.
    return result


def launch():
    import os
    environment = dict(os.environ)
    environment.pop('LD_LIBRARY_PATH', None)
    result = detect()
    if result['compatible'] and result['available']:
        info = json.loads((ROOT / 'krdp-fix/build-info.json').read_text())
        library = ROOT / 'krdp-fix/libKRdp.so.6.7.5'
        if hashlib.sha256(library.read_bytes()).hexdigest() == info['library_sha256']:
            environment['LD_LIBRARY_PATH'] = str(library.parent)
    binary = '/usr/bin/krdpserver' if Path('/usr/bin/krdpserver').exists() else '/usr/libexec/krdpserver'
    os.execve(binary, [binary], environment)


if __name__ == '__main__':
    launch()
