// 界面语言：中文（zh）和英文（en）。词典在 messages.js。
// 文本节点带 data-i18n（和 data-i18n-args），切换语言时就地重写，不重建页面、不断开连接。
import { MESSAGES } from './messages.js';

export const LANGUAGES = ['zh', 'en'];
const STORE_KEY = 'ai-phone.language';
const HTML_LANG = {zh: 'zh-CN', en: 'en'};
const ATTRIBUTES = [['i18nPlaceholder', 'placeholder'], ['i18nAriaLabel', 'aria-label'], ['i18nTitle', 'title']];  // data-i18n-placeholder 等
const listeners = new Set();

function saved() { try { return globalThis.localStorage?.getItem(STORE_KEY); } catch { return null; } }
function detect() {
  const choice = saved(); if (LANGUAGES.includes(choice)) return choice;
  const nav = globalThis.navigator, preferred = nav?.languages?.[0] || nav?.language || '';
  return /^zh\b/i.test(preferred) ? 'zh' : 'en';
}
let current = detect();

export const language = () => current;
export const has = key => Object.hasOwn(MESSAGES[current], key);

// 参数可以是普通值、文案引用（msg）或带文案引用的错误。
function resolve(value) {
  if (value?.i18n) return t(value.i18n.key, value.i18n.params);
  if (value instanceof Error) return value.message;
  return value ?? '';
}
// 存进 data-i18n-args 前把错误变成可序列化的形式。
function plain(params) {
  if (!params) return undefined;
  const result = {};
  for (const [name, value] of Object.entries(params)) result[name] = value?.i18n ? {i18n: value.i18n} : value instanceof Error ? value.message : value;
  return result;
}
export function t(key, params) {
  const text = MESSAGES[current][key] ?? key;
  return params ? text.replace(/\{(\w+)\}/g, (match, name) => Object.hasOwn(params, name) ? String(resolve(params[name])) : match) : text;
}
// 文案引用：先不翻译，显示时按当前语言翻译。String(msg(...)) 得到当前语言的文字。
export function msg(key, params) { return {i18n: {key, params: plain(params)}, toString() { return t(key, params); }}; }
export function localError(key, params) { const error = Error(t(key, params)); error.i18n = {key, params: plain(params)}; return error; }

/** 写入节点文字。message 可以是 msg()、带文案的错误、普通错误或字符串（用户内容，原样显示）。 */
export function show(node, message) {
  const ref = message?.i18n;
  if (ref) {
    node.dataset.i18n = ref.key;
    if (ref.params) node.dataset.i18nArgs = JSON.stringify(ref.params); else delete node.dataset.i18nArgs;
    node.textContent = t(ref.key, ref.params);
  } else {
    delete node.dataset.i18n; delete node.dataset.i18nArgs;
    node.textContent = message instanceof Error ? message.message : message ?? '';
  }
  return node;
}

/** 按当前语言重写 root 下所有带 data-i18n* 的节点。 */
export function translate(root = document) {
  for (const node of root.querySelectorAll('[data-i18n]')) node.textContent = t(node.dataset.i18n, node.dataset.i18nArgs ? JSON.parse(node.dataset.i18nArgs) : undefined);
  for (const [data, attribute] of ATTRIBUTES)
    for (const node of root.querySelectorAll(`[data-i18n-${attribute}]`)) node.setAttribute(attribute, t(node.dataset[data]));
}

export function setLanguage(next, remember = true) {
  if (!LANGUAGES.includes(next)) return;
  current = next;
  if (remember) { try { globalThis.localStorage?.setItem(STORE_KEY, next); } catch {} }
  const root = globalThis.document?.documentElement; if (root) root.lang = HTML_LANG[next];
  for (const listener of listeners) listener(next);
}
export function onLanguage(listener) { listeners.add(listener); }
