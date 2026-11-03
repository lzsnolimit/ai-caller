import { Socket } from 'node:net';

/** Private text IPC. No transcript content is written to stdout or diagnostics. */
export class TranscriptWriter {
  private sequence = 0;
  private dropped = 0;
  private invalid = 0;
  private seen = new Set<string>();
  private timer: NodeJS.Timeout;
  private closed = false;
  constructor(private sink: {writableLength:number; destroyed:boolean; write(frame:string):unknown; end():unknown; destroy():unknown}) {
    this.timer = setInterval(() => this.status(), 100);
    this.timer.unref();
  }
  private write(value: Record<string, unknown>): boolean {
    const frame = JSON.stringify(value) + '\n';
    if (this.closed || this.sink.destroyed || this.sink.writableLength + Buffer.byteLength(frame) > 65536) return false;
    try { this.sink.write(frame); return true; } catch { return false; }
  }
  status(): void { this.write({type:'transcript.status', sequence:this.sequence, dropped:this.dropped, invalid:this.invalid}); }
  append(event: Record<string, unknown>): void {
    const {delta, event_id, start_ms, end_ms} = event;
    if (typeof delta !== 'string' || !delta.length || delta.length > 8192 || typeof event_id !== 'string' || !event_id.length || event_id.length > 256 ||
      typeof start_ms !== 'number' || !Number.isFinite(start_ms) || start_ms < 0 || typeof end_ms !== 'number' || !Number.isFinite(end_ms) || end_ms < start_ms) {
      this.invalid++; return;
    }
    const role = event.type === 'session.input_transcript.delta' ? 'caller' : 'ai';
    const key = `${role}:${event_id}`;
    if (this.seen.has(key)) return;
    this.seen.add(key);
    if (this.seen.size > 4096) this.seen.delete(this.seen.values().next().value!);
    this.sequence++;
    if (!this.write({type:'transcript.delta', sequence:this.sequence, dropped:this.dropped, invalid:this.invalid,
      role, eventId:event_id, text:delta, startMs:start_ms, endMs:end_ms})) this.dropped++;
  }
  close(): void {
    if (this.closed) return;
    clearInterval(this.timer);
    this.status(); this.closed = true;
    this.sink.end();
    setTimeout(() => this.sink.destroy(), 100).unref();
  }
}

export function transcriptWriter(fd: string | undefined): TranscriptWriter | undefined {
  if (!fd || !/^\d+$/.test(fd)) return;
  const socket = new Socket({fd:Number(fd), readable:false, writable:true});
  socket.on('error', () => socket.destroy());
  socket.unref();
  return new TranscriptWriter(socket);
}
