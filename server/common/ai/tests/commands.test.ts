import assert from 'node:assert/strict';
import test from 'node:test';
import WebSocket from 'ws';
import { Commands } from '../src/commands.js';

function fixture(timeout = 10000) {
  const reports: Record<string, any>[] = [], events: Record<string, any>[] = [];
  let callback: ((error?: Error) => void) | undefined;
  const model = {readyState: WebSocket.OPEN, send(data: string, done: (error?: Error) => void) {
    events.push(JSON.parse(data)); callback = done;
  }} as WebSocket;
  const commands = new Commands('a', event => reports.push(event), timeout);
  const send = (n = 1, content = 'Private operator text') => commands.send({conversationId:'a',id:n.toString(16).padStart(32,'0'),content},model,true);
  const ack = (n = 1) => ({type:'session.instructions.appended',client_event_id:`operator_${n.toString(16).padStart(32,'0')}`});
  return {commands, reports, events, send, ack, written: (error?: Error) => callback?.(error), model};
}

test('instruction ID correlation, early receipt, duplicate and rejection are truthful', () => {
  const f = fixture();
  try {
    const content = 'hello '.repeat(100);
    f.send(1, content); assert.equal(f.events[0]!.type,'session.instructions.append');
    assert.equal(f.events[0]!.content, content);
    assert(Buffer.byteLength(content, 'utf8') > 500);
    assert.equal(f.events[0]!.delegation_id,null);
    assert.equal(f.events[0]!.event_id,f.ack().client_event_id);
    assert.equal(f.commands.observe(f.ack(2)),false); assert.equal(f.reports.length,0);
    f.commands.observe(f.ack()); f.written(); f.commands.observe(f.ack()); f.send();
    assert.deepEqual(f.reports.map(e=>e.commandStatus),['injected']);
    assert.equal(f.events.length,1);
    f.send(2); f.written();
    f.commands.observe({type:'error',error:{client_event_id:f.ack(2).client_event_id,message:'Private operator text'}});
    assert.equal(f.reports.at(-1)!.commandStatus,'rejected');
    assert(!JSON.stringify(f.reports).includes('Private operator text'));
  } finally { f.commands.close(); }
});

test('timeout, send error, close and late receipts never imply injection or resend', async () => {
  const f = fixture(20);
  f.send(); f.written();
  await new Promise(resolve=>setTimeout(resolve,35));
  assert.equal(f.reports.at(-1)!.commandStatus,'unknown');
  f.commands.observe(f.ack()); assert.equal(f.reports.at(-1)!.commandStatus,'unknown');
  f.send(2); f.written(new Error('private error')); assert.equal(f.reports.at(-1)!.commandReason,'model_write_failed');
  f.send(3); f.commands.close(); assert.equal(f.reports.at(-1)!.commandReason,'conversation_closed');
  const count=f.reports.length; await new Promise(resolve=>setTimeout(resolve,35)); assert.equal(f.reports.length,count);
  assert.equal(f.events.length,3);
});

test('pending work and lifetime IDs are bounded; oversize and wrong sessions do not send', () => {
  const f = fixture();
  try {
    f.send(1,'界'.repeat(4097)); assert.equal(f.reports.at(-1)!.commandReason,'command_too_large');
    f.commands.send({conversationId:'b',id:'f'.repeat(32),content:'wrong'},f.model,true);
    assert.equal(f.events.length,0);
    for(let n=2;n<35;n++) f.send(n);
    assert.equal(f.events.length,32); assert.equal(f.reports.at(-1)!.commandReason,'command_limit');
  } finally { f.commands.close(); }
});

test('Client labels distinguish transport, injection and execution', async () => {
  // Client modules are plain JavaScript and have no build dependency.
  const {commandLabel} = await import(new URL('../../../../client/assets/commands.js',import.meta.url).href);
  const {setLanguage} = await import(new URL('../../../../client/assets/i18n.js',import.meta.url).href);
  setLanguage('zh', false);
  for(const status of ['accepted','written','injected','rejected','unknown','sent_to_model']) {
    const text=commandLabel({id:'test',status}); assert.match(text,/不代表已说出或执行/);
  }
  assert.match(commandLabel({id:'test',status:'written'}),/等待 AI 确认收到/);
  assert.match(commandLabel({id:'test',status:'injected'}),/还不确定是否照做/);
  setLanguage('en', false);
  assert.match(commandLabel({id:'test',status:'injected'}),/Not known if the AI complies.*does not mean that the AI said or did it/);
  setLanguage('zh', false);
});
