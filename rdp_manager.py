"""The supported rdp-auth command. Management itself uses only the standard library."""
import argparse
import getpass
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import package_bootstrap  # noqa: F401 - native packages carry their own Python libraries

from management import (ROOT, DATA_ROOT, PACKAGED, ManagementError, check_wordlist, public_config, read_config,
                        save_config, select_target, service_operation, snapshot,
                        state_operation, validate_config)


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def secret(prompt):
    if not sys.stdin.isatty():
        raise ManagementError('敏感信息需要在交互终端中隐藏输入，或使用浏览器管理页填写。')
    return getpass.getpass(prompt)


def new_password():
    value = secret('固定密码（16～128 位，含大小写、数字和特殊符号）：')
    if value != secret('再次输入固定密码：'):
        raise ManagementError('两次密码不一致。')
    return value


def port_number(value):
    try:
        number = int(value)
        if 1 <= number <= 65535:
            return number
    except ValueError:
        pass
    raise argparse.ArgumentTypeError('端口必须为 1～65535。')


def parser():
    p = argparse.ArgumentParser(prog='./rdp-auth', description='RDP Access Auth 命令行与浏览器管理工具',
        epilog='默认只管理项目内的 private/ 配置。全局路径参数放在子命令前，例如：./rdp-auth --profile local gui')
    p.add_argument('--profile', choices=['local', 'system', 'legacy'], default='local',
                   help='local=当前项目（默认）；system=标准部署；legacy=旧版部署')
    p.add_argument('--config', type=Path, help='指定配置文件的路径')
    p.add_argument('--state', type=Path, help='指定状态数据库的路径')
    p.add_argument('--service', help='指定 systemd 服务名（以 .service 结尾）')
    p.add_argument('--scope', choices=['system', 'user'], default='system', help='systemd 服务范围')
    sub = p.add_subparsers(dest='command')
    launch = sub.add_parser('launch', help='自动检查本机部署，打开管理页或首次部署向导')
    launch.add_argument('url', nargs='?', help='教程链接，例如 rdp-auth://migrate?profile=legacy')
    launch.add_argument('--port', type=port_number, default=18124, help='浏览器管理端口')
    launch.add_argument('--no-open', action='store_true', help='只显示完整链接')
    migrate = sub.add_parser('migrate', help='将已有服务迁移为使用 RPM/DEB 中的程序')
    migrate.add_argument('--dry-run', action='store_true', help='仅检查并展示迁移计划')
    migrate.add_argument('--recover', action='store_true', help='恢复被中断的迁移')
    deploy = sub.add_parser('deploy', help='首次部署向导：浏览器表单或终端交互（需要安装 RPM/DEB）')
    deploy.add_argument('--gui', action='store_true', help='在浏览器中运行部署向导')
    deploy.add_argument('--dry-run', action='store_true', help='只展示部署位置和冲突，不修改系统、不询问凭据')
    deploy.add_argument('--port', type=port_number, default=18124, help='浏览器向导监听端口')
    deploy.add_argument('--no-open', action='store_true', help='仅打印浏览器向导链接')
    gui = sub.add_parser('gui', help='启动浏览器配置界面（无需安装依赖）')
    gui.add_argument('--port', type=port_number, default=18124, help='本机管理端口，默认 18124')
    gui.add_argument('--no-open', action='store_true', help='只显示链接，不自动打开浏览器')
    setup = sub.add_parser('setup', help='在项目 .venv 中安装认证服务依赖')
    setup.add_argument('--browser', action='store_true', help='同时安装 Playwright 和 Chromium，用于浏览器回归测试')
    config = sub.add_parser('config', help='创建、查看、修改和校验配置')
    config_sub = config.add_subparsers(dest='config_action', required=True)
    for action, description in [('init', '交互创建配置（不覆盖已有文件）'), ('set', '修改已有配置')]:
        edit = config_sub.add_parser(action, help=description)
        edit.add_argument('--hostname', help='认证域名，例如 auth.example.com')
        edit.add_argument('--rdp-address', help='远程桌面地址，例如 desktop.example.com:3389')
        edit.add_argument('--tunnel-id', type=int, help='SakuraFrp 隧道 ID')
        edit.add_argument('--wordlist-path', help='自定义词库绝对路径；空字符串恢复默认')
        if action == 'set':
            edit.add_argument('--password', action='store_true', help='隐藏输入新的固定密码')
            edit.add_argument('--sakura-token', action='store_true', help='隐藏输入新的 SakuraFrp Token')
        turnstile = edit.add_mutually_exclusive_group()
        turnstile.add_argument('--turnstile', action='store_true', help='交互配置 Turnstile Site key 和 Secret key')
        turnstile.add_argument('--disable-turnstile', action='store_true', help='关闭并清除 Turnstile 配置')
    config_sub.add_parser('show', help='查看配置摘要（不输出密码、Token 或会话密钥）')
    config_sub.add_parser('validate', help='校验配置与词库是否可用')
    status = sub.add_parser('status', help='查看配置、词库、认证封禁和服务状态')
    status.add_argument('--json', action='store_true', help='输出 JSON，供脚本读取')
    sub.add_parser('unlock', help='解除认证封禁，保留凭据')
    service = sub.add_parser('service', help='管理选定的 systemd 服务')
    service.add_argument('action', choices=['status', 'start', 'stop', 'restart', 'logs'])
    words = sub.add_parser('wordlist', help='构建中文词库')
    words.add_argument('--download', action='store_true', help='下载词库来源')
    words.add_argument('--refresh-sources', action='store_true', help='接受并记录上游来源更新')
    serve = sub.add_parser('serve', help='以前台方式启动认证服务（供 Cloudflare Tunnel 连接）')
    serve.add_argument('--port', type=port_number, default=18089)
    preview = sub.add_parser('preview', help='预览认证页面，使用示例数据')
    preview.add_argument('--port', type=port_number, default=18123)
    test = sub.add_parser('test', help='运行离线回归测试')
    test.add_argument('--browser', action='store_true', help='运行管理页的浏览器回归测试（需要 setup --browser）')
    return p


def require_runtime(*modules):
    if any(importlib.util.find_spec(name) is None for name in modules):
        raise ManagementError('认证运行依赖未安装，请先执行 ./rdp-auth setup。浏览器配置 gui 无需这些依赖。')


def configure(args, target):
    if args.config_action == 'show':
        value, _ = read_config(target.config)
        output(dict(path=str(target.config), config=public_config(value)))
        return
    if args.config_action == 'validate':
        value, _ = read_config(target.config)
        validate_config(value)
        wordlist = check_wordlist(target, value)
        if not wordlist['ok']:
            raise ManagementError(wordlist['message'] + ' 路径：' + wordlist['path'])
        print('配置校验通过；有效词库：' + str(wordlist['count']) + ' 个词。')
        return
    create = args.config_action == 'init'
    _, revision = read_config(target.config, missing_ok=create)
    if create and revision != 'missing':
        raise ManagementError('配置已经存在，请使用 config set 修改。')
    changes = {key: getattr(args, key) for key in ('hostname', 'rdp_address', 'tunnel_id', 'wordlist_path')
               if getattr(args, key) is not None}
    if create:
        if not sys.stdin.isatty():
            raise ManagementError('请在交互终端运行 config init，或使用 ./rdp-auth gui 创建配置。')
        for key, prompt in [('hostname', '认证域名：'), ('rdp_address', 'RDP 地址（域名:端口）：'), ('tunnel_id', 'SakuraFrp 隧道 ID：')]:
            if key not in changes:
                changes[key] = input(prompt).strip()
        try:
            changes['tunnel_id'] = int(changes['tunnel_id'])
        except ValueError:
            raise ManagementError('隧道 ID 必须为整数。') from None
    if create or args.password:
        changes['password'] = new_password()
    if create or args.sakura_token:
        changes['sakura_token'] = secret('SakuraFrp API Token：').strip()
        if not changes['sakura_token']:
            raise ManagementError('API Token 不能为空。')
    if args.turnstile:
        changes['turnstile_site_key'] = input('Turnstile Site key：').strip()
        changes['turnstile_secret_key'] = secret('Turnstile Secret key：').strip()
        if not all(changes[k] for k in ('turnstile_site_key', 'turnstile_secret_key')):
            raise ManagementError('Turnstile 的两个密钥都不能为空。')
    if args.disable_turnstile:
        changes['disable_turnstile'] = True
    if not changes:
        raise ManagementError('请指定要修改的选项。示例：config set --rdp-address desktop.example.com:3389')
    result = save_config(target, changes, revision=revision, create=create)
    print(result['message'] + '\n配置文件：' + str(target.config))
    if result['hostname_changed']:
        print('认证域名已改变：请同步 Cloudflare Tunnel 配置，并在新域名下重新绑定通行密钥。')


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        target = select_target(args.profile, args.config, args.state, args.service, args.scope)
        if args.command == 'launch':
            from installation import installed_targets, preferred_target
            from deploy import elevate_if_needed, run_wizard
            from management_web import run_gui
            from app_links import parse_app_link
            link = parse_app_link(args.url) if args.url else dict(view='open', profile='auto')
            if link['profile'] != 'auto':
                if args.profile != 'local' and args.profile != link['profile']:
                    raise ManagementError('命令参数和链接指定的管理目标不一致。')
                args.profile = link['profile']
            if args.config or args.state or args.service or args.scope != 'system':
                raise ManagementError('自动入口不接受自定义路径；请使用 gui 管理自定义部署。')
            elevate_if_needed(['--profile', args.profile, 'launch', '--port', str(args.port)] +
                              (['--no-open'] if args.no_open else []) + ([args.url] if args.url else []))
            targets = installed_targets()
            if targets and args.profile != 'local':
                target = next((item for item in targets if item.profile == args.profile), None)
                if target is None:
                    raise ManagementError('未检测到链接或命令指定的部署档案。请使用 rdp-auth launch 自动选择。')
            else:
                target = preferred_target(targets, service_operation)
            if target:
                run_gui(target, args.port, not args.no_open, view=link['view'], fallback_port=True)
            else:
                run_wizard(True, args.port, args.no_open)
        elif args.command == 'migrate':
            from installation import installed_targets, preferred_target
            from migration import Migration
            from deploy import elevate_if_needed
            if not args.dry_run:
                elevate_if_needed(['--profile', args.profile, 'migrate'] + (['--recover'] if args.recover else []))
            if args.config or args.state or args.service or args.scope != 'system':
                raise ManagementError('迁移不接受自定义路径或服务名。')
            if args.profile == 'local':
                target = preferred_target(installed_targets(), service_operation)
            if not target:
                raise ManagementError('未检测到已有部署，请先运行 rdp-auth launch。')
            migration = Migration(target)
            try:
                if args.recover:
                    if args.dry_run:
                        output(migration.status())
                        return 0
                    migration.start(recover=True)
                else:
                    plan = migration.plan()
                    output(plan)
                    if args.dry_run or not plan.get('available'):
                        return 0
                    if not sys.stdin.isatty():
                        raise ManagementError('请在交互终端确认迁移，或使用管理页。')
                    if input('备份后切换程序并短暂重启认证服务？[y/N]：').lower() not in ('y', 'yes'):
                        return 0
                    migration.start(plan['revision'])
                seen = 0
                while migration.worker.is_alive():
                    migration.worker.join(timeout=0.5)
                    events = migration.status()['events']
                    for event in events[seen:]:
                        print(event, flush=True)
                    seen = len(events)
                if migration.error:
                    raise ManagementError(migration.error)
            finally:
                migration.close()
        elif args.command == 'deploy':
            from deploy import deployment_plan, run_wizard
            if args.dry_run:
                output(deployment_plan())
            else:
                if args.profile != 'local' or args.config or args.state or args.service or args.scope != 'system':
                    raise ManagementError('首次部署使用固定系统路径，不接受管理目标参数；可用 deploy --dry-run 查看。')
                run_wizard(args.gui, args.port, args.no_open)
        elif args.command == 'gui':
            from management_web import run_gui
            run_gui(target, args.port, not args.no_open)
        elif args.command == 'config':
            configure(args, target)
        elif args.command == 'status':
            result = snapshot(target)
            if args.json:
                output(result)
            else:
                print(f'配置文件：{target.config}\n状态数据库：{target.state}\n服务：{target.service} ({target.scope})')
                print('配置：' + (result.get('config_error') or result.get('validation_error') or ('已创建' if result['exists'] else '未创建')))
                print('词库：' + ('可用' if result.get('wordlist', {}).get('ok') else '未就绪'))
                print('服务：' + ('本地模式（使用 ./rdp-auth serve 启动）' if target.profile == 'local' else
                                 result.get('service_error') or result['service'].get('ActiveState', '未知')))
                if 'state' in result:
                    print('通行密钥：{passkeys} 个；封禁 IP：{banned_ips} 个；全站锁定：{global_lock_seconds} 秒'.format(**result['state']))
                else:
                    print('认证状态：' + result['state_error'])
        elif args.command == 'unlock':
            state_operation(target, unlock=True)
            print('已解除认证封禁，密码和通行密钥保持不变。')
        elif args.command == 'service':
            output(service_operation(target, args.action))
        elif args.command == 'setup':
            if PACKAGED:
                print('RPM/DEB 已包含运行依赖，无需执行 setup。浏览器开发测试请使用源码目录。')
                return 0
            python = ROOT / '.venv/bin/python'
            if not python.exists():
                subprocess.run([sys.executable, '-m', 'venv', str(ROOT / '.venv')], check=True)
            subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.txt')], check=True)
            if args.browser:
                subprocess.run([str(python), '-m', 'pip', 'install', 'playwright>=1.50,<2'], check=True)
                subprocess.run([str(python), '-m', 'playwright', 'install', 'chromium'], check=True)
            print('运行依赖已准备完成。下一步：./rdp-auth wordlist --download')
        elif args.command == 'wordlist':
            from tools.build_wordlist import build_wordlist
            if args.refresh_sources and not args.download:
                raise ManagementError('--refresh-sources 需要 --download。')
            try:
                build_wordlist(DATA_ROOT, args.download, args.refresh_sources)
            except ValueError as exc:
                raise ManagementError(str(exc)) from None
        elif args.command == 'serve':
            require_runtime('flask', 'gunicorn', 'webauthn', 'cryptography')
            value, _ = read_config(target.config)
            validate_config(value)
            if os.environ.get('RDP_AUTH_WORDLIST'):
                value['wordlist_path'] = os.environ['RDP_AUTH_WORDLIST']
            # serve runs this checkout, including its default wordlist, for every profile.
            runtime_target = type(target)(target.config, target.state, target.service, DATA_ROOT, target.profile, target.scope)
            if not value.get('wordlist_path'):
                value['wordlist_path'] = str(DATA_ROOT / 'wordlists/objects.json')
            words = check_wordlist(runtime_target, value)
            if not words['ok']:
                raise ManagementError(words['message'])
            target.state.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            os.umask(0o077)
            env = dict(os.environ, RDP_AUTH_CONFIG=str(target.config), RDP_AUTH_STATE=str(target.state),
                       RDP_AUTH_WORDLIST=value['wordlist_path'])
            if PACKAGED:
                env['PYTHONPATH'] = str(ROOT / 'vendor') + os.pathsep + str(ROOT)
            return subprocess.call([sys.executable, '-m', 'gunicorn', '--workers', '1', '--threads', '4',
                '--bind', f'127.0.0.1:{args.port}', '--timeout', '30', '--access-logfile', '/dev/null',
                '--error-logfile', '-', 'portal:app'], cwd=ROOT, env=env)
        elif args.command == 'preview':
            require_runtime('flask')
            from tools.preview import app
            app.run(host='127.0.0.1', port=args.port, debug=False)
        elif args.command == 'test':
            if PACKAGED:
                raise ManagementError('软件包不包含开发测试，请在源码目录中运行测试。')
            env = dict(os.environ)
            env.pop('RDP_AUTH_CONFIG', None)
            env.pop('RDP_AUTH_STATE', None)
            if args.browser:
                if importlib.util.find_spec('playwright') is None:
                    raise ManagementError('浏览器测试依赖未安装，请先执行 ./rdp-auth setup --browser。')
                require_runtime('flask')
                for script in ('check_management_ui.py', 'check_deploy_ui.py', 'check_migration_ui.py', 'check_tabs.py', 'check_credentials_ui.py'):
                    result = subprocess.call([sys.executable, str(ROOT / 'tools' / script)], cwd=ROOT, env=env)
                    if result:
                        return result
                return 0
            require_runtime('flask', 'webauthn', 'cryptography')
            return subprocess.call([sys.executable, '-m', 'unittest', 'discover', '-q'], cwd=ROOT, env=env)
        return 0
    except ManagementError as exc:
        print('错误：' + str(exc), file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        print('错误：命令未完成，请检查权限、依赖或网络连接（' + type(exc).__name__ + '）。', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print('\n操作已取消。', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
