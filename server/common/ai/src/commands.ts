import WebSocket from 'ws';

type Command = { conversationId: string; id: string; content: string };
type Status = 'written' | 'injected' | 'rejected' | 'unknown';
/** Bounded command receipts. An injection receipt is not proof of spoken output. */
export class Commands {
  private readonly pending = new Map<string, { start: number; timer: NodeJS.Timeout }>();
  private readonly issued = new Set<string>();
  constructor(private readonly conversationId: string | undefined,
    private readonly report: (event: Record<string, unknown>) => void,
    private readonly timeoutMs = 10000) {}

  private publish(id: string, status: Status, reason?: string): void {
    const value = this.pending.get(id);
    if (!value) return;
    if (status !== 'written') { clearTimeout(value.timer); this.pending.delete(id); }
    try { this.report({ commandId: id, commandEventId: `operator_${id}`, commandStatus: status,
      commandElapsedMs: Math.round(performance.now() - value.start), ...(reason ? { commandReason: reason } : {}) }); }
    catch { /* Status output must not fail the audio path. */ }
  }

  send(command: Command, model: WebSocket | undefined, ready: boolean): void {
    const { id, content } = command;
    if (!/^[a-f0-9]{32}$/.test(id) || this.issued.has(id)) return;
    // Bound lifetime IDs as well as pending timers. Never resend an issued ID.
    if (this.issued.size >= 128 || this.pending.size >= 32) {
      this.report({commandId: id, commandStatus: 'rejected', commandReason: 'command_limit'}); return;
    }
    this.issued.add(id);
    this.pending.set(id, {start: performance.now(), timer: setTimeout(() => this.publish(id, 'unknown', 'ack_timeout'), this.timeoutMs)});
    if (command.conversationId !== this.conversationId || !ready || model?.readyState !== WebSocket.OPEN) {
      this.publish(id, 'rejected', 'not_ready'); return;
    }
    // Preserve the local character limit; the provider enforces its token limit.
    if (!content.trim() || content.length > 4096) {
      this.publish(id, 'rejected', 'command_too_large'); return;
    }
    try {
      model.send(JSON.stringify({type: 'session.instructions.append', event_id: `operator_${id}`,
        delegation_id: null, content}), error => this.publish(id, error ? 'unknown' : 'written', error ? 'model_write_failed' : undefined));
    } catch { this.publish(id, 'unknown', 'model_write_failed'); }
  }

  observe(event: Record<string, unknown>): boolean {
    if (event.type !== 'session.instructions.appended' && event.type !== 'error') return false;
    const error = event.error && typeof event.error === 'object' ? event.error as Record<string, unknown> : {};
    const reference = event.type === 'error' ? error.client_event_id ?? event.client_event_id : event.client_event_id;
    if (typeof reference !== 'string' || !/^operator_[a-f0-9]{32}$/.test(reference)) return false;
    const id = reference.slice(9);
    if (!this.issued.has(id)) return false;
    // Late and duplicate receipts cannot change a terminal status or fail the call.
    this.publish(id, event.type === 'error' ? 'rejected' : 'injected', event.type === 'error' ? 'provider_rejected' : undefined);
    return true;
  }

  close(): void {
    for (const id of this.pending.keys()) this.publish(id, 'unknown', 'conversation_closed');
  }
}
