import { EventEmitter } from 'node:events';
import WebSocket from 'ws';
import { z } from 'zod';

const addressSchema = z.string().regex(/^(?:[0-9a-f]{2}:){5}[0-9a-f]{2}$/i);
const leaseSchema = z.object({ lease: z.string().min(1), expiresIn: z.number().positive() });
const eventSchema = z.discriminatedUnion('type', [
  z.object({ type: z.literal('audio.ready') }),
  z.object({ type: z.literal('audio.closed') }),
  z.object({ type: z.literal('error'), code: z.string() }),
]);
const MAX_PAYLOAD = 48000;
const MAX_AUDIO_QUEUE = 9600; // 200 ms of PCM; never accumulate seconds of stale audio.

/** Network-only phone client. No SSH, Android tools or Bluetooth APIs are used here. */
export class GatewayClient extends EventEmitter {
  get queuedBytes(): number { return this.socket?.bufferedAmount ?? 0; }
  readonly base: URL;
  private lease: string | undefined;
  private renewal: NodeJS.Timeout | undefined;
  private socket: WebSocket | undefined;
  private stopped = false;
  private ready = false;
  private normalEnd = false;
  private callSocket: WebSocket | undefined;
  private callRetry: NodeJS.Timeout | undefined;

  watchCall(conversationId: string | undefined, address?: string, callId?: string): void {
    if (conversationId && !/^[a-f0-9]{32}$/.test(conversationId)) throw new Error('Invalid conversation ID.');
    if (!conversationId) addressSchema.parse(address);
    let retry = 250;
    const connect = (): void => {
      if (this.stopped) return;
      const url = new URL(conversationId ? `/v1/conversations/${conversationId}/events` : '/v1/events', this.base);
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
      const socket = this.callSocket = new WebSocket(url, {
        handshakeTimeout: 10000, followRedirects: false, maxPayload: 4096, perMessageDeflate: false,
      });
      socket.on('message', raw => {
        if (this.stopped || this.callSocket !== socket) return;
        try {
          let value = JSON.parse(raw.toString());
          if (!conversationId) {
            const snapshot = z.object({ type: z.literal('phone.state'), phones: z.array(z.object({ address: z.string(),
              calls: z.array(z.object({ id: z.string(), state: z.string() })).nullable() })) }).parse(value);
            const phone = snapshot.phones.find(p => p.address.toUpperCase() === address!.toUpperCase());
            value = { type: 'call.state', state: !phone || phone.calls === null ? 'unknown' : phone.calls.find(c => c.id === callId)?.state ?? 'ended' };
          }
          value = z.object({ type: z.literal('call.state'),
            state: z.enum(['unknown', 'dialing', 'alerting', 'active', 'held', 'incoming', 'waiting', 'ended']) }).parse(value);
          retry = 250;
          this.emit('callState', value.state);
        } catch { this.emit('callState', 'unknown'); }
      });
      socket.on('error', () => { /* The close handler retries without stopping audio. */ });
      socket.on('unexpected-response', (_request, response) => {
        response.resume();
        socket.terminate();
      });
      socket.on('close', () => {
        if (this.stopped || this.callSocket !== socket) return;
        this.emit('callState', 'unknown');
        this.callRetry = setTimeout(connect, retry);
        retry = Math.min(retry * 2, 5000);
      });
    };
    connect();
  }

  constructor(base: string) {
    super();
    this.base = new URL(base);
    if (
      !['http:', 'https:'].includes(this.base.protocol) ||
      this.base.username ||
      this.base.password ||
      this.base.search ||
      this.base.hash ||
      this.base.pathname !== '/'
    )
      throw new Error('Gateway must be a plain HTTP(S) origin.');
  }

  async request(path: string, method = 'GET', body?: unknown): Promise<unknown> {
    if (!/^\/v1\/[a-zA-Z0-9/:%._-]+$/.test(path)) throw new Error('Invalid API path.');
    const response = await fetch(new URL(path, this.base), {
      method,
      headers: {
        ...(process.env.PHONE_CONVERSATION_ID ? { 'X-Conversation-Id': process.env.PHONE_CONVERSATION_ID } : {}),
        ...(this.lease ? { 'X-Controller-Lease': this.lease } : {}),
        ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(10000),
      redirect: 'error',
    });
    // Do not expose response bodies: they can contain numbers or device details.
    if (!response.ok) {
      await response.body?.cancel();
      throw new Error(`Gateway HTTP ${response.status}`);
    }
    if (response.status === 204) return undefined;
    return response.json();
  }

  async acquire(): Promise<void> {
    if (this.lease) return;
    if (this.stopped) throw new Error('Gateway client is stopped.');
    const value = leaseSchema.parse(await this.request('/v1/lease', 'POST'));
    this.lease = value.lease;
    if (this.stopped) {
      await this.release();
      return;
    }
    this.renewal = setInterval(
      () => {
        void this.request('/v1/lease', 'PUT').catch(() => {
          this.emit('failure', 'lease_lost');
          void this.close();
        });
      },
      Math.min(10000, (value.expiresIn * 1000) / 3)
    );
  }

  async openAudio(address: string): Promise<void> {
    addressSchema.parse(address);
    await this.acquire();
    if (this.stopped) return;
    if (this.socket) throw new Error('Audio is already open.');
    const url = new URL('/v1/audio', this.base);
    url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
    url.searchParams.set('address', address.toUpperCase());
    const socket = (this.socket = new WebSocket(url, {
      headers: { 'X-Controller-Lease': this.lease! },
      handshakeTimeout: 10000,
      followRedirects: false,
      maxPayload: MAX_PAYLOAD,
      perMessageDeflate: false,
    }));
    socket.on('message', (data, binary) => {
      if (this.stopped) return;
      try {
        if (binary) {
          const pcm = Buffer.from(data as Buffer);
          if (!this.ready || !pcm.length || pcm.length % 2 || pcm.length > MAX_PAYLOAD)
            throw new Error('Invalid audio frame.');
          this.emit('pcm', pcm);
        } else {
          const event = eventSchema.parse(JSON.parse(data.toString()));
          if (event.type === 'audio.ready') {
            this.ready = true;
            this.emit('ready');
          } else if (event.type === 'error') {
            this.emit('failure', 'gateway_audio');
            void this.close();
          } else {
            this.normalEnd = true;
            this.emit('ended');
            void this.close();
          }
        }
      } catch {
        this.emit('failure', 'invalid_gateway_frame');
        void this.close();
      }
    });
    socket.on('error', () => {
      this.emit('failure', 'gateway_connection');
      void this.close();
    });
    socket.on('unexpected-response', (_request, response) => {
      response.resume();
      this.emit('failure', `gateway_http_${response.statusCode}`);
      void this.close();
    });
    socket.on('close', () => {
      if (!this.stopped && !this.normalEnd) this.emit('failure', 'gateway_disconnected');
      else this.emit('ended');
      void this.close();
    });
  }

  send(pcm: Buffer): void {
    if (
      !this.ready ||
      this.socket?.readyState !== WebSocket.OPEN ||
      pcm.length % 2 ||
      pcm.length > MAX_PAYLOAD ||
      this.socket.bufferedAmount > MAX_AUDIO_QUEUE
    )
      throw new Error('Audio unavailable or buffer limit exceeded.');
    // The model can emit large chunks. Send short frames without a pacing timer.
    for (let offset = 0; offset < pcm.length; offset += 960)
      this.socket.send(pcm.subarray(offset, offset + 960));
  }

  private async release(): Promise<void> {
    if (!this.lease) return;
    try {
      await this.request('/v1/lease', 'DELETE');
    } finally {
      this.lease = undefined;
    }
  }

  async close(): Promise<void> {
    if (this.stopped) return;
    this.stopped = true;
    this.ready = false;
    clearInterval(this.renewal);
    clearTimeout(this.callRetry);
    this.callSocket?.terminate();
    this.socket?.terminate();
    try {
      await this.release();
    } catch {
      /* Lease expiry also releases server resources. */
    }
  }
}
