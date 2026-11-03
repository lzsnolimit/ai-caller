import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ModelAudioWriter, type AudioSocket } from '../src/model-audio.js';

class Socket implements AudioSocket {
  frames: string[] = [];
  callbacks: Array<(error?: Error) => void> = [];
  send(data: string, callback: (error?: Error) => void): void {
    this.frames.push(data);
    this.callbacks.push(callback);
  }
  flush(error?: Error): void {
    this.callbacks.shift()!(error);
  }
}

test('temporary upload backlog preserves frames in order without a pacing delay', () => {
  const socket = new Socket();
  let written = 0;
  const failures: string[] = [];
  const writer = new ModelAudioWriter(
    socket,
    bytes => {
      written += bytes;
    },
    code => failures.push(code)
  );
  for (let index = 0; index < 20; index++) writer.write(Buffer.alloc(960, index));
  assert.equal(socket.frames.length, 1); // Only one write is in flight.
  assert.equal(writer.peakQueuedBytes, 19200); // Old 16 KB threshold would stop this burst.
  for (let index = 0; index < 20; index++) socket.flush();
  assert.equal(written, 19200);
  assert.deepEqual(failures, []);
  assert.deepEqual(
    socket.frames.map(frame => Buffer.from(JSON.parse(frame).audio, 'base64')[0]),
    Array.from({ length: 20 }, (_, index) => index)
  );
  writer.close();
});

test('one-second queue limit stops sustained overload explicitly', () => {
  const socket = new Socket();
  const failures: string[] = [];
  const writer = new ModelAudioWriter(
    socket,
    () => {},
    code => failures.push(code)
  );
  for (let index = 0; index < 51; index++) writer.write(Buffer.alloc(960));
  assert.deepEqual(failures, ['model_audio_queue_full']);
  assert.equal(writer.peakQueuedBytes, 48000);
  socket.flush(); // Late completion must not restart the stopped queue.
  assert.equal(socket.frames.length, 1);
});

test('send error fails once and clears queued audio', () => {
  const socket = new Socket();
  const failures: string[] = [];
  const writer = new ModelAudioWriter(
    socket,
    () => assert.fail('Not sent'),
    code => failures.push(code)
  );
  writer.write(Buffer.alloc(960));
  writer.write(Buffer.alloc(960));
  socket.flush(new Error('offline'));
  writer.write(Buffer.alloc(960));
  assert.deepEqual(failures, ['model_audio_send']);
  assert.equal(socket.frames.length, 1);
});

test('shutdown ignores late callbacks without inventing sent bytes', () => {
  const socket = new Socket();
  let written = 0;
  const writer = new ModelAudioWriter(
    socket,
    bytes => {
      written += bytes;
    },
    () => assert.fail('Stopped')
  );
  writer.write(Buffer.alloc(960));
  writer.close();
  socket.flush();
  assert.equal(written, 0);
});

test('stalled send has a finite deadline', async () => {
  const socket = new Socket();
  const failures: string[] = [];
  const writer = new ModelAudioWriter(
    socket,
    () => {},
    code => failures.push(code),
    48000,
    10
  );
  writer.write(Buffer.alloc(960));
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.deepEqual(failures, ['model_audio_send_timeout']);
  writer.close();
});
