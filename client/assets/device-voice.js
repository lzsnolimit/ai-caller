import { localError, msg } from './i18n.js';

/** Server memory owns the preference; pending saves retain their original device. Status is a msg() reference. */
export class DeviceVoice {
  constructor(request, changed) {
    this.request = request;
    this.changed = changed;
    this.epoch = 0;
    this.serial = 0;
    this.pending = new Map();
    this.state = {server: '', address: '', voice: null, ready: false, saving: false, status: ''};
  }
  emit(update) { Object.assign(this.state, update); this.changed({...this.state}); }
  reset() {
    this.epoch++;
    this.emit({server: '', address: '', voice: null, ready: false, saving: false, status: ''});
  }
  async load(server, address) {
    const epoch = ++this.epoch, key = server + '/' + address;
    this.emit({server, address, voice: null, ready: false, saving: false,
      status: msg(address ? 'voice.deviceLoading' : 'voice.pickPhone')});
    if (!address) return;
    try {
      await this.pending.get(key)?.catch(() => {});
      const value = await this.request(server, `/v1/devices/${encodeURIComponent(address)}/voice`);
      if (epoch === this.epoch) this.emit({voice: value.voice, ready: true,
        status: msg('voice.deviceReady')});
    } catch (error) {
      if (epoch === this.epoch) this.emit({status: msg('voice.loadFailed', {error})});
    }
  }
  async save(voice) {
    if (!this.state.ready || !this.state.address) throw localError('voice.notReady');
    const {server, address} = this.state, key = server + '/' + address;
    const epoch = this.epoch, serial = ++this.serial;
    this.emit({voice, saving: true, status: msg('voice.saving')});
    const previous = this.pending.get(key) || Promise.resolve();
    const job = previous.catch(() => {}).then(() => this.request(server,
      `/v1/devices/${encodeURIComponent(address)}/voice`, 'PUT', {voice}));
    this.pending.set(key, job);
    try {
      await job;
      if (epoch === this.epoch && serial === this.serial) this.emit({saving: false,
        status: msg('voice.saved')});
    } catch (error) {
      if (epoch === this.epoch && serial === this.serial) this.emit({saving: false,
        status: msg('voice.saveFailed', {error})});
    } finally {
      if (this.pending.get(key) === job) this.pending.delete(key);
    }
  }
}
