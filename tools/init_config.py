"""Generate a private configuration; secrets are entered interactively."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

def main():
    p = argparse.ArgumentParser(description='生成配置，不覆盖现有文件')
    p.add_argument('--hostname', required=True)
    p.add_argument('--rdp-address', required=True)
    p.add_argument('--tunnel-id', required=True, type=int)
    p.add_argument('--output', type=Path, default=Path('private/portal-settings.json'))
    args = p.parse_args()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', args.hostname) or '.' not in args.hostname:
        p.error('请输入认证域名，不含协议和端口')
    if args.tunnel_id <= 0 or args.output.exists():
        p.error('隧道 ID 必须为正整数，输出文件必须不存在')
    password = getpass.getpass('固定密码（16～128位，含大小写、数字、特殊符号）：')
    if not 16 <= len(password) <= 128 or not all(re.search(r, password) for r in (r'[a-z]', r'[A-Z]', r'[0-9]', r'[^A-Za-z0-9\s]')):
        p.error('密码不符合要求')
    if password != getpass.getpass('再次输入密码：'):
        p.error('两次密码不一致')
    token = getpass.getpass('SakuraFrp API Token：').strip()
    if not token:
        p.error('API Token 不能为空')
    salt = secrets.token_hex(16)
    config = dict(hostname=args.hostname, rdp_address=args.rdp_address, tunnel_id=args.tunnel_id,
        password_salt=salt, password_hash=hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex(),
        session_key=secrets.token_hex(32), sakura_token=token)
    os.umask(0o077)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
        f.write('\n')
    args.output.chmod(0o600)
    print('配置已保存：', args.output, '；未输出凭据。')

if __name__ == '__main__':
    main()
