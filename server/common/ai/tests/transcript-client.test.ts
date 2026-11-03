import assert from 'node:assert/strict';
import { test } from 'node:test';

class Node {
  textContent=''; className=''; dataset:Record<string,string>={}; children:Node[]=[]; parent:Node|undefined;
  scrollHeight=100; scrollTop=0; clientHeight=100;
  set innerHTML(_value:string){throw Error('HTML rendering is forbidden');}
  append(...nodes:Node[]){for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes:Node[]){this.children=[];this.append(...nodes);}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);}
}
class Socket {
  static all:Socket[]=[];
  onmessage:((event:{data:string})=>void)|undefined; onclose:(()=>void)|undefined;
  closed=false;
  constructor(public url:string){Socket.all.push(this);}
  close(){this.closed=true;}
  packet(value:unknown){this.onmessage?.({data:JSON.stringify(value)});}
}
// 不提供 requestAnimationFrame：窗口被遮住或在后台时浏览器不刷新画面，文字仍要显示且连接不能被关掉。
Object.assign(globalThis,{document:{createElement:()=>new Node(),createTextNode:(text:string)=>Object.assign(new Node(),{textContent:text})}});
const moduleURL=new URL('../../../../client/assets/transcripts.js',import.meta.url).href;
const {setLanguage}=await import(new URL('../../../../client/assets/i18n.js',import.meta.url).href);
setLanguage('zh',false);
const rows=(body:Node)=>body.children.map(row=>[row.className.replace("transcript-row ",""),row.children[1]!.textContent]);

test('actual Client consumer preserves text and ignores stale selected-session sockets', async () => {
  const {TranscriptView}=await import(moduleURL);
  const view=new TranscriptView((path:string)=>path,Socket),root=new Node();
  view.start('one',root);
  const first=Socket.all.at(-1)!;
  const packet={type:'transcript.update',conversationId:'one',cacheId:'cache-one',revision:3,reset:true,state:'active',events:[
    {seq:1,role:'caller',text:' 你好 ',startMs:0,endMs:1},
    {seq:2,role:'ai',text:'<img onerror=alert(1)>',startMs:0,endMs:1},
    {seq:3,role:'caller',text:'你好 ',startMs:1,endMs:2}]};
  first.packet(packet);
  assert.deepEqual(rows(view.body),[['caller','你好 你好'],['ai','<img onerror=alert(1)>']]);
  first.packet({...packet,reset:false});
  assert.deepEqual(rows(view.body),[['caller','你好 你好'],['ai','<img onerror=alert(1)>']],'Replay does not duplicate text');
  view.start('two',root);assert(first.closed);
  first.packet(packet);assert.equal(view.events.size,0,'Stale socket is ignored');
  const second=Socket.all.at(-1)!;
  second.packet({...packet,conversationId:'two',cacheId:'cache-two',state:'ended',gap:true});
  assert(view.ended);assert.match(view.status.textContent,/通话已结束/);assert.match(view.status.textContent,/有部分文字没收到/);
  view.stop();
});

test('a fast stream without display frames neither closes nor reconnects the socket', async () => {
  const {TranscriptView}=await import(moduleURL);
  const view=new TranscriptView((path:string)=>path,Socket),root=new Node();
  let ended=0;
  view.start('live',root,()=>ended++);
  const socket=Socket.all.at(-1)!,before=Socket.all.length;
  for(let seq=1;seq<=40;seq++) socket.packet({type:'transcript.update',conversationId:'live',cacheId:'c',revision:seq,reset:seq===1,state:'active',
    events:[{seq,role:'ai',text:'字',startMs:seq*200,endMs:seq*200+200}]});
  assert.equal(socket.closed,false);assert.equal(Socket.all.length,before);assert.equal(view.reconnects,0);
  assert.doesNotMatch(view.status.textContent,/断开|重连/);
  assert.deepEqual(rows(view.body),[['ai','字'.repeat(40)]]);
  // 真正断开时重连；结束包之后服务端关闭连接，不再重连，只通知一次。
  socket.onclose!();assert.match(view.status.textContent,/正在重连/);
  await new Promise(resolve=>setTimeout(resolve,300));
  const again=Socket.all.at(-1)!;assert.notEqual(again,socket);assert.match(again.url,/after=40&cacheId=c/);
  again.packet({type:'transcript.update',conversationId:'live',cacheId:'c',revision:40,reset:false,state:'ended',events:[]});
  again.onclose!();
  await new Promise(resolve=>setTimeout(resolve,300));
  assert.equal(Socket.all.at(-1),again);assert.equal(ended,1);assert.doesNotMatch(view.status.textContent,/重连/);
  view.stop();
});

test('fragments merge into sentences per speaker and interleave by time', async () => {
  const {turns}=await import(moduleURL);
  // 真实网关存盘片段（会话 062e2c86），每个片段都有自己的 eventId。
  const raw:[string,number,string][]=[['ai',6400,'。'],['caller',10000,' 收到了'],['caller',10400,',谢谢'],['ai',10400,'不客'],['caller',10600,'你'],
    ['ai',10600,'气,'],['ai',10800,'那我'],['ai',11000,'先不'],['ai',11200,'打扰了。'],['caller',17200,' 再'],['caller',17400,'见']];
  const events=raw.map(([role,start,text],index)=>({seq:index+1,role,eventId:`event_${index}`,text,startMs:start,endMs:start+200}));
  assert.deepEqual(turns(events).map((t:{role:string,text:string})=>[t.role,t.text]),
    [['ai','。'],['caller','收到了,谢谢你'],['ai','不客气,那我先不打扰了。'],['caller','再见']]);
  // 对方在停顿中说完一句，算换人说话；同一人停顿过长也分句。
  const reply=[{seq:1,role:'ai',text:'喂',startMs:0,endMs:200},{seq:2,role:'caller',text:'你好',startMs:300,endMs:600},{seq:3,role:'ai',text:'请问',startMs:700,endMs:900},
    {seq:4,role:'ai',text:'再说',startMs:5000,endMs:5200}];
  assert.deepEqual(turns(reply).map((t:{text:string})=>t.text),['喂','你好','请问','再说']);
  // 没有时间的旧数据只合并相邻的同一说话人。
  assert.deepEqual(turns([{role:'ai',text:'a'},{role:'ai',text:'b'},{role:'caller',text:'c'},{role:'ai',text:'d'}]).map((t:{text:string})=>t.text),['ab','c','d']);
});
