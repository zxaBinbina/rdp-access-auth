"""Check protocol registration in an isolated desktop MIME database."""
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix='rdp-app-links-') as directory:
        root = Path(directory)
        applications = root / 'share/applications'
        applications.mkdir(parents=True)
        (root / 'config').mkdir()
        shutil.copyfile(ROOT / 'packaging/rdp-access-auth.desktop', applications / 'rdp-access-auth.desktop')
        env = dict(os.environ, XDG_DATA_HOME=str(root/'share'), XDG_DATA_DIRS=str(root/'empty'),
                   XDG_CONFIG_HOME=str(root/'config'), XDG_CONFIG_DIRS=str(root/'empty'),
                   LC_ALL='C')
        subprocess.run(['update-desktop-database', str(applications)], env=env, check=True)
        result = subprocess.run(['gio', 'mime', 'x-scheme-handler/rdp-auth'], env=env,
                                check=True, capture_output=True, text=True)
        assert 'Default application for' in result.stdout and 'rdp-access-auth.desktop' in result.stdout, result.stdout
        print('协议注册检查通过：桌面 MIME 数据库将 rdp-auth:// 识别为 RDP Access Auth。未修改用户默认应用。')


if __name__ == '__main__':
    main()
