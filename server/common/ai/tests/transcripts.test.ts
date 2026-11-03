import assert from 'node:assert/strict';
import { test } from 'node:test';
import { TranscriptWriter } from '../src/transcripts.js';

test('text is exact, role independent, event deduplicated, bounded and failure isolated', () => {
  const frames: any[] = [];
  const sink = {writableLength:0,destroyed:false,write:(frame:string)=>{frames.push(JSON.parse(frame));return true;},end:()=>sink,destroy:()=>sink};
  const writer = new TranscriptWriter(sink);
  const event = {type:'session.input_transcript.delta',event_id:'a',delta:' Hello hello  世界!\n\t🙂 <img onerror=x> ',start_ms:0,end_ms:100};
  writer.append(event);writer.append(event);
  writer.append({...event,event_id:'b'});
  writer.append({...event,type:'session.output_transcript.delta'});
  assert.equal(frames.length,3);assert.equal(frames[0].text,event.delta);assert.equal(frames[2].role,'ai');
  sink.writableLength=65536;
  for(let i=0;i<10000;i++)writer.append({...event,event_id:`slow-${i}`});
  assert.equal(frames.length,3);
  writer.append({...event,delta:3});
  sink.writableLength=0;writer.status();
  assert.equal(frames[3].dropped,10000);assert.equal(frames[3].invalid,1);
  writer.close();
});
