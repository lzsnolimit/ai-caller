// 对话文字：实时（/transcript/events）和存盘（/transcript）共用同一套断句与显示。
// 只显示文字，不会开始监听声音。
import { msg, show } from './i18n.js';

const ACTIVE = ['dialing', 'answering', 'starting', 'active', 'ending', 'cleanup_failed'];
// 同一说话人两个片段之间停顿不超过这个值，算同一句。
export const PAUSE_MS = 1500;
const MAX_EVENTS = 512, MAX_CHARS = 131072;

const valid = event => event && ['caller', 'ai'].includes(event.role) && typeof event.text === 'string';
const finite = value => Number.isFinite(value);

/**
 * 把逐字到达的片段合并成句子（一个说话人的一段话），按开始时间交错排列双方。
 * Server 的每个片段都有自己的 eventId（多为一两个字），不能按 eventId 合并；按说话人和停顿合并。
 * 对方在本句停顿期间说完了一段话，才算本句结束；双方同时说话（插话）不打断本句。
 */
export function turns(events) {
  const list = (Array.isArray(events) ? events : []).filter(valid).map((event, index) => ({event, index}));
  list.sort((a, b) => (finite(a.event.startMs) && finite(b.event.startMs) ? a.event.startMs - b.event.startMs : 0) || a.index - b.index);
  const result = [], open = {caller: null, ai: null}, last = {caller: null, ai: null};
  for (const {event} of list) {
    const role = event.role, other = role === 'ai' ? 'caller' : 'ai', turn = open[role], start = event.startMs, end = event.endMs;
    let join = false;
    if (turn && finite(start) && finite(turn.endMs)) {
      const heard = last[other];
      const otherSpokeInGap = heard && finite(heard.startMs) && finite(heard.endMs) && heard.startMs >= turn.endMs && heard.endMs <= start;
      join = start - turn.endMs <= PAUSE_MS && !otherSpokeInGap;
    } else if (turn && !finite(start)) join = result.at(-1) === turn;  // 没有时间的旧数据：只合并相邻的同一说话人
    if (join) {
      turn.text += event.text;
      if (finite(end)) turn.endMs = Math.max(turn.endMs ?? end, end);
    } else {
      const next = {role, startMs: finite(start) ? start : null, endMs: finite(end) ? end : null, text: event.text};
      result.push(next); open[role] = next;
    }
    last[role] = event;
  }
  for (const turn of result) turn.text = turn.text.trim();
  return result.filter(turn => turn.text);
}

const clock = ms => { const s = Math.floor(ms / 1000); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`; };
// 说话人随语言切换；时间不翻译。
export function turnLabel(turn) {
  return msg(finite(turn.startMs) ? 'transcript.turnAt' : 'transcript.turn', {speaker: msg(turn.role === 'caller' ? 'speaker.caller' : 'speaker.ai'), time: finite(turn.startMs) ? clock(turn.startMs) : ''});
}

/** 用 textContent 写入，不解析 HTML。 */
export function renderTurns(body, events) {
  const rows = turns(events).map(turn => {
    const row = document.createElement('div'), label = document.createElement('small'), text = document.createElement('span');
    row.className = `transcript-row ${turn.role}`; text.className = 'transcript-text';
    show(label, turnLabel(turn)); text.textContent = turn.text;
    row.append(label, text); return row;
  });
  body.replaceChildren(...rows);
  return rows.length;
}

/**
 * 当前选中通话的实时文字。收到就显示，不等浏览器刷新画面：
 * 窗口被遮住或在后台时浏览器会暂停画面刷新，等待刷新会让连接被误判为堵塞而反复重连。
 */
export class TranscriptView {
  constructor(url, Socket = WebSocket) { this.url = url; this.Socket = Socket; this.epoch = 0; this.reconnects = 0; }
  stop() { this.epoch++; clearTimeout(this.timer); this.socket?.close(); this.socket = null; }
  start(key, root, onEnded) {
    this.stop(); this.key = key; this.root = root; this.onEnded = onEnded; this.seq = 0; this.cacheId = ''; this.ended = false; this.gap = false;
    this.events = new Map(); this.chars = 0; this.reconnects = 0;
    this.status = show(document.createElement('p'), msg('transcript.connecting')); this.status.className = 'status';
    this.body = document.createElement('div'); this.body.className = 'transcript-body'; root.replaceChildren(this.status, this.body);
    this.connect(this.epoch);
  }
  connect(epoch, delay = 250) {
    if (epoch !== this.epoch) return;
    const path = `/v1/conversations/${encodeURIComponent(this.key)}/transcript/events?after=${this.seq}&cacheId=${encodeURIComponent(this.cacheId)}`;
    const socket = this.socket = new this.Socket(this.url(path));
    socket.onmessage = event => {
      if (epoch !== this.epoch || socket !== this.socket) return;
      let packet; try { packet = JSON.parse(event.data); } catch { return; }
      if (packet.type !== 'transcript.update' || packet.conversationId !== this.key || !Array.isArray(packet.events)) return;
      delay = 250; this.apply(packet);
    };
    socket.onclose = () => {
      if (epoch !== this.epoch || socket !== this.socket || this.ended) return;
      this.reconnects++;
      show(this.status, msg('transcript.reconnecting'));
      this.timer = setTimeout(() => this.connect(epoch, Math.min(delay * 2, 5000)), delay);
    };
  }
  apply(packet) {
    const nearBottom = this.body.scrollHeight - this.body.scrollTop - this.body.clientHeight < 60;
    if (packet.reset || packet.cacheId !== this.cacheId) { this.events.clear(); this.chars = 0; this.seq = 0; this.gap = false; }
    this.cacheId = packet.cacheId; this.gap ||= Boolean(packet.gap || packet.dropped > 0 || packet.invalid > 0);
    for (const event of packet.events) {
      if (!Number.isSafeInteger(event?.seq) || event.seq <= this.seq || !valid(event)) continue;
      if (this.seq && event.seq !== this.seq + 1) this.gap = true;
      this.seq = event.seq; this.gap ||= Boolean(event.gapBefore);
      this.events.set(event.seq, event); this.chars += event.text.length;
      while (this.events.size > MAX_EVENTS || this.chars > MAX_CHARS) { const [first, old] = this.events.entries().next().value; this.events.delete(first); this.chars -= old.text.length; this.gap = true; }
    }
    this.seq = Math.max(this.seq, packet.revision || 0);
    renderTurns(this.body, [...this.events.values()]);
    this.ended = !ACTIVE.includes(packet.state);
    show(this.status, msg(this.ended ? 'transcript.ended' : 'transcript.live', {gap: this.gap ? msg('transcript.gap') : ''}));
    if (nearBottom) this.body.scrollTop = this.body.scrollHeight;
    if (this.ended) { const done = this.onEnded; this.onEnded = null; done?.(); }
  }
}
