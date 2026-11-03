import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { test } from 'node:test';
import WebSocket from 'ws';
import { createAISession } from '../src/session.js';

class Audio extends EventEmitter {
  queuedBytes = 0;
  output: Buffer[] = [];
  closed = false;
  send(pcm: Buffer): void { this.output.push(pcm); }
  async close(): Promise<void> { this.closed = true; }
}
class Model extends EventEmitter {
  readyState = WebSocket.OPEN;
  events: Record<string, any>[] = [];
  holdInput = false;
  send(data: string, callback?: (error?: Error) => void): void {
    const event = JSON.parse(data); this.events.push(event);
    if (!(this.holdInput && event.type === "session.input_audio.append")) callback?.();
    if (event.type === 'session.close') queueMicrotask(() => this.emit('message', Buffer.from(JSON.stringify({type:'session.closed',reason:'completed'}))));
  }
  close(): void { this.emit('close'); }
  terminate(): void { this.emit('close'); }
  event(value: unknown): void { this.emit('message', Buffer.from(JSON.stringify(value))); }
}

test('common AI supports independent hosts without CLI, phones or gateway APIs', async () => {
  const hosts = ['a', 'b'].map(id => {
    const text: Record<string, unknown>[] = [];
    const audio = new Audio(), model = new Model(), reports: Record<string, any>[] = [];
    const session = createAISession(audio, {key:'fixture',liveUrl:'wss://unused.invalid',conversationId:id,
      onTranscript:event=>text.push(event), managed:true,waitForAnswer:true, instructions:'fixture instructions',report:value => reports.push(value),
      onClosing:()=>{},onFailure:()=>assert.fail('unexpected failure'),connect:()=>model as unknown as WebSocket});
    audio.emit('ready'); model.emit('open');
    audio.emit('pcm',Buffer.alloc(960));
    assert(!model.events.some(event=>event.type==='session.input_audio.append'));
    model.event({type:'session.started'}); audio.emit('callState','active');
    return {audio,model,session,reports,id,text};
  });
  for (const host of hosts) {
    host.model.event({type:'session.input_transcript.delta',delta:' 你好 你好 ',event_id:'same',start_ms:0,end_ms:100});
    host.model.event({type:'session.output_transcript.delta',delta:' <img onerror=x> ',event_id:'same',start_ms:0,end_ms:100});
    host.model.event({type:'session.input_transcript.delta',delta:42});
    assert.equal(host.text.length,3);
    const pcm=Buffer.alloc(960,host.id.charCodeAt(0));
    host.audio.emit('pcm',pcm);
    host.model.event({type:'session.output_audio.delta',delta:pcm.toString('base64')});
    assert.deepEqual(host.audio.output,[pcm]);
    assert.equal(host.model.events.filter(event=>event.type==='session.input_audio.append').length,1);
    host.session.command({conversationId:host.id,id:'a'.repeat(32),content:'fixture command'});
    assert(host.reports.some(event=>event.commandId==='a'.repeat(32) && event.commandStatus==='written'));
    assert.equal(host.model.events.filter(event=>event.type==='session.instructions.append').length,1);
    host.model.event({type:'error',error:{client_event_id:'operator_'+'a'.repeat(32),message:'private command',code:'invalid_request_error'}});
    assert(host.reports.some(event=>event.commandStatus==='rejected'));
    host.model.holdInput = true;
    host.audio.emit('pcm',pcm);
    host.model.event({type:'session.output_audio.delta',delta:pcm.toString('base64')});
    assert.equal(host.audio.output.length,2,'Output must continue while an input send is pending');
    host.audio.emit('callState','unknown');
    assert(!host.audio.closed);
  }
  hosts[0]!.session.close(); await new Promise(resolve=>setImmediate(resolve));
  assert(hosts[0]!.audio.closed); assert(!hosts[1]!.audio.closed);
  hosts[1]!.session.close(); await new Promise(resolve=>setImmediate(resolve));
  assert(hosts.every(host=>host.reports.some(event=>event.sessionClosed)));
});
