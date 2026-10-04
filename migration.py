"""Adopt an existing deployment into the native package, preserving its identity.

Only a dedicated systemd drop-in is installed. The original config, state paths,
service name and connector continue to be used. A durable journal makes an
interrupted switch recoverable on the next launch.
"""
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading

from management import (ROOT, ManagementError, atomic_private_write, check_wordlist,
                        config_lock, read_config, validate_config)
from deploy import health_check

PENDING = {'stopping', 'switching', 'recovery-needed'}
DROPIN = '90-rdp-access-auth-package.conf'


def system(command):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        raise ManagementError('systemd 操作超时或不可用，请检查系统服务状态。') from None
    if result.returncode:
        # Do not return arbitrary unit output which might contain credentials.
        raise ManagementError('systemd ' + command[1] + ' 失败，请在管理页查看服务日志。')
    return result.stdout


def probe_service(target):
    output = system(['systemctl', 'show', target.service, '--no-pager',
                     '--property=LoadState,ActiveState,ExecStart,WorkingDirectory,FragmentPath,DropInPaths,StateDirectory,Type,Result,ExecMainStatus'])
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


def credential_line(name, path):
    # LoadCredential does not use ExecStart's shell-like quoting rules. Quotes
    # become literal filename characters, causing 243/CREDENTIALS at startup.
    value = str(path)
    if not Path(value).is_absolute() or not re.fullmatch(r'[A-Za-z0-9/_.:@+=-]+', value):
        raise ManagementError('服务凭据需要使用助手生成的标准路径。')
    return f'LoadCredential={name}:{value}\n'


def failure_summary(exc, properties):
    code = str(properties.get('ExecMainStatus', ''))
    if code == '243':
        return 'systemd 无法加载凭据文件（243/CREDENTIALS），请检查凭据来源路径。'
    if code == '203':
        return 'systemd 无法执行新版程序（203/EXEC），请检查软件包文件和执行权限。'
    if properties.get('Result') == 'oom-kill':
        return '新版认证服务超过内存限制，被系统终止。'
    return str(exc) if isinstance(exc, ManagementError) else '新版程序启动或健康检查失败，请查看服务日志。'


def sqlite_backup(source, destination):
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        if src.execute('PRAGMA quick_check').fetchone() != ('ok',):
            raise ManagementError('原数据库完整性检查失败，迁移已停止。')
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)


def validate_runtime(config, database, words):
    # Initialize the packaged app on disposable copies, never the live database.
    settings = json.loads(config.read_bytes())
    settings['wordlist_path'] = str(words)
    config.write_text(json.dumps(settings))
    env = dict(os.environ, PYTHONPATH=str(ROOT / 'vendor') + os.pathsep + str(ROOT),
               RDP_AUTH_CONFIG=str(config), RDP_AUTH_STATE=str(database), RDP_AUTH_WORDLIST=str(words),
               PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run(['/usr/bin/python3', '-c', 'import portal'], cwd=ROOT, env=env,
                            capture_output=True, timeout=30)
    if result.returncode:
        raise ManagementError('新版程序无法读取现有配置或数据库，原服务未切换。请核对运行依赖和数据库版本。')


class Migration:
    def __init__(self, target, root=Path('/'), check_host=True, probe=probe_service,
                 runner=system, health=health_check, validator=validate_runtime):
        self.target, self.root, self.check_host = target, Path(root), check_host
        self.probe, self.runner, self.health, self.validator = probe, runner, health, validator
        self.dropin = self.root / 'etc/systemd/system' / (target.service + '.d') / DROPIN
        self.receipt = target.config.parent / 'package-migration.json'
        self.backups = target.config.parent / 'migration-backups'
        self.lock = threading.RLock()
        self.worker = None
        self.events, self.error, self.phase = [], '', 'idle'

    def record(self):
        try:
            return json.loads(self.receipt.read_bytes())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ManagementError('迁移记录不可读，请检查 package-migration.json。') from None

    def pending(self):
        return self.record().get('phase') in PENDING

    def progress(self, message):
        with self.lock:
            self.events.append(message)

    def plan(self):
        target = self.target
        expected = {'legacy': 'rdp-auth.service', 'system': 'rdp-access-auth.service'}
        if expected.get(target.profile) != target.service or target.scope != 'system':
            raise ManagementError('迁移支持标准部署和旧版部署的系统服务。')
        if self.check_host:
            from management import select_target
            canonical = select_target(target.profile)
            if target.config != canonical.config or target.state != canonical.state:
                raise ManagementError('自定义配置或数据库路径需先核对，不能自动迁移。')
            if ROOT != Path('/usr/lib/rdp-access-auth') or not (ROOT / 'PACKAGED.json').is_file():
                raise ManagementError('请安装新版 RPM/DEB 后，从应用菜单执行迁移。')
        if self.pending():
            raise ManagementError('上次迁移未完成，请先恢复原服务。')
        properties = self.probe(target)
        if properties.get('LoadState') != 'loaded':
            raise ManagementError('原服务未加载，请先修复服务单元。')
        workdir = properties.get('WorkingDirectory')
        packaged = workdir == '/usr/lib/rdp-access-auth'
        if packaged:
            return dict(available=False, adopted=True, message='当前服务已使用软件包中的程序。')
        if workdir != str(target.runtime) or properties.get('Type') != 'simple':
            raise ManagementError('原服务使用了自定义运行目录或服务类型，请先核对部署。')
        if properties.get('StateDirectory') != target.service.removesuffix('.service'):
            raise ManagementError('原服务的状态目录与管理目标不一致，迁移已停止。')
        command = properties.get('ExecStart', '')
        ports = re.findall(r'(?:127\.0\.0\.1:|--port[=\s]+)([0-9]{1,5})(?=\s|;|$)', command)
        if len(set(ports)) != 1 or not 1024 <= int(ports[0]) <= 65535:
            raise ManagementError('无法可靠识别原服务的本机端口，迁移已停止。')
        value, revision = read_config(target.config)
        validate_config(value)
        words = check_wordlist(target, value)
        if not words['ok']:
            raise ManagementError(words['message'])
        credential_line('package-settings.json', target.config)
        if not target.state.is_file():
            raise ManagementError('未找到原状态数据库，不能迁移已有凭据。')
        if os.path.lexists(self.dropin):
            raise ManagementError('已存在迁移专用服务覆盖文件，请检查迁移记录。')
        # Plan fingerprint also protects against changed service overrides/wordlists.
        digest = hashlib.sha256(revision.encode())
        digest.update(Path(words['path']).read_bytes())
        for key in ('ExecStart', 'WorkingDirectory', 'StateDirectory', 'Type'):
            digest.update(properties.get(key, '').encode())
        for file in [properties.get('FragmentPath', ''), *properties.get('DropInPaths', '').split()]:
            if file:
                digest.update(Path(file).read_bytes())
        return dict(available=True, adopted=False, revision=digest.hexdigest(), hostname=value['hostname'],
                    port=int(ports[0]), service=target.service, config=str(target.config), state=str(target.state),
                    wordlist=words['path'], current_program=str(target.runtime), new_program='/usr/lib/rdp-access-auth',
                    backup_directory=str(self.backups), was_active=properties.get('ActiveState') == 'active',
                    message='备份后切换到软件包程序；保留原配置、数据库、服务名、端口和开机启动设置。')

    def status(self):
        with self.lock:
            value = dict(phase=self.phase, events=self.events[-30:], error=self.error)
            try:
                value['recovery_required'] = self.pending() and self.phase != 'running'
                value['plan'] = self.plan() if self.phase != 'running' and not value['recovery_required'] else {}
                if value['recovery_required']:
                    value['message'] = '检测到未完成的迁移，请恢复原服务后重试。'
                value['backup'] = self.record().get('backup', '')
                record = self.record()
                if not value['error'] and self.phase != 'running' and record.get('failure'):
                    value['error'] = '上次迁移失败：' + record['failure']
            except ManagementError as exc:
                value['plan'] = dict(available=False, message=str(exc))
            return value

    def start(self, revision=None, recover=False):
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ManagementError('迁移正在执行。', 409)
            if self.check_host and os.geteuid() != 0:
                raise ManagementError('请从应用菜单重新打开，以管理员权限迁移。', 403)
            if recover:
                if not self.pending():
                    raise ManagementError('没有待恢复的迁移。')
            elif not revision or self.plan().get('revision') != revision:
                raise ManagementError('迁移计划已变化，请重新检查后确认。', 409)
            self.phase, self.error, self.events = 'running', '', []
            self.worker = threading.Thread(target=self._run, args=(revision, recover), daemon=False)
            self.worker.start()
        return self.status()

    def _write_record(self, record, phase):
        record['phase'] = phase
        atomic_private_write(self.receipt, (json.dumps(record, ensure_ascii=False) + '\n').encode())

    def _run(self, revision, recover):
        try:
            lockfile = self.root / 'run/lock/rdp-access-auth-deploy.lock'
            lockfile.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(lockfile, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w') as lock, config_lock(self.target.config):
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ManagementError('另一项部署或迁移正在进行。', 409) from None
                if recover:
                    self._restore(self.record())
                else:
                    self._apply(revision)
            with self.lock:
                self.phase = 'complete'
        except Exception as exc:
            with self.lock:
                self.phase = 'failed'
                self.error = str(exc) if isinstance(exc, ManagementError) else '迁移未完成，请检查文件权限、磁盘空间和服务日志。'

    def _apply(self, revision):
        plan = self.plan()
        if plan.get('revision') != revision:
            raise ManagementError('配置或服务已变化，请重新核对迁移计划。', 409)
        self.progress('在临时副本上验证新版程序与现有凭据的兼容性')
        with tempfile.TemporaryDirectory(prefix='rdp-migration-check-') as temporary:
            stage = Path(temporary)
            atomic_private_write(stage / 'settings.json', self.target.config.read_bytes())
            sqlite_backup(self.target.state, stage / 'state.sqlite3')
            self.validator(stage / 'settings.json', stage / 'state.sqlite3', Path(plan['wordlist']))
        self.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='migration-', dir=self.backups))
        atomic_private_write(backup / 'settings.json', self.target.config.read_bytes())
        atomic_private_write(backup / 'objects.json', Path(plan['wordlist']).read_bytes())
        record = dict(backup=str(backup), was_active=plan['was_active'], port=plan['port'],
                      hostname=plan['hostname'], service=self.target.service, database_ready=False)
        # A crash before database backup only requires restarting the untouched old service.
        self._write_record(record, 'stopping')
        try:
            self.progress('暂停认证服务并备份数据库（包含通行密钥和临时密码）')
            self.runner(['systemctl', 'stop', self.target.service])
            sqlite_backup(self.target.state, backup / 'state.sqlite3')
            (backup / 'state.sqlite3').chmod(0o600)
            record['database_ready'] = True
            self._write_record(record, 'switching')
            self.dropin.parent.mkdir(parents=True, exist_ok=True)
            # Use the private, stable copy so user-selected wordlist paths with
            # spaces/non-ASCII characters never enter systemd's credential parser.
            content = self.unit_override(dict(plan, wordlist=str(backup / 'objects.json')))
            atomic_private_write(self.dropin, content.encode())
            self.dropin.chmod(0o644)
            restorecon = shutil.which('restorecon') if self.check_host else None
            if restorecon:
                self.runner([restorecon, '-F', str(self.dropin)])
            self.runner(['systemctl', 'daemon-reload'])
            self.runner(['systemctl', 'start', self.target.service])
            properties = self.probe(self.target)
            if properties.get('WorkingDirectory') != '/usr/lib/rdp-access-auth' or '/usr/bin/rdp-auth' not in properties.get('ExecStart', ''):
                raise ManagementError('迁移覆盖文件未生效，正在恢复原服务。')
            self.progress('检查新版认证服务的本机健康状态')
            self.health(plan['hostname'], plan['port'])
            if not plan['was_active']:
                self.runner(['systemctl', 'stop', self.target.service])
            self._write_record(record, 'complete')
            self.progress('迁移完成；备份已保留，后续升级软件包并重启该服务即可。')
        except Exception as exc:
            try:
                properties = self.probe(self.target)
            except Exception:
                properties = {}
            reason = failure_summary(exc, properties)
            record['failure'] = reason
            try:
                self._write_record(record, record['phase'])
            except OSError:
                pass  # A diagnostic write failure must not prevent recovery.
            self._restore(record)
            raise ManagementError(reason + ' 已恢复原程序与数据库。') from None

    def unit_override(self, plan):
        def quoted(path):
            return json.dumps(str(path).replace('%', '%%'), ensure_ascii=False)
        return ('# Managed by RDP Access Auth migration\n[Service]\n'
                'WorkingDirectory=/usr/lib/rdp-access-auth\n'
                'Environment=PYTHONPATH=/usr/lib/rdp-access-auth/vendor:/usr/lib/rdp-access-auth\n'
                'Environment=RDP_AUTH_WORDLIST=%d/package-words.json\n'
                + credential_line('package-settings.json', self.target.config)
                + credential_line('package-words.json', plan['wordlist'])
                + 'ExecStart=\nExecStart=/usr/bin/rdp-auth --config %d/package-settings.json '
                f'--state {quoted(self.target.state)} serve --port {plan["port"]}\n')

    def _restore(self, record):
        self.progress('恢复原服务入口与迁移前数据库')
        try:
            backup = Path(record['backup'])
            if backup.parent != self.backups or backup.is_symlink():
                raise ManagementError('迁移备份路径无效。')
            self.runner(['systemctl', 'stop', self.target.service])
            if record.get('database_ready'):
                self.dropin.unlink(missing_ok=True)
                # SQLite backup handles WAL consistently and keeps file owner/mode.
                sqlite_backup(backup / 'state.sqlite3', self.target.state)
            self.runner(['systemctl', 'daemon-reload'])
            if record['was_active']:
                self.runner(['systemctl', 'start', self.target.service])
                self.health(record['hostname'], record['port'])
            self._write_record(record, 'rolled-back')
            self.progress('已恢复原程序和原运行状态。')
        except Exception:
            self._write_record(record, 'recovery-needed')
            raise ManagementError('自动恢复尚未完成，备份已保留；修复系统权限或服务问题后点击“恢复原服务”。') from None

    def close(self):
        if self.worker:
            self.worker.join()
