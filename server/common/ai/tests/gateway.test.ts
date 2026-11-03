import assert from 'node:assert/strict';
import { once } from 'node:events';
import { createServer } from 'node:http';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { WebSocketServer } from 'ws';
import { GatewayClient } from '../src/gateway.js';

async function fixture() {
  let owner = false;
  let released = 0;
  const headers: Array<Record<string, unknown>> = [];
  const server = createServer((request, response) => {
    headers.push(request.headers);
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/v1/lease') {
      if (request.method === 'POST') {
        if (owner) {
          response.writeHead(409).end('{}');
          return;
        }
        owner = true;
        response.end(JSON.stringify({ lease: 'controller', expiresIn: 30 }));
      } else if (request.headers['x-controller-lease'] !== 'controller') {
        response.writeHead(409).end('{}');
      } else if (request.method === 'DELETE') {
        owner = false;
        released++;
        response.writeHead(204).end();
      } else response.end('{}');
    } else response.end(JSON.stringify({ telephony: false }));
  });
  const ws = new WebSocketServer({ server });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const address = server.address();
  assert(address && typeof address !== 'string');
  return {
    client: () => new GatewayClient(`http://127.0.0.1:${address.port}`),
    ws,
    headers,
    released: () => released,
    close: async () => {
      for (const socket of ws.clients) socket.terminate();
      await new Promise<void>(resolve => ws.close(() => resolve()));
      await new Promise<void>(resolve => server.close(() => resolve()));
    },
  };
}

test('plain origin validation prevents credentials and path confusion', () => {
  for (const url of [
    'ftp://raspberrypi.local',
    'http://user:pass@raspberrypi.local',
    'http://raspberrypi.local/api',
    'http://raspberrypi.local/?key=x',
    'http://raspberrypi.local/#secret',
  ])
    assert.throws(() => new GatewayClient(url));
});

test('call events replay after reconnect and never fail the audio client for unknown state', async () => {
  const f = await fixture();
  const client = f.client();
  const failures: string[] = [];
  const states: string[] = [];
  client.on('failure', value => failures.push(value));
  client.on('callState', value => states.push(value));
  let connections = 0;
  f.ws.on('connection', (socket, request) => {
    assert.match(request.url!, /\/v1\/conversations\/[a-f0-9]{32}\/events/);
    connections++;
    socket.send(JSON.stringify({ type: 'call.state', state: connections === 1 ? 'active' : 'held' }));
  });
  try {
    const first = once(client, 'callState');
    client.watchCall('a'.repeat(32));
    assert.deepEqual(await first, ['active']);
    const lost = once(client, 'callState');
    [...f.ws.clients][0]!.terminate();
    assert.deepEqual(await lost, ['unknown']);
    assert.deepEqual(await once(client, 'callState'), ['held']);
    assert.deepEqual(failures, []);
    assert.equal(f.headers.length, 0);
    await client.close();
    assert.deepEqual(states, ['active', 'unknown', 'held']);
  } finally { await client.close(); await f.close(); }
});

test('legacy call events use only the selected phone and preserve unknown', async () => {
  const f = await fixture();
  const client = f.client();
  f.ws.on('connection', (socket, request) => {
    assert.equal(request.url, '/v1/events');
    socket.send(JSON.stringify({ type: 'phone.state', phones: [
      { address: '11:22:33:44:55:66', calls: [{ id: 'call1', state: 'active' }] },
      { address: 'AA:BB:CC:DD:EE:FF', calls: null },
    ] }));
  });
  try {
    const state = once(client, 'callState');
    client.watchCall(undefined, 'AA:BB:CC:DD:EE:FF', 'call1');
    assert.deepEqual(await state, ['unknown']);
    const next = once(client, 'callState');
    [...f.ws.clients][0]!.send(JSON.stringify({ type: 'phone.state', phones: [{ address: 'AA:BB:CC:DD:EE:FF', calls: [{ id: 'call1', state: 'active' }] }] }));
    assert.deepEqual(await next, ['active']);
  } finally { await client.close(); await f.close(); }
});

test('no authentication; lease is only an exclusive control mechanism', async () => {
  const f = await fixture();
  const first = f.client();
  const second = f.client();
  try {
    assert.deepEqual(await first.request('/v1/capabilities'), { telephony: false });
    await first.acquire();
    await assert.rejects(second.acquire(), /409/);
    await first.close();
    await second.acquire();
    await second.close();
    assert.equal(f.released(), 2);
    assert(f.headers.every(header => !header.authorization));
  } finally {
    await first.close();
    await second.close();
    await f.close();
  }
});

test('invalid address does not acquire a lease or start audio', async () => {
  const f = await fixture();
  const client = f.client();
  try {
    await assert.rejects(client.openAudio('not-a-phone'));
    assert.equal(f.headers.length, 0);
  } finally {
    await client.close();
    await f.close();
  }
});

test('audio waits for SCO ready, sends short frames and releases on stop', async () => {
  const f = await fixture();
  const client = f.client();
  const frames: number[] = [];
  f.ws.on('connection', (socket, request) => {
    assert.equal(request.headers['x-controller-lease'], 'controller');
    assert.equal(request.headers.authorization, undefined);
    socket.send(JSON.stringify({ type: 'audio.ready' }));
    socket.on('message', (data, binary) => {
      assert(binary);
      frames.push((data as Buffer).length);
    });
  });
  try {
    assert.throws(() => client.send(Buffer.alloc(960)));
    const ready = once(client, 'ready');
    await client.openAudio('AA:BB:CC:DD:EE:01');
    await ready;
    client.send(Buffer.alloc(2880));
    // A ping/pong barrier waits for the server to receive all earlier frames.
    const socket = [...f.ws.clients][0]!;
    const pong = once(socket, 'pong');
    socket.ping();
    await pong;
    assert.deepEqual(frames, [960, 960, 960]);
    await client.close();
    assert.equal(f.released(), 1);
    assert.throws(() => client.send(Buffer.alloc(960)));
  } finally {
    await client.close();
    await f.close();
  }
});

test('one audio websocket carries both PCM directions without mixing them', async () => {
  const f = await fixture();
  const client = f.client();
  const capture = Buffer.alloc(960, 3);
  const playback = Buffer.alloc(960, 5);
  let connections = 0;
  let received: Buffer | undefined;
  f.ws.on('connection', socket => {
    connections++;
    socket.send(JSON.stringify({ type: 'audio.ready' }));
    socket.send(capture);
    socket.on('message', (data, binary) => { if (binary) received = Buffer.from(data as Buffer); });
  });
  try {
    const input = once(client, 'pcm');
    await client.openAudio('AA:BB:CC:DD:EE:FF');
    assert.deepEqual((await input)[0], capture);
    client.send(playback);
    const socket = [...f.ws.clients][0]!;
    const pong = once(socket, 'pong');
    socket.ping();
    await pong;
    assert.deepEqual(received, playback);
    assert.equal(connections, 1);
  } finally { await client.close(); await f.close(); }
});

test('odd PCM fails rather than corrupting subsequent audio', async () => {
  const f = await fixture();
  const client = f.client();
  f.ws.on('connection', socket => {
    socket.send(JSON.stringify({ type: 'audio.ready' }));
    socket.send(Buffer.alloc(3));
  });
  try {
    const failure = once(client, 'failure');
    await client.openAudio('AA:BB:CC:DD:EE:01');
    assert.deepEqual(await failure, ['invalid_gateway_frame']);
  } finally {
    await client.close();
    await f.close();
  }
});

test('unexpected close before SCO ready makes the real CLI exit nonzero', async () => {
  const f = await fixture();
  f.ws.on('connection', socket => socket.terminate());
  try {
    const child = spawn(
      process.execPath,
      [
        '--import',
        'tsx',
        fileURLToPath(new URL('../src/index.ts', import.meta.url)),
        '--gateway',
        f.client().base.origin,
        '--address',
        'AA:BB:CC:DD:EE:01',
      ],
      {
        env: {
          ...process.env,
          OPENAI_API_KEY: 'unused-test-fixture',
          OPENAI_LIVE_URL: 'wss://example.invalid/live/sessions',
        },
        stdio: ['ignore', 'pipe', 'pipe'],
      }
    );
    let output = '';
    child.stdout.on('data', data => {
      output += data.toString();
    });
    const [code] = await once(child, 'exit');
    assert.equal(code, 1);
    assert.match(output, /gateway_disconnected/);
  } finally {
    await f.close();
  }
});

test('unexpected close after SCO ready emits failure', async () => {
  const f = await fixture();
  const client = f.client();
  f.ws.on('connection', socket => {
    socket.send(JSON.stringify({ type: 'audio.ready' }));
    setImmediate(() => socket.terminate());
  });
  try {
    const failure = once(client, 'failure');
    await client.openAudio('AA:BB:CC:DD:EE:01');
    assert.deepEqual(await failure, ['gateway_disconnected']);
  } finally {
    await client.close();
    await f.close();
  }
});

test('normal audio.closed and local stop do not emit failure', async () => {
  for (const remoteStop of [true, false]) {
    const f = await fixture();
    const client = f.client();
    const failures: string[] = [];
    client.on('failure', code => failures.push(code));
    f.ws.on('connection', socket => socket.send(JSON.stringify({ type: 'audio.ready' })));
    try {
      const ready = once(client, 'ready');
      await client.openAudio('AA:BB:CC:DD:EE:01');
      await ready;
      if (remoteStop) {
        const ended = once(client, 'ended');
        [...f.ws.clients][0]!.send(JSON.stringify({ type: 'audio.closed' }));
        await ended;
      }
      await client.close();
      assert.deepEqual(failures, []);
    } finally {
      await client.close();
      await f.close();
    }
  }
});
