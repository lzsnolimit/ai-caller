import assert from 'node:assert/strict';
import { test } from 'node:test';
import { PrivateAudioWriter, type BinarySink } from '../src/private-audio.js';

class Sink implements BinarySink {
  frames: Buffer[] = [];
  callbacks: ((error?: Error | null) => void)[] = [];
  write(frame: Buffer, callback: (error?: Error | null) => void): void {
    this.frames.push(frame); this.callbacks.push(callback);
  }
  flush(error?: Error): void { this.callbacks.shift()!(error); }
}

test('private audio preserves bursts in a bounded FIFO, with one write in flight', () => {
  const sink = new Sink(), failures: string[] = [];
  const writer = new PrivateAudioWriter(sink, code => failures.push(code));
  for (let index=0; index<30; index++) writer.write(Buffer.alloc(960,index));
  assert.equal(sink.frames.length,1);
  assert.equal(writer.queuedBytes,28800);
  for (let index=0; index<30; index++) sink.flush();
  assert.deepEqual(sink.frames.map(frame=>frame[5]),Array.from({length:30},(_,index)=>index));
  assert(sink.frames.every(frame=>frame.readUInt32BE()===961 && frame[4]===0));
  assert.equal(writer.queuedBytes,0); assert.deepEqual(failures,[]);
  writer.close();
});

test('private audio fails sustained overload once and ignores late callbacks', () => {
  const sink = new Sink(), failures: string[] = [];
  const writer = new PrivateAudioWriter(sink, code=>failures.push(code));
  for(let index=0;index<51;index++) writer.write(Buffer.alloc(960));
  assert.deepEqual(failures,['private_audio_queue_full']);
  assert.equal(writer.peakQueuedBytes,48000);
  assert.equal(writer.queuedBytes,0);
  sink.flush(); writer.write(Buffer.alloc(960));
  assert.equal(sink.frames.length,1);
});

test('private audio has a stalled-send deadline and discards only after failure', async () => {
  const sink = new Sink(), failures: string[] = [];
  const writer = new PrivateAudioWriter(sink, code=>failures.push(code),48000,10);
  writer.write(Buffer.alloc(960));
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.deepEqual(failures,['private_audio_send_timeout']);
  sink.flush(); assert.equal(writer.queuedBytes,0);
});

test('private audio sends a maximum model chunk as short ordered frames', () => {
  const sink = new Sink();
  const writer = new PrivateAudioWriter(sink, ()=>assert.fail('unexpected failure'));
  const pcm=Buffer.alloc(48000,7);
  writer.write(pcm);
  for(let index=0;index<50;index++) sink.flush();
  assert.deepEqual(Buffer.concat(sink.frames.map(frame=>frame.subarray(5))),pcm);
  writer.close();
});

test('private audio write errors and explicit close cannot restart pending frames', () => {
  for (const failure of [false,true]) {
    const sink = new Sink(), failures: string[] = [];
    let completed=0;
    const writer = new PrivateAudioWriter(sink, code=>failures.push(code),48000,2000,()=>{completed++;});
    writer.write(Buffer.alloc(1920));
    if (failure) sink.flush(new Error('offline'));
    else { writer.close(); sink.flush(); }
    assert.equal(completed,0);
    assert.equal(sink.frames.length,1);
    assert.equal(writer.queuedBytes,0);
    assert.deepEqual(failures,failure ? ['private_audio_send'] : []);
  }
});
