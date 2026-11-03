import assert from 'node:assert/strict';
import { test } from 'node:test';

const moduleURL = new URL('../../../../client/assets/device-voice.js', import.meta.url).href;
const { DeviceVoice } = await import(moduleURL);
const { setLanguage } = await import(new URL('../../../../client/assets/i18n.js', import.meta.url).href);
setLanguage('zh', false);
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const calls: any[] = [];
  const request = (server: string, path: string, method = 'GET', body?: unknown) => new Promise((resolve, reject) => {
    calls.push({ server, path, method, body, resolve, reject });
  });
  return { calls, view: new DeviceVoice(request, () => {}) };
}

test('device preference reads do not save defaults and stale device/server reads cannot apply', async () => {
  const {calls, view} = harness();
  const a = view.load('one', 'A'); await tick();
  await assert.rejects(view.save('quartz'), /读取完成/);
  const b = view.load('one', 'B'); await tick();
  calls[1].resolve({voice: 'vesper'}); await b;
  calls[0].resolve({voice: 'quartz'}); await a;
  assert.equal(view.state.voice, 'vesper');
  const old = view.load('one', 'A'); await tick();
  view.reset();
  const next = view.load('two', 'A'); await tick();
  calls[3].resolve({voice: 'marin'}); await next;
  calls[2].resolve({voice: 'quartz'}); await old;
  assert.equal(view.state.server, 'two'); assert.equal(view.state.voice, 'marin');
  assert(calls.every(call => call.method === 'GET'));
});

test('rapid choices save in order, last choice survives refresh and switching devices', async () => {
  const {calls, view} = harness();
  const load = view.load('one', 'A'); await tick(); calls[0].resolve({voice: 'marin'}); await load;
  const first = view.save('quartz'), last = view.save('vesper'); await tick();
  assert.equal(calls.length, 2); assert.equal(calls[1].body.voice, 'quartz');
  calls[1].resolve({voice: 'quartz'}); await first; await tick();
  assert.equal(view.state.voice, 'vesper'); assert(view.state.saving);
  assert.equal(calls[2].body.voice, 'vesper');
  const refresh = view.load('one', 'A'); await tick();
  assert.equal(calls.length, 3, 'Refresh waits for the final save');
  calls[2].resolve({voice: 'vesper'}); await last; await tick();
  calls[3].resolve({voice: 'vesper'}); await refresh;
  assert.equal(view.state.voice, 'vesper'); assert(view.state.ready);
  const savedA = view.save('quartz'); await tick();
  const loadB = view.load('two', 'B'); await tick();
  calls[5].resolve({voice: 'marin'}); await loadB;
  calls[4].resolve({voice: 'quartz'}); await savedA;
  assert.equal(calls[4].server, 'one'); assert.match(calls[4].path, /A\/voice$/);
  assert.equal(view.state.server, 'two'); assert.equal(view.state.address, 'B'); assert.equal(view.state.voice, 'marin');
});

test('failed save is visible and does not block a later selection', async () => {
  const {calls, view} = harness();
  const load = view.load('one', 'A'); await tick(); calls[0].resolve({voice: 'marin'}); await load;
  const failed = view.save('quartz'); await tick(); calls[1].reject(Error('offline')); await failed;
  assert.match(String(view.state.status), /未保存.*offline/);
  const retry = view.save('vesper'); await tick(); calls[2].resolve({voice: 'vesper'}); await retry;
  assert.match(String(view.state.status), /已保存/); assert.equal(view.pending.size, 0);
});
