"""Loopback-only, PROXY-protocol TCP admission gateway and persistent grants."""
import asyncio
from contextlib import closing
import ipaddress
import secrets
import sqlite3
import struct
import threading
import time


def configuration(settings):
    value = settings.get('local_admission')
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'listen_port', 'target_port', 'duration_seconds'}:
        raise ValueError('local_admission must contain listen_port, target_port and duration_seconds only')
    result = dict(listen_port=13389, target_port=3389, duration_seconds=21600)
    result.update(value)
    for key in ('listen_port', 'target_port'):
        if type(result[key]) is not int or not 1024 <= result[key] <= 65535:
            raise ValueError('Admission ports must be integers from 1024 to 65535')
    if result['listen_port'] == result['target_port']:
        raise ValueError('Admission listener and RDP target must use different ports')
    if type(result['duration_seconds']) is not int or not 60 <= result['duration_seconds'] <= 86400:
        raise ValueError('Admission lifetime must be between 60 and 86400 seconds')
    return result


class Admissions:
    def __init__(self, path, duration=21600):
        self.path, self.duration = path, duration
        self.last_heartbeat = None
        with closing(self.connect()) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS admission_grants (ip TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=1)

    def ready(self):
        return self.last_heartbeat is not None and 0 <= time.monotonic() - self.last_heartbeat < 3

    def heartbeat(self):
        self.last_heartbeat = time.monotonic()

    def get(self, ip):
        with closing(self.connect()) as db:
            row = db.execute('SELECT token,expires FROM admission_grants WHERE ip=? AND expires>?', (ip, time.time())).fetchone()
        return dict(token=row[0], expires=row[1]) if row else None

    def grant(self, ip):
        ip = str(ipaddress.ip_address(ip))
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM admission_grants WHERE expires<=?', (time.time(),))
            row = db.execute('SELECT token FROM admission_grants WHERE ip=? AND expires>?', (ip, time.time())).fetchone()
            db.execute('INSERT OR REPLACE INTO admission_grants VALUES (?,?,?)',
                       (ip, row[0] if row else secrets.token_urlsafe(24), time.time() + self.duration))

    def revoke(self, ip):
        with closing(self.connect()) as db, db:
            db.execute('DELETE FROM admission_grants WHERE ip=?', (ip,))

    def active(self):
        with closing(self.connect()) as db:
            return dict(db.execute('SELECT ip,token FROM admission_grants WHERE expires>?', (time.time(),)))


async def proxy_source(reader):
    """Consume exactly one bounded v1/v2 header. Never trust a destination in it."""
    prefix = await reader.readexactly(12)
    if prefix == b'\r\n\r\n\x00\r\nQUIT\n':
        version, family, size = struct.unpack('!BBH', await reader.readexactly(4))
        if version != 0x21 or family not in (0x11, 0x21) or size > 512:
            raise ValueError('Expected PROXY v2 TCP')
        payload = await reader.readexactly(size)
        width = 4 if family == 0x11 else 16
        if len(payload) < width * 2 + 4:
            raise ValueError('Truncated PROXY address')
        source = ipaddress.ip_address(payload[:width])
    elif prefix.startswith(b'PROXY '):
        line = prefix
        while not line.endswith(b'\r\n'):
            if len(line) >= 108:
                raise ValueError('PROXY v1 header too long')
            line += await reader.readexactly(1)
        parts = line.decode('ascii').strip().split()
        if len(parts) != 6 or parts[1] not in ('TCP4', 'TCP6'):
            raise ValueError('Expected PROXY v1 TCP')
        source, destination = ipaddress.ip_address(parts[2]), ipaddress.ip_address(parts[3])
        version = 4 if parts[1] == 'TCP4' else 6
        if source.version != version or destination.version != version or any(
                not p.isdecimal() or not 1 <= int(p) <= 65535 for p in parts[4:]):
            raise ValueError('Invalid PROXY address')
    else:
        raise ValueError('PROXY protocol required')
    if isinstance(source, ipaddress.IPv6Address) and source.ipv4_mapped:
        source = source.ipv4_mapped
    if not source.is_global:
        raise ValueError('Public source address required')
    return str(source)


class Gateway:
    def __init__(self, store, listen_port, target_port):
        self.store, self.listen_port, self.target_port = store, listen_port, target_port
        self.connections = {}
        self.pending = set()
        self.server = None

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', self.listen_port, limit=65536)
        self.store.heartbeat()
        self.monitor = asyncio.create_task(self.watch())
        return self.server.sockets[0].getsockname()[1]

    async def watch(self):
        while True:
            try:
                active = self.store.active()
                for task, (ip, token) in list(self.connections.items()):
                    if active.get(ip) != token:
                        task.cancel()
                self.store.heartbeat()
            except sqlite3.Error:
                # A failed state read never keeps existing streams admitted.
                self.store.last_heartbeat = None
                for task in list(self.pending):
                    task.cancel()
            await asyncio.sleep(0.25)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        upstream = None
        pipes = []
        try:
            if len(self.pending) >= 128 or not self.store.ready():
                return
            self.pending.add(task)
            peer = writer.get_extra_info('peername')
            if not peer or not ipaddress.ip_address(peer[0]).is_loopback:
                return
            ip = await asyncio.wait_for(proxy_source(reader), 3)
            grant = self.store.get(ip)
            if not grant:
                return
            self.connections[task] = (ip, grant['token'])
            backend, upstream = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', self.target_port), 3)
            # Recheck after connect, before forwarding any RDP payload.
            current = self.store.get(ip)
            if not current or current['token'] != grant['token']:
                return

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            pipes = [asyncio.create_task(copy(reader, upstream)), asyncio.create_task(copy(backend, writer))]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, sqlite3.Error, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            self.connections.pop(task, None)
            self.pending.discard(task)
            for pipe in pipes:
                pipe.cancel()
            for stream in (writer, upstream):
                if stream:
                    stream.close()
            if pipes:
                await asyncio.gather(*pipes, return_exceptions=True)

    async def stop(self):
        self.server.close()
        self.monitor.cancel()
        pending = list(self.pending)
        for task in pending:
            task.cancel()
        await asyncio.gather(self.monitor, *pending, return_exceptions=True)
        await self.server.wait_closed()
        self.store.last_heartbeat = None


def start_gateway(store, config):
    """Start once in the single gunicorn worker; fail worker boot on bind errors."""
    ready, outcome = threading.Event(), []

    def run():
        async def serve():
            gateway = Gateway(store, config['listen_port'], config['target_port'])
            try:
                await gateway.start()
            except Exception as exc:
                outcome.append(exc)
                ready.set()
                return
            ready.set()
            try:
                await gateway.server.serve_forever()
            finally:
                await gateway.stop()
        asyncio.run(serve())

    thread = threading.Thread(target=run, name='rdp-admission', daemon=True)
    thread.start()
    if not ready.wait(5):
        raise RuntimeError('Admission gateway startup timed out')
    if outcome:
        raise RuntimeError('Admission gateway could not bind') from outcome[0]
    return thread
