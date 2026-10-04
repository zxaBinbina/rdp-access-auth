"""Run disposable user services to verify real systemd credential parsing.

Never reads or switches a deployed authentication service. Requires a running
user systemd manager; all files and linked fixture units are cleaned afterward.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import uuid
import shutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from management import Target
from migration import Migration


def main():
    env = dict(os.environ, XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
               DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus')
    def run(*args):
        return subprocess.run(['systemctl', '--user', *args], env=env, capture_output=True, text=True, timeout=20)
    with tempfile.TemporaryDirectory(prefix='rdp-systemd-credentials-') as directory:
        folder = Path(directory) / '中文 有空格 100%'
        folder.mkdir()
        config, words = folder / 'settings.json', folder / 'words.json'
        config.write_text('fixture-settings')
        words.write_text('fixture-words')
        # Match migration: arbitrary source paths are copied to private,
        # standard paths before being passed to systemd.
        staged_config, staged_words = Path(directory)/'settings.json', Path(directory)/'objects.json'
        shutil.copyfile(config, staged_config)
        shutil.copyfile(words, staged_words)
        checker = Path(directory) / 'check.py'
        checker.write_text('import os\nfrom pathlib import Path\np=Path(os.environ["CREDENTIALS_DIRECTORY"])\n'
                           'assert (p/"package-settings.json").read_text()=="fixture-settings"\n'
                           'assert (p/"package-words.json").read_text()=="fixture-words"\n')
        target = Target(staged_config, Path(directory)/'state', 'rdp-auth.service', Path(directory), 'legacy')
        migration = Migration(target, check_host=False)
        lines = [line for line in migration.unit_override(dict(port=18089, wordlist=str(staged_words))).splitlines()
                 if line.startswith('LoadCredential=')]
        for broken in (True, False):
            name = 'rdp-credentials-test-' + uuid.uuid4().hex[:10] + '.service'
            unit = Path(directory) / name
            if broken:
                # Reproduce the reported failure on ordinary paths, then check
                # special characters separately against the fixed generator.
                plain_config, plain_words = Path(directory)/'settings.json', Path(directory)/'words.json'
                plain_config.write_text('fixture-settings')
                plain_words.write_text('fixture-words')
                directives = [f'LoadCredential="package-settings.json:{plain_config}"',
                              f'LoadCredential="package-words.json:{plain_words}"']
            else:
                directives = lines
            unit.write_text('[Service]\nType=oneshot\n' + '\n'.join(directives) +
                            f'\nExecStart=/usr/bin/python3 {checker}\n')
            try:
                linked = run('link', str(unit))
                if linked.returncode:
                    raise RuntimeError(linked.stderr)
                result = run('start', name)
                status = run('show', name, '-p', 'ExecMainStatus', '-p', 'Result').stdout
                if broken:
                    assert result.returncode and 'ExecMainStatus=243' in status, status
                else:
                    assert result.returncode == 0 and 'Result=success' in status, status + result.stderr
            finally:
                run('stop', name)
                run('disable', name)
                run('reset-failed', name)
                run('daemon-reload')
    print('真实 systemd 检查通过：旧引号写法复现 243，修正后从标准暂存路径成功读取两个凭据。')


if __name__ == '__main__':
    main()
