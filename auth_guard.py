"""Persistent admission throttling; all credential methods share the same guard."""
from contextlib import closing
import secrets
import sqlite3
import time

class AuthGuard:
    def __init__(self, path, ip_seconds=900, global_seconds=900, burst_seconds=600):
        self.path = path
        self.ip_seconds = ip_seconds
        self.global_seconds = global_seconds
        self.burst_seconds = burst_seconds
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS guard_global (id INTEGER PRIMARY KEY CHECK(id=1), until INTEGER NOT NULL)')
            db.execute('INSERT OR IGNORE INTO guard_global VALUES (1,0)')
            db.execute('CREATE TABLE IF NOT EXISTS guard_ips (ip TEXT PRIMARY KEY, failures INTEGER NOT NULL, until INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS guard_bans (ip TEXT PRIMARY KEY, occurred INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS guard_requests (token TEXT PRIMARY KEY, ip TEXT NOT NULL, expires INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS guard_options (ip TEXT PRIMARY KEY, started INTEGER, count INTEGER)')

    def options_allowed(self, ip):
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM guard_options WHERE started <= ?', (now - 300,))
            row = db.execute('SELECT count FROM guard_options WHERE ip=?', (ip,)).fetchone()
            if row and row[0] >= 30:
                return False
            db.execute('INSERT INTO guard_options VALUES (?,?,1) ON CONFLICT(ip) DO UPDATE SET count=count+1', (ip, now))
            return True

    def begin(self, ip):
        """Reserve one of at most five in-flight checks. Return (token, wait, scope)."""
        now = int(time.time())
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM guard_requests WHERE expires <= ?', (now,))
            until = db.execute('SELECT until FROM guard_global WHERE id=1').fetchone()[0]
            if until > now:
                return None, until - now, 'global'
            row = db.execute('SELECT failures, until FROM guard_ips WHERE ip=?', (ip,)).fetchone()
            failures, until = row or (0, 0)
            if until > now:
                return None, until - now, 'ip'
            if until:
                db.execute('UPDATE guard_ips SET failures=0, until=0 WHERE ip=?', (ip,))
                failures = 0
            pending = db.execute('SELECT count(*) FROM guard_requests WHERE ip=?', (ip,)).fetchone()[0]
            if failures + pending >= 5:
                return None, 60, 'pending'
            token = secrets.token_urlsafe(24)
            db.execute('INSERT INTO guard_requests VALUES (?,?,?)', (token, ip, now + 60))
            return token, 0, ''

    def finish(self, token, success):
        """False counts wrong credentials; None releases transient/non-auth failures."""
        now = int(time.time())
        with closing(sqlite3.connect(self.path, timeout=5)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('DELETE FROM guard_requests WHERE token=? RETURNING ip,expires', (token,)).fetchone()
            if not row or row[1] <= now:
                return
            ip = row[0]
            if success is True:
                db.execute('DELETE FROM guard_ips WHERE ip=?', (ip,))
            elif success is False:
                row = db.execute('SELECT failures,until FROM guard_ips WHERE ip=?', (ip,)).fetchone()
                if row and row[1] > now:
                    return
                count = (row[0] if row else 0) + 1
                until = now + self.ip_seconds if count >= 5 else 0
                db.execute('INSERT INTO guard_ips VALUES (?,?,?) ON CONFLICT(ip) DO UPDATE SET failures=excluded.failures, until=excluded.until',
                           (ip, count, until))
                if until:
                    db.execute('DELETE FROM guard_bans WHERE occurred < ?', (now - self.burst_seconds,))
                    db.execute('INSERT INTO guard_bans VALUES (?,?) ON CONFLICT(ip) DO UPDATE SET occurred=excluded.occurred', (ip, now))
                    if db.execute('SELECT count(*) FROM guard_bans').fetchone()[0] >= 5:
                        db.execute('UPDATE guard_global SET until=? WHERE id=1', (now + self.global_seconds,))
                        db.execute('DELETE FROM guard_bans')

    def locked(self, ip):
        now = int(time.time())
        with closing(sqlite3.connect(self.path)) as db:
            until = db.execute('SELECT until FROM guard_global WHERE id=1').fetchone()[0]
            if until > now:
                return until - now, 'global'
            row = db.execute('SELECT until FROM guard_ips WHERE ip=?', (ip,)).fetchone()
            return (row[0] - now, 'ip') if row and row[0] > now else (0, '')
