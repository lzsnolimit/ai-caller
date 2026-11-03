// AI 电话助理前端。所有状态来自 Server 的 /v1/console 快照和 console/events 推送；页面只负责显示和发请求。
import { TranscriptView, renderTurns } from './transcripts.js';
import { DeviceVoice } from './device-voice.js';
import { commandLabel } from './commands.js';
import { directionLabel, transcriptEvents, inboundPromptTag, InboundPrompt } from './inbound.js';
import { errorMessage, serverError, isActive, outcomeText, outcomeIsBad, recoveryText, stateLabel, deviceStatus, duration, callSeconds, whenText, clockText } from './labels.js';
import { has, language, localError, msg, onLanguage, setLanguage, show, t, translate } from './i18n.js';

const $ = id => document.getElementById(id);
let origin = location.origin, events, state, pairingId, selected, generation = 0, reconnectTimer;
const drafts = new Map();
let uploadSource = 'editor';
let voiceCatalog;
const voicePreview = {controller: null, url: null, epoch: 0};
let devicesVersion = '', callsVersion = '';
let commandRevision = -1;
let detailLive = false;  // 当前详情是按进行中的通话搭建的；结束后要换成结束视图
let currentPage = 'devices';
let lastCommands = [];

// ?theme=dark|light 只用于预览；默认跟随系统。
const theme = new URLSearchParams(location.search).get('theme');
if (theme === 'dark' || theme === 'light') document.documentElement.dataset.theme = theme;

// 文字参数可以是 msg() 文案引用（随语言切换）或原样显示的字符串（用户内容、设备名、地址）。
function notice(text = '') { show($('notice'), text); }
function element(tag, text, cls) { const node = document.createElement(tag); if (text !== undefined && text !== null) show(node, text); if (cls) node.className = cls; return node; }
function button(text, action, cls) { const node = element('button', text, cls); node.type = 'button'; node.onclick = () => run(action, node); return node; }
async function run(action, node) { if (node) node.disabled = true; try { notice(); await action(); } catch (error) { notice(error); } finally { if (node) node.disabled = false; updateDialGate(); } }
async function api(path, method = 'GET', body, server = origin) {
  const response = await fetch(server + path, {method, headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body), cache: 'no-store', signal: AbortSignal.timeout(90000)});
  let result; try { result = await response.json(); } catch { throw localError('error.badResponse', {status: response.status}); }
  if (!response.ok) throw serverError(result.error?.code); return result;
}
function wsURL(path) { return origin.replace(/^http/, 'ws') + path; }
// 手机名称优先；没有名称（Mac 首次连接前、旧快照没有 name 字段）时显示“未知设备”，地址只作小字。
const UNKNOWN = msg('device.unknown');
const nameOf = device => (device?.name && String(device.name).trim()) || '';
const deviceTitle = device => nameOf(device) || UNKNOWN;
const deviceName = address => deviceTitle(state?.devices?.find(d => d.address === address));
const isNamed = address => Boolean(nameOf(state?.devices?.find(d => d.address === address)));
// 名称 + 小字地址（无名称时）。用于线路区、通话记录。
function whoNode(address, cls = '') {
  const node = element('span', '', cls); node.append(element('span', deviceName(address)));
  if (!isNamed(address) && address) node.append(document.createTextNode(' '), element('span', address, 'mono addr'));
  return node;
}

// ---------- 线路 ----------
// 优先用快照里的 line；旧服务没有 line 时按会话列表推断，只用于显示。
function lineView() {
  const line = state?.line, conversations = state?.conversations || [];
  if (line && typeof line === 'object') return {known: true, state: line.state, conversationId: line.conversationId ?? null, address: line.address ?? null, reason: line.reason ?? null};
  const current = [...conversations].reverse().find(isActive);
  if (!current) return {known: false, state: 'idle', conversationId: null, address: null, reason: null};
  return {known: false, state: current.state === 'cleanup_failed' ? 'recovering' : 'busy', conversationId: current.id, address: current.address, reason: current.state === 'cleanup_failed' ? 'cleanup_failed' : null};
}
function currentConversation(line) {
  const conversations = state?.conversations || [];
  return conversations.find(c => c.id === line.conversationId) || (line.state === 'busy' ? [...conversations].reverse().find(isActive) : null) || null;
}
function renderLine() {
  const line = lineView(), devices = state?.devices || [], current = currentConversation(line);
  $('meta-paired').textContent = devices.filter(d => d.paired).length;
  $('meta-ready').textContent = devices.filter(d => d.connected).length;
  const actions = $('line-actions'); actions.replaceChildren();
  let lamp = 'unknown', title = msg('line.unknown', {state: line.state}), sub = '', timer = '';
  if (line.state === 'idle') {
    lamp = 'idle'; title = msg('line.idle');
    sub = msg(devices.some(d => d.hfp) ? 'line.idleReady' : devices.some(d => d.connected) ? 'line.idleNoHfp' : 'line.idleNoPhone');
  } else if (line.state === 'busy') {
    lamp = 'busy';
    title = msg('line.busyTitle', {direction: directionLabel(current), phase: current ? stateLabel(current.state) : msg('state.active')});
    const address = line.address || current?.address;
    // 时长只从接通算起；拨号、响铃阶段不计时，与记录、详情一致。
    const answered = Number.isFinite(current?.answeredAt);
    const subNode = show($('line-sub'), ''); subNode.append(whoNode(address));
    if (current?.callStatus?.state === 'unknown') subNode.append(element('span', msg('line.phoneUnverified')));
    if (current && !answered) subNode.append(element('span', msg('line.timerPending'))); else if (answered) subNode.append(element('span', msg('line.timerRunning')));
    timer = current ? duration(callSeconds(current)) : '';
    if (current) {
      actions.append(button(msg('line.view'), () => { page('calls'); return choose(current.id); }, 'primary'));
      actions.append(button(msg('common.hangup'), async () => { if (!confirm(t('line.confirmHangup'))) return; await api(`/v1/conversations/${current.id}/end`, 'POST'); }));
    }
    $('lamp').dataset.state = lamp; show($('line-title'), title); $('line-timer').textContent = timer;
    return;
  } else if (line.state === 'recovering') {
    lamp = 'recovering'; title = msg('line.recovering');
    sub = msg('line.recoveringSub', {reason: recoveryText(line.reason)});
  }
  $('lamp').dataset.state = lamp; show($('line-title'), title); show($('line-sub'), sub); $('line-timer').textContent = timer;
}
// 每秒刷新计时：主视觉、当前通话一行、详情头。
setInterval(() => {
  if (!state) return;
  const line = lineView(), current = currentConversation(line);
  if (line.state === 'busy' && current) $('line-timer').textContent = duration(callSeconds(current));
  for (const node of document.querySelectorAll('[data-live-duration]')) {
    const item = state.conversations?.find(c => c.id === node.dataset.liveDuration);
    if (item) node.textContent = duration(callSeconds(item));
  }
}, 1000);

// ---------- 拨号门禁 ----------
function updateDialGate() {
  const submit = $('dial-submit'), gate = $('dial-gate'); if (!submit || !gate) return;
  if (!state) { show(gate, ''); return; }
  const line = lineView(), device = state.devices?.find(d => d.address === $('dial-device').value); let reason = '';
  if (line.known && line.state === 'busy') reason = msg('gate.busy');
  else if (line.known && line.state === 'recovering') reason = msg('gate.recovering', {reason: recoveryText(line.reason)});
  else if (line.known && line.state !== 'idle') reason = msg('gate.unknown', {state: line.state});
  // 待核对的手机不禁用拨号：拨号会触发服务端核对，已结束则自动解除，否则返回 device_recovering。
  const hint = !reason && device?.recovering ? msg('gate.deviceRecovering') : '';
  submit.disabled = Boolean(reason); show(gate, reason || hint);
}

// ---------- 音色 ----------
function stopVoicePreview() { voicePreview.epoch++; voicePreview.controller?.abort(); voicePreview.controller = null; $('voice-player').pause(); $('voice-player').removeAttribute('src'); $('voice-player').hidden = true; if (voicePreview.url) URL.revokeObjectURL(voicePreview.url); voicePreview.url = null; show($('voice-preview-status'), ''); }
function describeVoice() {
  const voice = voiceCatalog?.voices.find(v => v.id === $('dial-voice').value);
  if (!voice) { show($('voice-info'), msg('voice.noInfo')); return; }
  const style = [voice.language, voice.regionalInfluence].filter(Boolean).join(' · ');
  const source = msg(voice.sample?.state === 'bundled' ? 'voice.bundled' : 'voice.generated');
  show($('voice-info'), msg('voice.info', {accent: style ? msg('voice.accent', {style}) : '', source}));
}
// 音色名来自服务端，不翻译；只有“默认”标记随语言切换。
const voiceText = voice => voice.default ? t('voice.default', {name: voice.displayName}) : voice.displayName;
function labelVoices() { for (const option of $('dial-voice').options) { const voice = voiceCatalog?.voices.find(v => v.id === option.value); if (voice) option.text = voiceText(voice); } }
const defaultVoiceOption = () => { const option = new Option(t('voice.default', {name: 'Marin'}), 'marin'); show(option, msg('voice.default', {name: 'Marin'})); return option; };
async function loadVoices() { const gen = generation; const catalog = await api('/v1/voices'); if (gen !== generation) return; voiceCatalog = catalog; $('dial-voice').replaceChildren(...catalog.voices.map(v => new Option(voiceText(v), v.id))); $('dial-voice').value = catalog.defaultVoice; describeVoice(); syncDeviceVoice(true); }
async function previewVoice() {
  stopVoicePreview(); const epoch = voicePreview.epoch; const controller = voicePreview.controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 25000);
  show($('voice-preview-status'), msg(voiceCatalog?.voices.find(v => v.id === $('dial-voice').value)?.sample?.state === 'bundled' ? 'preview.loading' : 'preview.generating'));
  try {
    const response = await fetch(origin + `/v1/voices/${encodeURIComponent($('dial-voice').value)}/preview`, {method: 'POST', signal: controller.signal, cache: 'no-store'});
    if (!response.ok) { const body = await response.json(); throw serverError(body.error?.code); }
    const blob = await response.blob(); if (epoch !== voicePreview.epoch) return;
    voicePreview.url = URL.createObjectURL(blob); $('voice-player').src = voicePreview.url; $('voice-player').hidden = false;
    try { await $('voice-player').play(); } catch { if (epoch === voicePreview.epoch) show($('voice-preview-status'), msg('preview.ready')); }
    if (epoch === voicePreview.epoch && !$('voice-player').paused) show($('voice-preview-status'), msg('preview.playing'));
  } catch (error) { if (epoch === voicePreview.epoch) show($('voice-preview-status'), error.name === 'AbortError' ? msg('preview.stopped') : error); } finally { clearTimeout(timer); }
}
const preferenceStatus = element('p', '', 'status'); preferenceStatus.id = 'device-voice-status'; preferenceStatus.setAttribute('role', 'status'); $('voice-preview-status').before(preferenceStatus);
const deviceVoice = new DeviceVoice((server, path, method, body) => api(path, method, body, server), value => {
  $('dial-voice').disabled = !value.ready; $('voice-preview').disabled = !value.ready;
  if (value.voice) { $('dial-voice').value = value.voice; describeVoice(); } show(preferenceStatus, value.status);
});
function syncDeviceVoice(force = false) {
  if (!voiceCatalog) return; const address = $('dial-device').value, current = deviceVoice.state;
  const saved = state.devices?.find(device => device.address === address)?.voice;
  if (force || current.server !== origin || current.address !== address || (!current.saving && current.ready && saved && saved !== current.voice)) { stopVoicePreview(); void deviceVoice.load(origin, address); }
}

// ---------- 来电接待语 ----------
const inboundPrompt = new InboundPrompt((server, path, method, body) => api(path, method, body, server), value => {
  show($('inbound-title'), value.address ? msg('inbound.titleFor', {name: deviceName(value.address)}) : msg('inbound.title'));
  if (value.ready && !value.saving && $('inbound-content').dataset.address !== value.address + '|' + value.server) { $('inbound-content').value = value.content; $('inbound-content').dataset.address = value.address + '|' + value.server; }
  if (!value.ready) $('inbound-content').dataset.address = '';
  $('inbound-content').disabled = !value.ready || value.saving; $('inbound-save').disabled = !value.ready || value.saving;
  $('inbound-guard').textContent = value.guard || '—'; show($('inbound-status'), value.status);
});
function openInboundPrompt(address) { $('pair-panel').hidden = true; $('inbound-panel').hidden = false; $('inbound-content').value = ''; $('inbound-content').dataset.address = ''; void inboundPrompt.load(origin, address); $('inbound-panel').scrollIntoView({block: 'nearest'}); }

// ---------- 分页 ----------
function page(name) {
  currentPage = name;
  for (const node of document.querySelectorAll('.page')) node.hidden = node.id !== name;
  for (const node of document.querySelectorAll('[data-page]')) node.setAttribute('aria-selected', node.dataset.page === name ? 'true' : 'false');
  const [hashPage] = location.hash.replace(/^#/, '').split('/');
  const hash = name === 'calls' && selected ? `#calls/${selected}` : `#${name}`;
  if (hashPage !== name || (name === 'calls' && selected)) history.replaceState(null, '', hash);
}
for (const node of document.querySelectorAll('[data-page]')) node.onclick = () => page(node.dataset.page);
// 地址栏 #calls/<id>、#devices/pair、#devices/inbound/<address> 可以直接打开对应内容。
function applyHash() {
  const [name, sub, extra] = location.hash.replace(/^#/, '').split('/');
  if (['devices', 'calls', 'prompt'].includes(name)) page(name); else page('devices');
  if (name === 'calls' && sub) void choose(sub).catch(error => notice(error.message));
  if (name === 'devices' && sub === 'pair') { $('inbound-panel').hidden = true; $('pair-panel').hidden = false; }
  if (name === 'devices' && sub === 'inbound' && extra) openInboundPrompt(decodeURIComponent(extra));
}

// ---------- 监听 ----------
const monitor = {socket: null, context: null, nodes: new Set(), next: [0, 0], key: null, counts: [0, 0], gain: [1, 1], epoch: 0,
  stop() { this.epoch++; this.socket?.close(); this.socket = null; for (const node of this.nodes) { try { node.stop(); } catch {} } this.nodes.clear(); this.next = [0, 0]; this.key = null; this.counts = [0, 0]; const status = $('monitor-status'); if (status) show(status, msg('monitor.idle')); },
  async start(key) { this.stop(); const epoch = this.epoch; this.context ||= new AudioContext({sampleRate: 24000}); await this.context.resume(); if (epoch !== this.epoch) return; this.key = key; this.socket = new WebSocket(wsURL(`/v1/conversations/${key}/monitor`)); this.socket.binaryType = 'arraybuffer';
    this.socket.onmessage = event => { if (epoch !== this.epoch || this.key !== key) return; const bytes = new Uint8Array(event.data); const channel = bytes[0]; if (channel > 1 || bytes.length < 3 || (bytes.length - 1) % 2) return; this.counts[channel] += bytes.length - 1; const view = new DataView(event.data); const samples = (bytes.length - 1) / 2; const context = this.context;
      if (this.next[channel] - context.currentTime > .15) return;
      const buffer = context.createBuffer(1, samples, 24000); const data = buffer.getChannelData(0); for (let i = 0; i < samples; i++) data[i] = view.getInt16(1 + i * 2, true) / 32768;
      const source = context.createBufferSource(), gain = context.createGain(); source.buffer = buffer; gain.gain.value = this.gain[channel]; source.connect(gain).connect(context.destination); this.nodes.add(source); source.onended = () => this.nodes.delete(source); const time = Math.max(context.currentTime + .02, this.next[channel]); source.start(time); this.next[channel] = time + samples / 24000;
      const status = $('monitor-status'); if (status) show(status, msg('monitor.live', {caller: (this.counts[0] / 48000).toFixed(0), ai: (this.counts[1] / 48000).toFixed(0)}));
    };
    this.socket.onclose = () => { if (epoch === this.epoch && $('monitor-status')) show($('monitor-status'), msg('monitor.closed')); };
  }
};

// ---------- 连接服务 ----------
async function attach() {
  const parsed = new URL($('origin').value || location.origin); if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password || parsed.pathname !== '/' || parsed.search || parsed.hash) throw localError('server.badOrigin');
  generation++; events?.close(); monitor.stop(); transcriptView.stop(); stopVoicePreview(); clearTimeout(reconnectTimer); selected = null; origin = parsed.origin; $('detail').replaceChildren(element('p', msg('detail.empty'), 'detail-empty'));
  devicesVersion = ''; callsVersion = ''; voiceCatalog = null; deviceVoice.reset(); inboundPrompt.reset(); $('inbound-panel').hidden = true; $('dial-voice').replaceChildren(defaultVoiceOption());
  show($('health'), msg('health.connecting')); $('health').className = '';
  const gen = generation, snapshot = await api('/v1/console'); if (gen !== generation) return; state = snapshot; render(); setHealth();
  applyHash();
  const prompt = await api('/v1/system-prompt'); if (gen !== generation) return; $('prompt-content').value = prompt.content;
  connectEvents(gen);
  try { await loadVoices(); } catch (error) { voiceCatalog = null; show($('voice-info'), msg('voice.catalogFailed')); throw error; }
}
function setHealth(text) { show($('health'), text || msg('health.ok', {host: origin.replace(/^https?:\/\//, '')})); $('health').className = text ? '' : 'ok'; }
function connectEvents(current, delay = 250) {
  if (current !== generation) return;
  const socket = events = new WebSocket(wsURL('/v1/console/events'));
  socket.onmessage = event => { if (current !== generation || events !== socket) return; state = JSON.parse(event.data); render(); setHealth(); delay = 250; };
  socket.onclose = () => { if (current !== generation || events !== socket) return; setHealth(msg('health.reconnecting')); reconnectTimer = setTimeout(() => connectEvents(current, Math.min(delay * 2, 5000)), delay); };
}

// ---------- 渲染 ----------
function closeMenus(except) { for (const node of document.querySelectorAll('details.menu[open]')) if (node !== except) node.open = false; }
document.addEventListener('click', event => { if (!event.target.closest('details.menu')) closeMenus(); });
function menu(items) {
  const node = element('details', '', 'menu'), summary = element('summary', '⋯'); summary.setAttribute('aria-label', t('common.more')); const sheet = element('div', '', 'sheet');
  node.ontoggle = () => { if (node.open) closeMenus(node); };
  for (const [text, action, cls] of items) sheet.append(button(text, () => { node.open = false; return action(); }, cls));
  node.append(summary, sheet); return node;
}
const optionNote = d => { const notes = [d.hfp ? '' : t(d.connected ? 'device.noteNoHfp' : 'device.noteOffline'), d.recovering ? t('device.noteRecovering') : ''].filter(Boolean); return notes.length ? t('device.notes', {notes: notes.join(t('common.listSep'))}) : ''; };
function renderDevices(devices, line) {
  const list = $('device-list'); list.replaceChildren();
  for (const device of devices) {
    const row = element('div', '', 'jack'), status = deviceStatus(device, line.state === 'busy' ? line.address : null);
    const dot = element('span', '', 'dot'); dot.dataset.tone = status.tone;
    const who = element('div', '', 'who'), name = element('div', deviceTitle(device), 'name' + (nameOf(device) ? '' : ' unknown'));
    who.append(name, element('div', status.text, 'state'), element('div', device.address, 'addr'));
    const tags = element('div', '', 'tags'); const promptTag = inboundPromptTag(device); if (promptTag) tags.append(element('span', promptTag, 'tag'));
    if (device.voice && device.voice !== 'marin') tags.append(element('span', msg('device.voiceTag', {voice: device.voice}), 'tag'));
    if (tags.childElementCount) who.append(tags);
    const ops = element('div', '', 'ops');
    const control = operation => async () => { await api(`/v1/management/${device.address}/${operation}`, 'POST'); state = await api('/v1/console'); render(); };
    const dial = () => { page('calls'); $('dial-device').value = device.address; syncDeviceVoice(true); updateDialGate(); $('dial-form').scrollIntoView({block: 'start'}); };
    const onLine = line.state === 'busy' && line.address === device.address && line.conversationId;
    if (onLine) ops.append(button(msg('device.viewCall'), () => { page('calls'); return choose(line.conversationId); }, 'primary'));
    else if (device.connected) ops.append(button(msg('device.dial'), dial, 'primary'));
    else ops.append(button(msg('device.connect'), control('connect'), 'primary'));
    const extra = [];
    if (device.connected && !onLine) extra.push([msg('device.disconnect'), control('disconnect')]); else if (!device.connected && device.paired) extra.push([msg('device.dial'), dial]);
    extra.push([msg('device.greeting'), () => openInboundPrompt(device.address)]);
    extra.push([msg('device.remove'), async () => { if (!confirm(t('device.confirmRemove', {name: deviceTitle(device), address: device.address}))) return; await control('remove')(); }, 'danger']);
    ops.append(menu(extra));
    row.append(dot, who, ops); list.append(row);
  }
  if (!devices.length) list.append(element('p', msg('devices.empty'), 'empty'));
  const previous = $('dial-device').value; $('dial-device').replaceChildren();
  for (const d of devices.filter(d => d.paired)) $('dial-device').append(new Option(String(deviceTitle(d)) + (nameOf(d) ? '' : ` ${d.address}`) + optionNote(d), d.address));
  if ([...$('dial-device').options].some(o => o.value === previous)) $('dial-device').value = previous;
  else { const ready = devices.find(d => d.paired && d.hfp); if (ready) $('dial-device').value = ready.address; }
}
function callRow(c, current) {
  const node = element('button', '', 'call' + (selected === c.id ? ' selected' : '') + (current ? ' current' : '')); node.type = 'button'; node.onclick = () => run(() => choose(c.id));
  const inbound = c.direction === 'inbound';
  const arrow = element('span', inbound ? '↙' : '↗', 'arrow ' + (inbound ? 'in' : 'out'));
  const main = element('div', '', 'main'), l1 = element('div', '', 'l1'), l2 = element('div', '', 'l2');
  l1.append(element('span', directionLabel(c), 'dir'), whoNode(c.address, 'who'));
  const live = isActive(c), outcome = String(outcomeText(c)) + (live && c.callStatus?.state === 'unknown' ? t('line.phoneUnverified') : '');
  l2.append(element('span', whenText(c.created)), document.createTextNode(' · '), element('span', outcome, live ? 'live' : outcomeIsBad(c) ? 'bad' : ''));
  main.append(l1, l2);
  const dur = element('span', duration(callSeconds(c)), 'dur'); if (live) dur.dataset.liveDuration = c.id;
  node.append(arrow, main, dur); return node;
}
function render() {
  const devices = state.devices || [], conversations = state.conversations || [];
  const line = lineView();
  renderLine();
  const nextDevicesVersion = JSON.stringify([devices, line.state, line.address]);
  if (nextDevicesVersion !== devicesVersion) { devicesVersion = nextDevicesVersion; renderDevices(devices, line); }
  syncDeviceVoice();
  const nextCallsVersion = JSON.stringify([selected, line.conversationId, conversations.map(c => [c.id, c.direction, c.address, c.state, c.ai, c.error, c.endReason, c.endedAt, c.callStatus?.state])]);
  if (nextCallsVersion !== callsVersion) { callsVersion = nextCallsVersion;
    // 当前通话置顶，其余为记录（新到旧）。
    const current = currentConversation(line), history = [...conversations].reverse().filter(c => c !== current);
    const list = $('conversation-list'); list.replaceChildren(element('h4', msg('calls.current')));
    list.append(current ? callRow(current, true) : element('p', msg(line.state === 'recovering' ? 'calls.idleRecovering' : 'calls.idle'), 'empty'));
    list.append(element('h4', msg('calls.records')));
    for (const c of history) list.append(callRow(c, false)); if (!history.length) list.append(element('p', msg('calls.empty'), 'empty'));
  }
  updateDialGate();
  if (selected) { const item = conversations.find(c => c.id === selected); if (item) { if (!isActive(item) && monitor.key === selected) monitor.stop();
    // 通话结束：换成结束视图（结局 + 存盘对话），去掉发送、监听等进行中才有的控件，不再重连实时文字。
    if (detailLive && !isActive(item)) endDetail(selected);
    else { renderDetailHead(item); if (commandRevision !== item.commandRevision) { commandRevision = item.commandRevision; void commands(selected, generation).catch(() => {}); } } } }
  renderPairing();
}
const pairState = value => has('pair.' + value) ? msg('pair.' + value) : value;
function renderPairing() {
  const session = state.pairings?.find(p => p.id === pairingId); if (!session) return;
  const panel = $('pair-status'); if (panel.dataset.version === JSON.stringify(session)) return; panel.dataset.version = JSON.stringify(session);
  panel.replaceChildren(element('p', '', '')); panel.firstChild.append(element('span', session.address, 'mono'), document.createTextNode(' '), element('span', pairState(session.state), 'pair-state'));
  if (session.state === 'failed') panel.append(element('p', errorMessage(session.error || 'pairing_failed'), 'warn'));
  if (session.state === 'paired') panel.append(element('p', msg('pair.done'), 'hint'));
  $('pair-cancel').hidden = !['connecting', 'waiting'].includes(session.state);
  if (session.state !== 'waiting') return;
  if (session.kind === 'confirmation') {
    panel.append(element('p', msg('pair.compare')), element('p', session.code, 'pair-code'));
    const row = element('div', '', 'row');
    for (const [title, accept, cls] of [[msg('pair.match'), true, 'primary'], [msg('pair.reject'), false, '']]) row.append(button(title, () => api(`/v1/pairings/${session.id}`, 'POST', {address: session.address, accept}), cls));
    panel.append(row);
  } else {
    panel.append(element('p', msg(session.kind === 'passkey' ? 'pair.passkey' : 'pair.pin')));
    const row = element('div', '', 'row'), code = document.createElement('input'); code.className = 'mono'; code.style.width = '160px'; code.inputMode = session.kind === 'passkey' ? 'numeric' : 'text'; code.maxLength = session.kind === 'passkey' ? 6 : 16;
    row.append(code, button(msg('pair.submit'), () => api(`/v1/pairings/${session.id}`, 'POST', {address: session.address, code: code.value}), 'primary')); panel.append(row);
  }
}

// ---------- 详情 ----------
const transcriptView = new TranscriptView(wsURL);
function block(title, note) { const node = element('section', '', 'block'), head = element('h4'); head.append(element('span', title)); if (note) head.append(element('small', note)); node.append(head); return node; }
function renderDetailHead(item) {
  const head = $('detail').querySelector('.detail-head'); if (!head) return;
  const next = detailHead(item); if (head.dataset.version === next.dataset.version) return; head.replaceWith(next);
}
function detailHead(item) {
  const head = element('div', '', 'detail-head'); head.dataset.version = JSON.stringify([language(), item.state, item.error, item.endReason, item.endedAt, item.answeredAt, item.callStatus?.state]);
  const left = element('div'), title = element('h3');
  title.append(document.createTextNode(`${item.direction === 'inbound' ? '↙' : '↗'} ${directionLabel(item)} `), element('span', deviceName(item.address)));
  if (item.address) title.append(document.createTextNode(' '), element('span', item.address, 'mono addr'));
  const times = [['detail.started', item.created], ['detail.answered', item.answeredAt], ['detail.ended', item.endedAt]].filter(([, v]) => Number.isFinite(v)).map(([k, v]) => `${t(k)} ${clockText(v).slice(11)}`);
  const seconds = callSeconds(item), live = isActive(item);
  const timesNode = element('div', [clockText(item.created).slice(0, 10), ...times].filter(Boolean).join(' · '), 'times');
  if (Number.isFinite(seconds)) { const durNode = element('span', duration(seconds)); if (live) durNode.dataset.liveDuration = item.id; timesNode.append(document.createTextNode(t('detail.duration')), durNode); }
  else if (live) timesNode.append(document.createTextNode(t('line.timerPending')));
  left.append(title, timesNode);
  const chip = element('span', String(outcomeText(item)) + (live && item.callStatus?.state === 'unknown' ? t('line.phoneUnverified') : ''), 'chip' + (live ? ' live' : outcomeIsBad(item) ? ' bad' : ''));
  const right = element('div', '', 'row'); right.append(chip);
  if (live) right.append(button(msg('common.hangup'), async () => { if (!confirm(t('detail.confirmHangup'))) return; await api(`/v1/conversations/${item.id}/end`, 'POST'); }));
  head.append(left, right); return head;
}
async function choose(key, rebuild = false) {
  if (selected && $('command-content')) drafts.set(selected, $('command-content').value);
  monitor.stop(); transcriptView.stop(); selected = key; commandRevision = -1; detailLive = false; const gen = generation; if (!rebuild) $('detail').replaceChildren(element('p', msg('common.loading'), 'detail-empty'));
  if (currentPage === 'calls') history.replaceState(null, '', `#calls/${key}`);
  // 存盘的历史会话可能没有内存详情：读取失败时用列表中的记录，只显示记录和完整转写。
  let item, historyOnly = false; try { item = await api(`/v1/conversations/${key}`); } catch (error) { item = state?.conversations?.find(c => c.id === key); if (!item) throw error; historyOnly = true; }
  if (selected !== key || gen !== generation) return;
  const detail = $('detail'); detail.replaceChildren(detailHead(item));
  if (!rebuild && window.innerWidth <= 980 && currentPage === 'calls') detail.scrollIntoView({block: 'start'});
  // 推送快照已显示结束时以快照为准，避免详情与快照不一致时反复重建。
  const known = state?.conversations?.find(c => c.id === key), live = detailLive = isActive(item) && (!known || isActive(known));
  if (historyOnly) { detail.append(element('p', msg('detail.historyOnly'), 'hint')); appendFullTranscript(key, gen, true); render(); return; }
  if (live) {
    const talk = block(msg('detail.live')); const transcriptPanel = element('div', '', 'transcripts'); talk.append(transcriptPanel); detail.append(talk); transcriptView.start(key, transcriptPanel, () => endDetail(key));
    const cmd = block(msg('detail.commands'), msg('detail.commandsNote'));
    const input = document.createElement('textarea'); input.id = 'command-content'; input.value = drafts.get(key) || ''; input.rows = 2; input.dataset.i18nPlaceholder = 'detail.commandHint'; input.placeholder = t('detail.commandHint'); input.oninput = () => drafts.set(key, input.value);
    const row = element('div', '', 'foot'); row.append(button(msg('detail.send'), async () => { if (selected !== key) throw localError('detail.switched'); await api(`/v1/conversations/${key}/commands`, 'POST', {content: input.value}); input.value = ''; drafts.delete(key); notice(); await commands(key, gen); }, 'primary'));
    const history = element('div', '', 'commands'); history.id = 'command-history'; cmd.append(input, row, history); detail.append(cmd);
    const mon = block(msg('detail.monitor'), msg('detail.monitorNote')); const box = element('div', '', 'monitor'), ctl = element('div', '', 'row');
    ctl.append(button(msg('detail.monitorStart'), () => monitor.start(key)), button(msg('common.stop'), () => monitor.stop(), 'quiet')); const status = element('span', msg('monitor.idle'), 'status'); status.id = 'monitor-status'; status.style.marginTop = '0'; ctl.append(status);
    const vol = element('div', '', 'row');
    for (const [title, channel] of [[msg('speaker.caller'), 0], [msg('speaker.ai'), 1]]) { const label = element('label'), slider = document.createElement('input'); label.append(element('span', title), ' '); slider.type = 'range'; slider.min = 0; slider.max = 1; slider.step = .05; slider.value = monitor.gain[channel]; slider.oninput = () => monitor.gain[channel] = +slider.value; label.append(slider); vol.append(label); }
    box.append(ctl, vol); mon.append(box); detail.append(mon);
  }
  const task = block(msg('detail.task')); const box = element('div', '', 'task'), dl = element('dl');
  for (const [k, v] of [['detail.taskVoice', item.task?.voice || item.voice || 'marin'], ['detail.taskPurpose', item.task?.purpose], ['detail.taskOutcome', item.task?.outcome]]) if (v) dl.append(element('dt', msg(k)), element('dd', v));
  box.append(dl); if (item.task?.content) box.append(element('pre', item.task.content)); task.append(box); detail.append(task);
  if (!live) { const history = element('div', '', 'commands'); history.id = 'command-history'; const cmd = block(msg('detail.sentCommands')); cmd.append(history); detail.append(cmd); cmd.hidden = !(item.commands?.length); }
  appendFullTranscript(key, gen, !live);
  renderCommands(item.commands); render();
}
// 完整转写：读 Server 存盘内容；进行中的通话只返回已写入部分。已结束的通话自动读取。
function appendFullTranscript(key, gen, auto) {
  const sec = block(msg(auto ? 'transcript.title' : 'transcript.savedTitle'), auto ? '' : msg('transcript.savedNote'));
  const body = element('div', '', 'transcript-body'), status = element('p', auto ? msg('common.loading') : '', 'status');
  const load = async () => {
    show(status, msg('common.loading')); const result = await api(`/v1/conversations/${encodeURIComponent(key)}/transcript`); if (selected !== key || gen !== generation) return;
    // 与实时文字同一套断句：同一说话人连续的片段合并成一句，双方按时间交错。
    const count = renderTurns(body, transcriptEvents(result));
    show(status, count ? '' : msg('transcript.none'));
  };
  const btn = button(msg(auto ? 'transcript.reload' : 'transcript.load'), load, 'quiet'); sec.firstChild.append(btn);
  sec.append(status, body); $('detail').append(sec);
  if (auto) void load().catch(error => { show(status, error); });
}
// 进行中的详情在通话结束时重建一次，只保留结局、任务、发过的指令和存盘对话。
function endDetail(key) {
  if (selected !== key || !detailLive) return;
  detailLive = false; transcriptView.stop(); if (monitor.key === key) monitor.stop();
  void choose(key, true).catch(error => notice(error.message));
}
function renderCommands(commands) { lastCommands = commands || []; const node = $('command-history'); if (!node) return; node.replaceChildren(); for (const command of commands || []) { const row = element('div', '', 'command'); row.append(element('small', commandLabel(command)), document.createTextNode(command.content)); node.append(row); } if (node.parentElement?.classList.contains('block')) node.parentElement.hidden = !(commands?.length) && !$('command-content'); }
async function commands(key, gen) { const result = await api(`/v1/conversations/${key}`); if (selected === key && generation === gen) renderCommands(result.commands); }

// ---------- 事件绑定 ----------
$('origin').value = location.port === '5173' ? 'http://127.0.0.1:8765' : location.origin;
$('attach').onclick = () => run(attach, $('attach'));
$('refresh').onclick = () => run(async () => { const gen = generation, snapshot = await api('/v1/console'); if (gen !== generation) return; state = snapshot; render(); syncDeviceVoice(true); });
$('add').onclick = () => { $('inbound-panel').hidden = true; $('pair-panel').hidden = false; $('pair-panel').scrollIntoView({block: 'nearest'}); };
$('inbound-back').onclick = () => { inboundPrompt.reset(); $('inbound-panel').hidden = true; };
$('inbound-save').onclick = () => run(() => inboundPrompt.save($('inbound-content').value), $('inbound-save')); $('pair-back').onclick = () => { $('pair-panel').hidden = true; };
$('scan').onclick = () => run(async () => { const result = await api('/v1/discovery', 'POST'); $('discovered').replaceChildren(); for (const device of result.devices) $('discovered').append(button(device.name ? `${device.name} ${device.address}` : device.address, () => { $('pair-address').value = device.address; })); if (!result.devices.length) notice(msg('pair.scanNone')); }, $('scan'));
$('pair-start').onclick = () => run(async () => { const result = await api('/v1/pairings', 'POST', {address: $('pair-address').value.trim().toUpperCase()}); pairingId = result.id; state = await api('/v1/console'); render(); }, $('pair-start'));
$('pair-cancel').onclick = () => run(() => api(`/v1/pairings/${pairingId}`, 'DELETE'));
$('task-file').onchange = () => run(async () => { const file = $('task-file').files[0]; if (!file) return; if (!/\.(txt|md)$/i.test(file.name) || file.size > 65536) throw localError('dial.fileInvalid'); $('task-content').value = new TextDecoder('utf-8', {fatal: true}).decode(await file.arrayBuffer()); uploadSource = 'upload'; });
$('task-content').oninput = () => { uploadSource = 'editor'; };
$('dial-form').onsubmit = event => { event.preventDefault(); run(async () => { if (!deviceVoice.state.ready) throw localError('voice.notReady'); const result = await api('/v1/conversations', 'POST', {address: $('dial-device').value, number: $('number').value.trim(), voice: $('dial-voice').value, task: {purpose: $('purpose').value, outcome: $('outcome').value, content: $('task-content').value, source: uploadSource}}); await choose(result.id); $('detail').scrollIntoView({block: 'start'}); }, event.submitter); };
$('dial-device').onchange = () => { syncDeviceVoice(true); updateDialGate(); };
$('dial-voice').onchange = () => { stopVoicePreview(); describeVoice(); void deviceVoice.save($('dial-voice').value).catch(error => notice(error.message)); };
$('voice-preview').onclick = () => run(previewVoice, $('voice-preview'));
$('voice-stop').onclick = stopVoicePreview;
$('voice-player').onplay = () => { show($('voice-preview-status'), msg('preview.playing')); };
$('voice-player').onpause = () => { if (voicePreview.url && !$('voice-player').ended) show($('voice-preview-status'), msg('preview.paused')); };
$('voice-player').onended = () => { show($('voice-preview-status'), msg('preview.ended')); };
$('voice-player').onerror = () => { if (voicePreview.url) show($('voice-preview-status'), msg('preview.error')); };
$('prompt-save').onclick = () => run(async () => { await api('/v1/system-prompt', 'PUT', {content: $('prompt-content').value}); notice(); }, $('prompt-save'));
window.addEventListener('hashchange', () => { if (state) applyHash(); });
window.addEventListener('pagehide', () => { generation++; deviceVoice.reset(); clearTimeout(reconnectTimer); events?.close(); monitor.stop(); transcriptView.stop(); stopVoicePreview(); });
// ---------- 界面语言 ----------
// 切换时就地重写文字，再按当前快照重画列表和详情头；不断开推送、实时文字和监听。
function applyLanguage() {
  translate();
  for (const node of document.querySelectorAll('[data-lang]')) node.setAttribute('aria-pressed', String(node.dataset.lang === language()));
  labelVoices();
  if (!state) return;
  devicesVersion = ''; callsVersion = ''; delete $('pair-status').dataset.version;
  render(); renderCommands(lastCommands);
}
for (const node of document.querySelectorAll('[data-lang]')) node.onclick = () => setLanguage(node.dataset.lang);
onLanguage(applyLanguage);
setLanguage(language(), false);
page(['devices', 'calls', 'prompt'].find(name => location.hash.startsWith('#' + name)) || 'devices');
run(attach);
