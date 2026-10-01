"""Local-only state inspection and unlocking; no credential output."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

def main():
    p = argparse.ArgumentParser()
    p.add_argument('action', choices=['status', 'unlock'])
    p.add_argument('--state', type=Path, default=Path('/var/lib/rdp-access-auth/state.sqlite3'))
    args = p.parse_args()
    if not args.state.is_file():
        p.error('数据库不存在或无权访问；systemd 部署请使用 sudo')
    with closing(sqlite3.connect(args.state)) as db, db:
        if args.action == 'unlock':
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE guard_global SET until=0 WHERE id=1')
            for name in ('guard_ips', 'guard_bans', 'guard_requests', 'guard_options'):
                db.execute('DELETE FROM ' + name)
            print('已解除封禁，凭据未更改。')
        else:
            now = int(time.time())
            print(json.dumps(dict(passkeys=db.execute('SELECT count(*) FROM passkeys').fetchone()[0],
                temporary_generation=db.execute('SELECT generation FROM temporary_password WHERE id=1').fetchone()[0],
                global_lock_seconds=max(0, db.execute('SELECT until FROM guard_global WHERE id=1').fetchone()[0]-now),
                banned_ips=db.execute('SELECT count(*) FROM guard_ips WHERE until>?', (now,)).fetchone()[0])))

if __name__ == '__main__':
    main()
