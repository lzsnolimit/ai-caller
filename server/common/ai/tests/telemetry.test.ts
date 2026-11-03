import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Telemetry } from '../src/telemetry.js';

test('resolved Luna configuration alone does not claim a backend invocation', () => {
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(record => records.push(record));
  logger.observe({ type: 'session.started', session: { model: 'gpt-live-1',
    delegation: { responses: { model: 'gpt-6-luna' } }, instructions: 'private task' } });
  assert.equal(logger.snapshot().backendCreated, 0);
  assert.deepEqual(logger.snapshot().observedBackendModels, []);
  assert.equal(records[0]!.resolvedBackendModel, 'gpt-6-luna');
  assert(!JSON.stringify(records).includes('private task'));
});

test('nested response events record actual model and backend duration without content', () => {
  let now = 1000;
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(record => records.push(record), () => now);
  logger.observe({ type: 'response.event', delegation_id: 'private-identifier', event: {
    type: 'response.created', response: { id: 'response-1', model: 'gpt-6-luna', instructions: 'private' } } });
  now += 1350;
  logger.observe({ type: 'response.event', event: { type: 'response.completed',
    response: { id: 'response-1', model: 'gpt-6-luna', output: ['private answer'] } } });
  assert.equal(records[1]!.durationMs, 1350);
  assert.equal(records[0]!.backendResponseIndex, records[1]!.backendResponseIndex);
  assert.equal(logger.snapshot().backendCreated, 1);
  assert.equal(logger.snapshot().backendCompleted, 1);
  assert.deepEqual(logger.snapshot().observedBackendModels, ['gpt-6-luna']);
  assert(!JSON.stringify(records).includes('private'));
  assert(!JSON.stringify(records).includes('response-1'));
});

test('transcript, tool arguments and provider errors are not logged', () => {
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(record => records.push(record));
  logger.observe({ type: 'session.input_transcript.delta', delta: 'private transcript' });
  logger.observe({ type: 'response.event', event: { type: 'response.function_call_arguments.delta', delta: 'secret-key' } });
  logger.observe({ type: 'error', error: { message: 'secret-key', code: 'private' } });
  assert.deepEqual(records.map(record => record.event), ['model_error']);
  assert(!JSON.stringify(records).includes('secret-key'));
});

test('audio activity is sampled, metadata only, and is not a speech recognition event', () => {
  let now = 0;
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(record => records.push(record), () => now);
  const pcm = Buffer.alloc(960);
  for (let offset = 0; offset < pcm.length; offset += 2) pcm.writeInt16LE(1000, offset);
  logger.audio('input', pcm);
  now += 10;
  logger.audio('input', Buffer.alloc(960));
  assert.equal(records.length, 1);
  now += 100;
  logger.audio('input', Buffer.alloc(960));
  assert.deepEqual(records.map(record => record.active), [true, false]);
  assert.equal(records[1]!.elapsedMs, 110);
  now += 300;
  logger.audio('output', pcm);
  now += 300;
  logger.sampleQuiet();
  assert.equal(records.at(-1)!.direction, 'output');
  assert.equal(records.at(-1)!.deliveryGap, true);
  assert.equal(records.at(-1)!.active, false);
});

test('stage aggregates are bounded, use one clock, and preserve arithmetic', () => {
  let now = 5000;
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(value => records.push(value), () => now);
  for (let i = 0; i < 10000; i++) logger.stage('model_input_send', 2, 960, 3);
  assert.equal(records.length, 0);
  now += 20; logger.flush();
  assert.equal(records[0]!.frames, 10000);
  assert.equal(records[0]!.meanMs, 2);
  assert.equal(records[0]!.bytes, 9600000);
  assert.equal(records[0]!.elapsedMs, 20);
  assert.equal(records[0]!.clockDomain, 'node_performance');
  const broken = new Telemetry(() => { throw Error('failed sink'); });
  assert.doesNotThrow(() => { broken.stage('model_input_send', 1); broken.flush(); });
});

test('first output uses session ready and does not invent a speech turn', () => {
  let now = 100;
  const records: Record<string, unknown>[] = [];
  const logger = new Telemetry(value => records.push(value), () => now);
  logger.observe({type:'session.started'});
  now=150; logger.observe({type:'session.output_audio.delta'});
  now=160; logger.observe({type:'session.output_audio.delta'});
  logger.flush();
  const first = records.find(value=>value.stage==='model_first_output');
  assert.equal(first?.frames,1); assert.equal(first?.meanMs,50);
  assert.equal(records.find(value=>value.stage==='model_output_gap')?.meanMs,10);
  assert.equal(first?.audioSequence,2);
  assert(!('turnIndex' in first!));
});
