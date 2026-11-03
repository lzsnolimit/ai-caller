export interface AudioSocket {
  send(data: string, callback: (error?: Error) => void): void;
}

/** Preserve short network bursts without queuing seconds of stale phone audio. */
export class ModelAudioWriter {
  private queue: { pcm: Buffer; arrived: number }[] = [];
  private pendingBytes = 0;
  private sending = false;
  private stopped = false;
  private timer: NodeJS.Timeout | undefined;
  peakQueuedBytes = 0;
  get queuedBytes(): number { return this.pendingBytes; }

  constructor(
    private readonly socket: AudioSocket,
    private readonly onWritten: (bytes: number, elapsedMs: number) => void,
    private readonly onFailure: (code: string) => void,
    private readonly maxPendingBytes = 48000, // One second at 24 kHz mono PCM16.
    private readonly sendTimeoutMs = 2000
  ) {}

  write(pcm: Buffer): void {
    if (this.stopped) return;
    if (!pcm.length || pcm.length % 2) return this.fail('invalid_capture_frame');
    if (this.pendingBytes + pcm.length > this.maxPendingBytes)
      return this.fail('model_audio_queue_full');
    this.queue.push({ pcm, arrived: performance.now() });
    this.pendingBytes += pcm.length;
    this.peakQueuedBytes = Math.max(this.peakQueuedBytes, this.pendingBytes);
    this.pump();
  }

  private pump(): void {
    if (this.stopped || this.sending) return;
    const item = this.queue.shift();
    if (!item) return;
    const { pcm, arrived } = item;
    this.sending = true;
    this.timer = setTimeout(() => this.fail('model_audio_send_timeout'), this.sendTimeoutMs);
    try {
      this.socket.send(
        JSON.stringify({ type: 'session.input_audio.append', audio: pcm.toString('base64') }),
        error => {
          if (this.stopped) return;
          clearTimeout(this.timer);
          if (error) return this.fail('model_audio_send');
          this.pendingBytes -= pcm.length;
          this.sending = false;
          this.onWritten(pcm.length, performance.now() - arrived);
          this.pump();
        }
      );
    } catch {
      this.fail('model_audio_send');
    }
  }

  private fail(code: string): void {
    if (this.stopped) return;
    this.close();
    this.onFailure(code);
  }

  close(): void {
    this.stopped = true;
    clearTimeout(this.timer);
    this.queue = [];
    this.pendingBytes = 0;
    this.sending = false;
  }
}
