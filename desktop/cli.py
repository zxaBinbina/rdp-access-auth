"""Route installed non-root CLI management through the same password authority."""
from desktop.control import Client
from management import ManagementError


def manage(args, prompt, output):
    if args.config or args.state or args.service or args.scope != 'system':
        raise ManagementError('安装版命令行管理本机后台；自定义运行参数请用于源码开发或 serve。')
    client = Client()
    if args.command == 'status' or args.command == 'service' and args.action == 'status':
        output(client.request('status'))
        return 0
    if args.command == 'config' and args.config_action in ('show', 'validate'):
        result = client.request('settings.read')
        output(result)
        if args.config_action == 'validate' and (not result.get('exists') or result.get('validation_error') or not result.get('wordlist', {}).get('ok')):
            raise ManagementError('配置或词库尚未通过校验。')
        return 0
    if args.command == 'config' and args.config_action == 'init':
        raise ManagementError('首次安装请运行 rdp-auth deploy 使用欢迎向导。Agent 请使用 MCP。')
    password = prompt('管理密码：')
    session = client.request('security.login', {'password': password})['session']
    try:
        if args.command == 'service':
            method = 'logs.read' if args.action == 'logs' else 'services.' + ('pause' if args.action == 'stop' else args.action)
            output(client.request(method, session=session))
        elif args.command == 'unlock':
            output(client.request('credentials.unlock', session=session))
        elif args.command == 'config':
            current = client.request('settings.read')
            changes = {key: getattr(args, key) for key in ('hostname', 'rdp_address', 'tunnel_id', 'wordlist_path') if getattr(args, key) is not None}
            if args.password:
                changes['password'] = prompt('新的固定访问密码：')
                if changes['password'] != prompt('再次输入固定访问密码：'):
                    raise ManagementError('两次密码不一致。')
            if args.sakura_token: changes['sakura_token'] = prompt('SakuraFrp 访问密钥：')
            if args.turnstile:
                changes['turnstile_site_key'] = prompt('Turnstile Site key：')
                changes['turnstile_secret_key'] = prompt('Turnstile Secret key：')
            if args.disable_turnstile: changes['disable_turnstile'] = True
            admission = {key: getattr(args, flag) for key, flag in [('listen_port', 'admission_listen_port'), ('target_port', 'admission_target_port'), ('duration_seconds', 'admission_duration')] if getattr(args, flag) is not None}
            if admission: changes['local_admission'] = {**(current['config'].get('local_admission') or {}), **admission}
            output(client.request('settings.save', {'changes': changes, 'revision': current['revision']}, session=session))
        elif args.command == 'migrate':
            plan = client.request('migration.plan', session=session)
            output(plan)
            if not args.dry_run:
                if input('按以上计划迁移？[y/N]：').lower() in ('y', 'yes'):
                    output(client.request('migration.start', {'revision': plan.get('revision'), 'recover': args.recover}, session=session))
    finally:
        client.request('security.logout', session=session)
    return 0
