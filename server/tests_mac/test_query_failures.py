"""Query failures remain unknown and never authorize phone control."""
import asyncio
import tempfile
import json
from pathlib import Path
import secrets
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp.test_utils import TestClient, TestServer
from server.common.api import create_app
from server.common.call_state import CallState
from server.common.contracts import GatewayError
from server.common.diagnostics import exception_fields, sanitize
from server.common.storage import Store
from test_mac_gateway import fixture, PHONE


class QueryFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_retains_identity_and_tracker_unknown_then_recovers(self):
        adapter, protocol, _ = fixture()
        await adapter.operate(PHONE, 'dial', '123')  # Substitute protocol only.
        identity = adapter.owned_signature
        item = {'address': PHONE, 'callId': 'call1', 'identity': identity}
        tracker = CallState(adapter, item, lambda: None)
        tracker.set('active')
        updated = asyncio.Event()
        tracker.notify = updated.set
        with patch.object(protocol, 'query_current_calls', AsyncMock(side_effect=TimeoutError('private-value'))):
            tracker.start()
            await asyncio.wait_for(updated.wait(), 1)
            self.assertEqual(tracker.snapshot()['state'], 'unknown')
            self.assertEqual(tracker.snapshot()['lastKnownState'], 'active')
            self.assertEqual(adapter.owned_signature, identity)
            await tracker.close()
        self.assertNotIn('AT+CHUP', protocol.commands)
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'dialing')
        protocol.current = []
        self.assertEqual(await adapter.call_snapshot(PHONE, 'call1', identity), 'ended')

    async def test_disconnect_must_belong_to_current_adapter(self):
        adapter, protocol, _ = fixture()
        other, _, _ = fixture()
        other.connection.emit('disconnection')
        async def query():
            raise TimeoutError('private-value')
        protocol.query_current_calls = query
        with self.assertRaises(GatewayError) as raised:
            await adapter.calls(PHONE)
        self.assertEqual(raised.exception.code, 'hfp_query_timeout')
        async def disconnected_query():
            adapter.connection.emit('disconnection')
            raise TimeoutError('private-value')
        protocol.query_current_calls = disconnected_query
        with self.assertRaises(GatewayError) as raised:
            await adapter.calls(PHONE)
        self.assertEqual(raised.exception.code, 'phone_disconnected')
        self.assertEqual(type(raised.exception.__cause__), TimeoutError)
        self.assertEqual(protocol.commands, [])

    async def test_api_classification_privacy_and_preflight_no_dial(self):
        adapter, protocol, _ = fixture()
        root = Path(tempfile.gettempdir()) / 'phone-call-gateway/query-tests'/secrets.token_hex(8)
        store = Store(root)
        app = create_app(adapter, managed=True, store=store)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for error, status, code in ((TimeoutError('private-value'), 503, 'hfp_query_timeout'),
                    (ConnectionError('private-value'), 503, 'hfp_query_failed'),
                    (RuntimeError('private-value'), 500, 'internal_error')):
                with patch.object(protocol, 'query_current_calls', AsyncMock(side_effect=error)):
                    response = await client.get(f'/v1/devices/{PHONE}/calls')
                    self.assertEqual(response.status, status)
                    self.assertEqual(await response.json(), {'error': {'code': code}})
                    response = await client.post('/v1/conversations', json={'address': PHONE, 'number': '+123',
                        'task': {'purpose': 'Test', 'outcome': 'Test', 'content': 'Test', 'source': 'editor'}})
                    self.assertEqual(response.status, status)
            runtime = app['conversations']
            self.assertFalse(runtime.items)
            self.assertTrue(runtime.line.idle())
            self.assertFalse(runtime.sessions)
            self.assertFalse(runtime.recovering)
            self.assertEqual(protocol.commands, [])
        finally:
            await client.close()
        raw = (root/'logs/calls.jsonl').read_text()
        self.assertNotIn('private-value', raw)
        records = [json.loads(line) for line in raw.splitlines()]
        failures = [r for r in records if r['event'] == 'operation_failure']
        self.assertEqual(len(failures), 6)
        self.assertEqual(failures[-1]['errorType'], 'RuntimeError')
        self.assertEqual(failures[-1]['errorCode'], 'unclassified')
        self.assertEqual(failures[0]['causeType'], 'TimeoutError')
        self.assertTrue(all(r['errorStage'] == 'api_request' and 'durationMs' in r for r in failures))

    def test_exact_error_classes_without_messages(self):
        for error, code in ((TimeoutError('secret'), 'audio_transport_timeout'),
                (RuntimeError('secret'), 'unclassified')):
            value = sanitize(exception_fields(error, 'capture_transport'))
            self.assertEqual(value['errorType'], type(error).__name__)
            self.assertEqual(value['errorCode'], code)
            self.assertNotIn('secret', json.dumps(value))
