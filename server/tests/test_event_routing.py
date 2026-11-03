"""One adapter change must reach every existing event API consumer."""
import asyncio
import tempfile
from pathlib import Path
import secrets
import unittest
from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.storage import Store
from server.tests.test_console import Phones, PHONE, OTHER


class EventRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def check_streams(self, managed):
        phones = Phones()
        phones.connected = {PHONE}
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/event-routing-tests' / secrets.token_hex(8)
        client = TestClient(TestServer(create_app(phones, lambda _: None, managed=managed, store=Store(root))))
        await client.start_server()
        legacy = await client.ws_connect('/v1/events')
        console = await client.ws_connect('/v1/console/events') if managed else None
        async def receive(socket, connected):
            value = await socket.receive_json(timeout=1)
            device = next(d for d in value['devices'] if d['address'] == OTHER)
            self.assertEqual(device['connected'], connected)
        try:
            streams = [legacy, *([console] if console else [])]
            await asyncio.gather(*(receive(ws, False) for ws in streams))
            for connected in (True, False):
                if connected: phones.connected.add(OTHER)
                else: phones.connected.discard(OTHER)
                phones.on_change()
                await asyncio.gather(*(receive(ws, connected) for ws in streams))
            await legacy.close()
            if console:
                phones.connected.add(OTHER)
                phones.on_change()
                await receive(console, True)
                replay = await client.ws_connect('/v1/events')
                await receive(replay, True)
                await replay.close()
        finally:
            await legacy.close()
            if console: await console.close()
            await client.close()

    async def test_managed_device_changes_reach_legacy_and_console_streams(self):
        await self.check_streams(True)

    async def test_nonmanaged_device_changes_reach_legacy_stream(self):
        await self.check_streams(False)
