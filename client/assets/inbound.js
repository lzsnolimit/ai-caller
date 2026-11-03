// 来电相关的纯逻辑：方向显示、完整转写解析、每台手机的来电接待语编辑状态。
// 不直接操作 DOM，方便离线测试。状态文字是 msg() 文案引用，显示时按当前语言翻译。
import { localError, msg } from './i18n.js';

// 旧服务快照没有 direction；旧版本只有去电，因此缺失时按去电显示。
export function directionLabel(item) {
  return msg(item?.direction === 'inbound' ? 'direction.inbound' : 'direction.outbound');
}

// 完整转写：Server 返回 {type:"transcript.record", events:[...]}，按写入顺序显示；
// 旧服务返回的缓存快照也有 events。丢弃无效条目。
export function transcriptEvents(body) {
  const list = Array.isArray(body?.events) ? body.events : [];
  return list.filter(event => event && ['caller', 'ai'].includes(event.role) && typeof event.text === 'string');
}

// 手机一行的来电接待语标签；旧快照没有 inboundPrompt 时返回 null，不显示。
export function inboundPromptTag(device) {
  const value = device?.inboundPrompt;
  if (!value || typeof value !== 'object') return null;
  return msg(value.configured ? 'inbound.tagCustom' : 'inbound.tagDefault');
}

export const INBOUND_PROMPT_MAX_BYTES = 16 * 1024;

/** 一次只编辑一台手机；切换手机或服务后，旧请求的结果不再生效。 */
export class InboundPrompt {
  constructor(request, changed) {
    this.request = request;
    this.changed = changed;
    this.epoch = 0;
    this.state = {server: '', address: '', content: '', guard: '', isDefault: false, ready: false, saving: false, status: ''};
  }
  emit(update) { Object.assign(this.state, update); this.changed({...this.state}); }
  reset() {
    this.epoch++;
    this.emit({server: '', address: '', content: '', guard: '', isDefault: false, ready: false, saving: false, status: ''});
  }
  path(address) { return `/v1/devices/${encodeURIComponent(address)}/inbound-prompt`; }
  async load(server, address) {
    const epoch = ++this.epoch;
    this.emit({server, address, content: '', guard: '', isDefault: false, ready: false, saving: false, status: msg('common.loading')});
    try {
      const value = await this.request(server, this.path(address));
      if (epoch !== this.epoch) return;
      this.emit({content: typeof value.content === 'string' ? value.content : '', guard: typeof value.guard === 'string' ? value.guard : '',
        isDefault: Boolean(value.default), ready: true,
        status: msg(value.default ? 'inbound.isDefault' : 'inbound.own')});
    } catch (error) {
      if (epoch === this.epoch) this.emit({status: msg('inbound.loadFailed', {error})});
    }
  }
  async save(content) {
    if (!this.state.ready || !this.state.address) throw localError('inbound.notReady');
    if (new TextEncoder().encode(content).length > INBOUND_PROMPT_MAX_BYTES) throw localError('inbound.tooLarge');
    const {server, address} = this.state, epoch = this.epoch;
    this.emit({saving: true, status: msg('inbound.saving')});
    try {
      const value = await this.request(server, this.path(address), 'PUT', {content});
      if (epoch !== this.epoch) return;
      const saved = typeof value?.content === 'string' ? value.content : content;
      this.emit({content: saved, isDefault: Boolean(value?.default), saving: false,
        status: msg('inbound.saved')});
    } catch (error) {
      if (epoch === this.epoch) this.emit({saving: false, status: msg('inbound.saveFailed', {error})});
    }
  }
}
