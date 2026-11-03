import { EventEmitter } from 'node:events';
import { Socket } from 'node:net';

const MAX_PENDING_PCM = 48000; // One second of PCM, as in the model input writer.
const SEND_TIMEOUT_MS = 2000; // Match the model input writer's stalled-send deadline.

export interface BinarySink {
  write(data: Buffer, callback: (error?: Error | null) => void): unknown;
}

/** Keep short bursts in order. Only one socket write is in flight. */
export class PrivateAudioWriter {
  private frames: { data: Buffer; arrived: number }[] = [];
  private pendingBytes = 0;
  private sending = false;
  private stopped = false;
  private timer: NodeJS.Timeout | undefined;
  peakQueuedBytes = 0;
  get queuedBytes(): number { return this.pendingBytes; }

  constructor(private readonly sink: BinarySink, private readonly onFailure: (code: string) => void,
    private readonly maxPendingBytes = MAX_PENDING_PCM, private readonly sendTimeoutMs = SEND_TIMEOUT_MS,
    private readonly onWritten: (bytes: number, elapsedMs: number, queuedBytes: number) => void = () => {}) {}

  write(pcm: Buffer): void {
    if (this.stopped) return;
    if (this.pendingBytes + pcm.length > this.maxPendingBytes) return this.fail('private_audio_queue_full');
    this.pendingBytes += pcm.length;
    this.peakQueuedBytes = Math.max(this.peakQueuedBytes, this.pendingBytes);
    const arrived = performance.now();
    for (let offset = 0; offset < pcm.length; offset += 960) {
      const part = pcm.subarray(offset, offset + 960), frame = Buffer.allocUnsafe(part.length + 5);
      frame.writeUInt32BE(part.length + 1); frame[4] = 0; part.copy(frame, 5);
      this.frames.push({ data: frame, arrived });
    }
    this.pump();
  }

  private pump(): void {
    if (this.stopped || this.sending) return;
    const item = this.frames.shift();
    if (!item) return;
    const { data: frame, arrived } = item;
    this.sending = true;
    this.timer = setTimeout(() => this.fail('private_audio_send_timeout'), this.sendTimeoutMs);
    try {
      this.sink.write(frame, error => {
        if (this.stopped) return;
        clearTimeout(this.timer);
        if (error) return this.fail('private_audio_send');
        this.pendingBytes -= frame.length - 5;
        this.sending = false;
        this.onWritten(frame.length - 5, performance.now() - arrived, this.pendingBytes);
        this.pump();
      });
    } catch { this.fail('private_audio_send'); }
  }

  private fail(code: string): void {
    if (this.stopped) return;
    this.close();
    this.onFailure(code);
  }

  close(): void {
    this.stopped = true;
    clearTimeout(this.timer);
    this.frames = [];
    this.pendingBytes = 0;
    this.sending = false;
  }
}

/** Inherited socket, private to one conversation. Length + type + binary PCM/JSON. */
export class PrivateAudio extends EventEmitter {
  private socket: Socket | undefined;
  private pending = Buffer.alloc(0);
  private output: PrivateAudioWriter | undefined;
  private stopped = false;
  private ready = false;
  get queuedBytes(): number { return this.output?.queuedBytes ?? 0; }
  constructor(private readonly fd: number) { super(); }
  async openAudio(_address?: string): Promise<void> {
    if (!Number.isInteger(this.fd) || this.fd < 3) throw new Error('Invalid audio descriptor.');
    const socket = this.socket = new Socket({ fd: this.fd, readable: true, writable: true });
    this.output = new PrivateAudioWriter(socket, code => {
      this.emit('failure', code);
      void this.close();
    }, MAX_PENDING_PCM, SEND_TIMEOUT_MS, (bytes, elapsedMs, queuedBytes) => {
      this.emit('outputWritten', bytes, elapsedMs, queuedBytes);
    });
    socket.on('data', data => {
      if (this.stopped) return;
      this.pending = Buffer.concat([this.pending, data]);
      try {
        while (this.pending.length >= 4) {
          const size = this.pending.readUInt32BE();
          if (size < 2 || size > 4096) throw new Error('Invalid private frame.');
          if (this.pending.length < size + 4) break;
          const kind = this.pending[4], payload = this.pending.subarray(5, size + 4);
          this.pending = this.pending.subarray(size + 4);
          if (kind === 0) {
            if (!this.ready || payload.length > 960 || payload.length % 2) throw new Error('Invalid PCM.');
            this.emit('pcm', payload);
          } else if (kind === 1) {
            const event = JSON.parse(payload.toString());
            if (event.type === 'audio.ready') { this.ready = true; this.emit('ready'); }
            else if (event.type === 'call.state') this.emit('callState', event.state);
            else if (event.type === 'audio.closed') { this.emit('ended'); void this.close(); }
            else if (event.type === 'error') { this.emit('failure', 'gateway_audio'); void this.close(); }
            else throw new Error('Invalid control frame.');
          } else throw new Error('Invalid private frame.');
        }
      } catch { this.emit('failure', 'invalid_gateway_frame'); void this.close(); }
    });
    socket.on('error', () => { if (!this.stopped) this.emit('failure', 'gateway_connection'); });
    socket.on('close', () => { if (!this.stopped) this.emit('failure', 'gateway_disconnected'); });
    // Announce readiness only after the inherited socket reader and handlers exist.
    const payload = Buffer.from(JSON.stringify({type: 'audio.reader.ready'}));
    const frame = Buffer.allocUnsafe(payload.length + 5);
    frame.writeUInt32BE(payload.length + 1); frame[4] = 1; payload.copy(frame, 5);
    socket.write(frame, error => {
      if (error && !this.stopped) { this.emit('failure', 'gateway_connection'); void this.close(); }
    });
  }
  send(pcm: Buffer): void {
    if (!this.ready || this.stopped || !pcm.length || pcm.length % 2 || pcm.length > 48000)
      throw new Error('Audio unavailable or invalid PCM.');
    this.output!.write(pcm);
  }
  async close(): Promise<void> {
    this.stopped = true; this.ready = false; this.pending = Buffer.alloc(0); this.output?.close(); this.socket?.destroy();
  }
}
