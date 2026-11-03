import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { EventEmitter, once } from 'node:events';
import { createServer as httpServer } from 'node:http';
import { createServer as httpsServer } from 'node:https';
import { chmod, mkdir, mkdtemp, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import WebSocket, { WebSocketServer } from 'ws';

let certificate: Buffer, privateKey: Buffer, certificatePath: string;
before(async () => {
  const root = join(tmpdir(), 'phone-call-gateway/worker-tests');
  await mkdir(root, { recursive: true, mode: 0o700 });
  const folder = await mkdtemp(join(root, 'tls-'));
  certificatePath = join(folder, 'certificate.pem');
  const keyPath = join(folder, 'key.pem');
  const result = spawnSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
    '-keyout', keyPath, '-out', certificatePath, '-subj', '/CN=localhost',
    '-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], { encoding: 'utf8' });
  assert.equal(result.status, 0, 'Offline TLS fixture requires openssl');
  await chmod(keyPath, 0o600);
  certificate = await readFile(certificatePath);
  privateKey = await readFile(keyPath);
});
after(() => { privateKey = Buffer.alloc(0); });

async function fixture(initialState: string) {
  const changed = new EventEmitter();
  const conversationId = 'a'.repeat(32);
  let state = initialState, stateSocket: WebSocket | undefined, audioSocket: WebSocket | undefined;
  let modelSocket: WebSocket | undefined, session: Record<string, any> | undefined;
  let stateConnections = 0, audioConnections = 0, released = 0, unexpectedRequests = 0;
  const transcripts: Record<string, any>[] = [];
  const input: Buffer[] = [], output: Buffer[] = [], comments: string[] = [], reports: Record<string, any>[] = [];
  const gateway = httpServer((request, response) => {
    response.setHeader('Content-Type', 'application/json');
    if (request.url === '/v1/lease') {
      if (request.method === 'DELETE') { released++; response.writeHead(204).end(); }
      else response.end(JSON.stringify({ lease: 'offline-owner', expiresIn: 30 }));
    } else { unexpectedRequests++; response.writeHead(404).end('{}'); }
  });
  const gatewayWS = new WebSocketServer({ server: gateway });
  gatewayWS.on('connection', (socket, request) => {
    if (request.url === `/v1/conversations/${conversationId}/events`) {
      stateSocket = socket; stateConnections++;
      socket.send(JSON.stringify({ type: 'call.state', state }));
    } else {
      assert.match(request.url!, /^\/v1\/audio\?address=/);
      assert.equal(request.headers['x-controller-lease'], 'offline-owner');
      audioSocket = socket; audioConnections++;
      socket.send(JSON.stringify({ type: 'audio.ready' }));
      socket.on('message', (data, binary) => {
        assert(binary); output.push(Buffer.from(data as Buffer)); changed.emit('change');
      });
    }
    changed.emit('change');
  });
  const model = httpsServer({ key: privateKey, cert: certificate });
  const modelWS = new WebSocketServer({ server: model });
  modelWS.on('connection', socket => {
    modelSocket = socket;
    socket.on('message', data => {
      const event = JSON.parse(data.toString());
      if (event.type === 'session.start') session = event.session;
      if (event.type === 'session.input_audio.append') input.push(Buffer.from(event.audio, 'base64'));
      if (event.type === 'session.commentary.append') comments.push(event.content);
      if (event.type === 'session.close') socket.send(JSON.stringify({ type: 'session.closed', reason: 'completed' }));
      changed.emit('change');
    });
  });
  gateway.listen(0, '127.0.0.1'); model.listen(0, '127.0.0.1');
  await Promise.all([once(gateway, 'listening'), once(model, 'listening')]);
  const gatewayAddress = gateway.address(), modelAddress = model.address();
  assert(gatewayAddress && typeof gatewayAddress !== 'string');
  assert(modelAddress && typeof modelAddress !== 'string');
  const child = spawn(process.execPath, ['--import', 'tsx', fileURLToPath(new URL('../src/index.ts', import.meta.url)),
    '--gateway', `http://127.0.0.1:${gatewayAddress.port}`, '--address', 'AA:BB:CC:DD:EE:FF', '--call-test'], {
    env: { PATH: process.env.PATH, HOME: process.env.HOME, PHONE_MANAGED: '1', PHONE_CONVERSATION_ID: conversationId,
      PHONE_TEST_CALL_ID: 'call1', PHONE_TRANSCRIPT_FD:'3', OPENAI_API_KEY: 'offline-fixture-only',
      OPENAI_LIVE_URL: `wss://127.0.0.1:${modelAddress.port}/v1/live/sessions`, NODE_EXTRA_CA_CERTS: certificatePath,
      OPENAI_BACKEND_MODEL: 'gpt-6-luna' }, stdio: ['pipe', 'pipe', 'pipe', 'pipe'],
  });
  let textRemainder='';
  (child.stdio[3] as NodeJS.ReadableStream).on('data', data=>{textRemainder+=data.toString();const lines=textRemainder.split('\n');textRemainder=lines.pop()!;for(const line of lines)if(line)transcripts.push(JSON.parse(line));changed.emit('change');});
  const exit = once(child, 'exit');
  let rawOutput = '', remainder = '';
  child.stdout.on('data', data => {
    rawOutput += data.toString(); remainder += data.toString();
    const lines = remainder.split('\n'); remainder = lines.pop()!;
    for (const line of lines) { if (line) reports.push(JSON.parse(line)); }
    changed.emit('change');
  });
  child.stderr.resume();
  child.stdin.write(JSON.stringify({ type: 'init', task: { purpose: 'fixture purpose', outcome: 'fixture outcome',
    content: 'private fixture task', systemPrompt: 'private fixture prompt' } }) + '\n');
  async function until(predicate: () => boolean) {
    if (predicate()) return;
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => { changed.off('change', inspect); reject(Error('Worker condition was not reached')); }, 5000);
      function inspect() { if (predicate()) { clearTimeout(timer); changed.off('change', inspect); resolve(); } }
      changed.on('change', inspect); inspect();
    });
  }
  async function barrier(socket: WebSocket) { const pong = once(socket, 'pong'); socket.ping(); await pong; }
  return {
    until, input, output, comments, reports, child, exit, transcripts,
    text: (value:unknown)=>modelSocket!.send(JSON.stringify(value)),
    session: () => session!, rawOutput: () => rawOutput,
    ready: () => modelSocket!.send(JSON.stringify({ type: 'session.started' })),
    state: (next: string) => { state = next; stateSocket!.send(JSON.stringify({ type: 'call.state', state })); },
    capture: async (pcm: Buffer) => { audioSocket!.send(pcm); await barrier(audioSocket!); },
    playback: (pcm: Buffer) => modelSocket!.send(JSON.stringify({ type: 'session.output_audio.delta', delta: pcm.toString('base64') })),
    reconnect: () => stateSocket!.terminate(),
    stateConnections: () => stateConnections, audioConnections: () => audioConnections,
    released: () => released, unexpectedRequests: () => unexpectedRequests,
    close: async () => {
      if (child.exitCode === null) { child.kill('SIGTERM'); const timer = setTimeout(() => child.kill('SIGKILL'), 3000); await exit; clearTimeout(timer); }
      for (const ws of [gatewayWS, modelWS]) { for (const socket of ws.clients) socket.terminate(); await new Promise<void>(resolve => ws.close(() => resolve())); }
      await Promise.all([gateway, model].map(server => new Promise<void>(resolve => server.close(() => resolve()))));
    },
  };
}

for (const order of ['model-first', 'answer-first']) {
  test(`real worker gates greeting and duplex PCM: ${order}`, { timeout: 15000 }, async t => {
    const f = await fixture(order === 'answer-first' ? 'active' : 'alerting');
    t.after(f.close);
    await f.until(() => !!f.session());
    assert.equal(f.session().model, 'gpt-live-1');
    assert.equal(f.session().delegation.responses.model, 'gpt-6-luna');
    assert.equal(f.session().store, false);
    assert.deepEqual(f.session().audio, { format: { type: 'audio/pcm', rate: 24000 }, output: { voice: 'marin' } });
    const capture = Buffer.alloc(960, 3), playback = Buffer.alloc(960, 5);
    await f.capture(capture);
    assert.equal(f.input.length, 0);
    assert.equal(f.comments.length, 0);
    f.ready();
    await f.until(() => f.reports.some(report => report.sessionStarted));
    if (order === 'model-first') {
      await f.capture(capture);
      assert.equal(f.input.length, 0);
      assert.equal(f.comments.length, 0);
      f.state('active');
    }
    await f.until(() => f.comments.length === 1 && f.reports.some(report => report.callAnswered));
    f.state('active');
    await f.capture(capture); f.playback(playback);
    await f.until(() => f.input.length === 1 && f.output.length === 1);
    assert.deepEqual(f.input[0], capture); assert.deepEqual(f.output[0], playback);
    for(const role of ['input','output'])f.text({type:`session.${role}_transcript.delta`,event_id:role,delta:' private-transcript 你好 你好 ',start_ms:20,end_ms:90});
    await f.until(()=>f.transcripts.filter(e=>e.type==='transcript.delta').length===2);
    assert(f.transcripts.some(e=>e.role==='caller'&&e.text===' private-transcript 你好 你好 '));
    assert(f.transcripts.some(e=>e.role==='ai'));
    f.text({type:'session.input_transcript.delta',delta:42});
    f.state('unknown'); f.reconnect();
    await f.until(() => f.stateConnections() === 2);
    await f.capture(capture); f.playback(playback);
    await f.until(() => f.input.length === 2 && f.output.length === 2);
    assert.equal(f.child.exitCode, null);
    assert.equal(f.comments.length, 1);
    assert.equal(f.audioConnections(), 1);
    f.state('ended');
    const [code] = await f.exit;
    assert.equal(code, 0);
    const ended = f.reports.find(report => report.sessionClosed);
    assert.equal(ended?.callAudioObserved, true);
    assert.equal(ended?.inputBytes, 1920); assert.equal(ended?.outputBytes, 1920);
    assert.equal(f.released(), 1);
    assert.equal(f.unexpectedRequests(), 0);
    assert(!f.reports.some(report => report.bridgeError));
    assert(!f.rawOutput().includes('private fixture'));
    assert(!f.rawOutput().includes('private-transcript'));
  });
}
