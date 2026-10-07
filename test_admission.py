import asyncio
from contextlib import closing
import ipaddress
from pathlib import Path
import sqlite3
import struct
import tempfile
import time
import unittest

from admission import Admissions, Gateway, configuration


def header(ip='8.8.8.8', version=1):
    if version == 1:
        return f'PROXY TCP4 {ip} 127.0.0.1 45000 3389\r\n'.encode()
    body = ipaddress.ip_address(ip).packed + ipaddress.ip_address('127.0.0.1').packed + struct.pack('!HH', 45000, 3389)
    return b'\r\n\r\n\x00\r\nQUIT\n' + struct.pack('!BBH', 0x21, 0x11, len(body)) + body


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Admissions(str(Path(self.tmp.name) / 'state.db'))
        self.received, self.accepted, self.writers = [], [], []

        async def echo(reader, writer):
            self.accepted.append(writer)
            try:
                while data := await reader.read(65536):
                    self.received.append(data)
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        self.backend = await asyncio.start_server(echo, '127.0.0.1', 0)
        self.gateway = Gateway(self.store, 0, self.backend.sockets[0].getsockname()[1])
        self.port = await self.gateway.start()

    async def asyncTearDown(self):
        await self.gateway.stop()
        for writer in self.writers + self.accepted:
            writer.close()
        self.backend.close()
        await self.backend.wait_closed()
        self.tmp.cleanup()

    async def connect(self, ip='8.8.8.8', version=1):
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        self.writers.append(writer)
        writer.write(header(ip, version) + b'RDP-payload')
        await writer.drain()
        return reader, writer

    async def test_unauthorized_never_reaches_rdp(self):
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.assertEqual(self.accepted, [])

    async def test_v1_v2_strip_headers_and_forward_bidirectionally(self):
        self.store.grant('8.8.8.8')
        for version in (1, 2):
            reader, writer = await self.connect(version=version)
            self.assertEqual(await asyncio.wait_for(reader.readexactly(11), 2), b'RDP-payload')
            writer.write(b'second')
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.readexactly(6), 2), b'second')
        self.assertNotIn(b'PROXY', b''.join(self.received))

    async def test_revoke_disconnects_only_target_and_blocks_new_connections(self):
        for ip in ('8.8.8.8', '1.1.1.1'):
            self.store.grant(ip)
        first, _ = await self.connect()
        other, writer = await self.connect('1.1.1.1')
        for reader in (first, other):
            await asyncio.wait_for(reader.readexactly(11), 2)
        self.store.revoke('8.8.8.8')
        self.assertEqual(await asyncio.wait_for(first.read(), 2), b'')
        writer.write(b'still-connected')
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(other.readexactly(15), 2), b'still-connected')
        reader, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.assertEqual(len(self.accepted), 2)

    async def test_revoke_then_regrant_still_closes_old_connection(self):
        self.store.grant('8.8.8.8')
        reader, _ = await self.connect()
        await reader.readexactly(11)
        self.store.revoke('8.8.8.8')
        self.store.grant('8.8.8.8')
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        new, _ = await self.connect()
        self.assertEqual(await asyncio.wait_for(new.readexactly(11), 2), b'RDP-payload')

    async def test_expiry_and_database_failure_close_streams(self):
        self.store.grant('8.8.8.8')
        reader, _ = await self.connect()
        await reader.readexactly(11)
        with closing(self.store.connect()) as db, db:
            db.execute('UPDATE admission_grants SET expires=?', (time.time()-1,))
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.store.grant('8.8.8.8')
        reader, _ = await self.connect()
        await reader.readexactly(11)
        with closing(self.store.connect()) as db, db:
            db.execute('DROP TABLE admission_grants')
        self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')

    async def test_missing_malformed_and_private_headers_are_rejected(self):
        self.store.grant('8.8.8.8')
        for payload in (b'not-a-proxy-header', header('127.0.0.1'), b'PROXY UNKNOWN\r\n',
                        b'PROXY TCP4 ' + b'x'*110, header(version=2)[:12] + b'\x20\x11\x00\x00'):
            reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
            self.writers.append(writer)
            writer.write(payload)
            writer.write_eof()
            await writer.drain()
            self.assertEqual(await asyncio.wait_for(reader.read(), 2), b'')
        self.assertEqual(self.accepted, [])

    async def test_persistence_and_gateway_health(self):
        self.store.grant('8.8.8.8')
        restored = Admissions(self.store.path)
        self.assertEqual(restored.get('8.8.8.8'), self.store.get('8.8.8.8'))
        self.assertFalse(restored.ready())  # Readiness cannot survive a process restart.
        self.assertTrue(self.store.ready())
        self.store.last_heartbeat -= 5
        self.assertFalse(self.store.ready())


class ConfigurationTests(unittest.TestCase):
    def test_gateway_is_opt_in_and_ports_cannot_loop(self):
        self.assertIsNone(configuration({}))
        self.assertEqual(configuration({'local_admission': {}})['listen_port'], 13389)
        for config in (False, {'listen_port': 3389}, {'listen_port': True}, {'target_port': 0},
                       {'duration_seconds': 0}, {'target_host': 'evil.example'}):
            with self.assertRaises(ValueError):
                configuration({'local_admission': config})


if __name__ == '__main__':
    unittest.main()
