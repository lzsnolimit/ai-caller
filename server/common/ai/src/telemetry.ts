/** Metadata only: never pass provider payloads, transcripts or PCM to the sink. */
export class Telemetry {
  private readonly origin: number;
  private readonly responses = new Map<string, { start: number; index: number }>();
  private readonly activity = new Map<string, { sampled: number; active: boolean }>();
  private sequence = 0;
  private stages = new Map<string, { count: number; sum: number; max: number; bytes: number; peak: number }>();
  private lastFlush = 0;
  private outputAt: number | undefined;
  private sessionReadyAt: number | undefined;
  private audioSequence = 0;

  stage(stage: string, durationMs = 0, bytes = 0, depth = 0): void {
    if (!this.stages.has(stage) && this.stages.size >= 32) return;
    const value = this.stages.get(stage) ?? { count: 0, sum: 0, max: 0, bytes: 0, peak: 0 };
    value.count++; value.sum += durationMs; value.max = Math.max(value.max, durationMs);
    value.bytes += bytes; value.peak = Math.max(value.peak, depth); this.stages.set(stage, value);
    if (this.clock() - this.lastFlush >= 10000) this.flush();
  }
  flush(): void {
    this.lastFlush = this.clock();
    for (const [stage, value] of this.stages) this.emit('audio_stage', {
      stage, frames: value.count, meanMs: value.sum / value.count, maxMs: value.max,
      bytes: value.bytes, peakDepth: value.peak, audioSequence: this.audioSequence });
    this.stages.clear();
  }

  private created = 0;
  private completed = 0;
  private failed = 0;
  private backendEvents = 0;
  private voiceModel: string | undefined;
  private backendModel: string | undefined;
  private readonly observedModels = new Set<string>();

  constructor(private readonly sink: (value: Record<string, unknown>) => void,
    private readonly clock = () => performance.now()) {
    this.origin = clock();
  }

  emit(event: string, fields: Record<string, unknown> = {}): void {
    try { this.sink({ event, sequence: ++this.sequence, elapsedMs: Math.round(this.clock() - this.origin), clockDomain: "node_performance", ...fields }); }
    catch { /* A failed diagnostic sink must not stop audio. */ }
  }

  observe(value: Record<string, unknown>): void {
    if (value.type === 'session.output_audio.delta') {
      this.audioSequence++;
      if (this.outputAt !== undefined) this.stage('model_output_gap', this.clock() - this.outputAt);
      this.outputAt = this.clock();
      if (this.sessionReadyAt !== undefined) {
        this.stage('model_first_output', this.clock() - this.sessionReadyAt);
        this.sessionReadyAt = undefined;
      }
    }
    if (value.type === 'session.started') {
      this.sessionReadyAt = this.clock();
      const session = object(value.session);
      this.voiceModel = model(session.model);
      this.backendModel = model(object(object(session.delegation).responses).model);
      this.emit('model_session_ready', { resolvedVoiceModel: this.voiceModel, resolvedBackendModel: this.backendModel });
    } else if (value.type === 'response.event') {
      this.backendEvents++;
      const nested = object(value.event);
      const response = object(nested.response);
      const observed = model(response.model);
      if (observed && this.observedModels.size < 32) this.observedModels.add(observed);
      const id = typeof response.id === 'string' ? response.id : undefined;
      const type = nested.type;
      if (type === 'response.created') {
        this.created++;
        if (id && this.responses.size < 128) this.responses.set(id, { start: this.clock(), index: this.created });
      } else if (type === 'response.completed') this.completed++;
      else if (type === 'response.failed' || type === 'response.incomplete' || type === 'response.cancelled') this.failed++;
      else return; // Text and tool deltas are counted, never serialized.
      const start = id ? this.responses.get(id) : undefined;
      this.emit('backend_response', { responseEvent: type, observedBackendModel: observed,
        ...(type === 'response.created' ? { backendResponseIndex: this.created } : start ? {
          backendResponseIndex: start.index, durationMs: Math.round(this.clock() - start.start) } : {}) });
      if (id && type !== 'response.created') this.responses.delete(id);
    } else if (value.type === 'error') {
      // The error message can contain prompts and credentials. Preserve classification only.
      this.emit('model_error');
    }
  }

  audio(direction: 'input' | 'output', pcm: Buffer): void {
    const now = this.clock();
    const previous = this.activity.get(direction);
    if (previous && now - previous.sampled < 100) return;
    let energy = 0, count = 0;
    for (let offset = 0; offset + 1 < pcm.length; offset += 16) {
      const sample = pcm.readInt16LE(offset);
      energy += sample * sample;
      count++;
    }
    const active = count > 0 && Math.sqrt(energy / count) > 150;
    this.activity.set(direction, { sampled: now, active });
    if (active !== (previous?.active ?? false)) this.emit('audio_activity', { direction, active });
  }

  snapshot(): Record<string, unknown> {
    return { resolvedVoiceModel: this.voiceModel, resolvedBackendModel: this.backendModel,
      backendEvents: this.backendEvents, backendCreated: this.created, backendCompleted: this.completed,
      backendFailed: this.failed, observedBackendModels: [...this.observedModels] };
  }

  sampleQuiet(): void {
    for (const [direction, value] of this.activity) {
      if (value.active && this.clock() - value.sampled >= 300) {
        value.active = false;
        this.emit('audio_activity', { direction, active: false, deliveryGap: true });
      }
    }
  }
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function model(value: unknown): string | undefined {
  return typeof value === 'string' && /^gpt-[a-z0-9.-]{1,80}$/.test(value) ? value : undefined;
}
