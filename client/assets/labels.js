// 界面文案：错误码、通话结局、手机状态、时间格式。纯函数，不碰 DOM；文案来自 messages.js。
import { has, localError, msg } from './i18n.js';

// 服务端错误码在词典里是 error.<code>；没有对应文案时显示错误码本身。
export function errorMessage(code) {
  if (!code) return msg('error.none');
  return has('error.' + code) ? msg('error.' + code) : msg('error.unknown', {code});
}
export const serverError = code => { const ref = errorMessage(code).i18n; return localError(ref.key, ref.params); };

// 结束原因（endReason）和故障码（error）的人话：end.<reason>、fault.<code>。
const fault = code => has('fault.' + code) ? msg('fault.' + code) : msg('outcome.failedCode', {code});
export const ACTIVE_STATES = ['dialing', 'answering', 'starting', 'active', 'ending', 'cleanup_failed'];
export const isActive = item => ACTIVE_STATES.includes(item?.state);

// 会话状态；未知状态原样显示。
export function stateLabel(state) { return has('state.' + state) ? msg('state.' + state) : state == null ? msg('state.unknown') : String(state); }

// 列表里的一句话结局；进行中返回当前阶段。返回 msg() 文案引用或原样字符串。
export function outcomeText(item) {
  const state = item?.state;
  if (ACTIVE_STATES.includes(state)) return stateLabel(state);
  if (state === 'failed') return item.error ? fault(item.error) : msg('state.failed');
  if (state === 'ended') {
    if (item.error && item.endReason !== 'remote_end' && item.endReason !== 'user_end') return fault(item.error);
    return has('end.' + item.endReason) ? msg('end.' + item.endReason) : msg(item.answeredAt ? 'state.ended' : 'outcome.missed');
  }
  return stateLabel(state);
}
export const outcomeIsBad = item => item?.state === 'failed' || item?.state === 'cleanup_failed'
  || (item?.state === 'ended' && Boolean(item.error) && !['remote_end', 'user_end'].includes(item.endReason));

// 线路恢复原因的人话。
export function recoveryText(reason) {
  if (has('fault.' + reason)) return msg('fault.' + reason);
  if (has('error.' + reason)) return msg('error.' + reason);
  return reason ? msg('recovery.reason', {reason}) : msg('recovery.unknown');
}

// 手机一行的状态句和灯色。
export function deviceStatus(device, lineAddress) {
  if (lineAddress && device.address === lineAddress) return {text: msg('device.onCall'), tone: 'live'};
  if (device.recovering) return {text: msg('device.recovering'), tone: 'red'};
  if (!device.paired) return {text: msg('device.unpaired'), tone: 'grey'};
  if (!device.connected) return {text: msg('device.offline'), tone: 'grey'};
  if (!device.hfp) return {text: msg('device.noHfp'), tone: 'amber'};
  return {text: msg('device.ready'), tone: 'green'};
}

// 时长 mm:ss（超过一小时为 h:mm:ss）。
export function duration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.floor(seconds), h = Math.floor(total / 3600), m = Math.floor(total % 3600 / 60), s = total % 60;
  const mm = String(m).padStart(2, '0'), ss = String(s).padStart(2, '0');
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
// 通话时长只从接通算起（顶部线路、记录、详情一致）：接通到结束，进行中到现在；没接通没有时长。
export function callSeconds(item, nowMs = Date.now()) {
  const start = item?.answeredAt;
  if (!Number.isFinite(start)) return NaN;
  const end = Number.isFinite(item.endedAt) ? item.endedAt : nowMs / 1000;
  return Math.max(0, end - start);
}
const pad = n => String(n).padStart(2, '0');
// 列表时间：今天只显示时:分，否则加月-日。
export function whenText(seconds, nowMs = Date.now()) {
  if (!Number.isFinite(seconds)) return '';
  const d = new Date(seconds * 1000), now = new Date(nowMs);
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}
export function clockText(seconds) {
  if (!Number.isFinite(seconds)) return '';
  const d = new Date(seconds * 1000);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
