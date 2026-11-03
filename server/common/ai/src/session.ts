import { EventEmitter } from 'node:events';
import WebSocket from 'ws';
import { z } from 'zod';
import { ModelAudioWriter } from './model-audio.js';
import { Telemetry } from './telemetry.js';
import { validateVoice } from './voices.js';
import { Commands } from './commands.js';

/** PCM16LE, mono, 24 kHz. The host owns devices, calls and transport startup. */
export interface DuplexAudio extends EventEmitter {
  readonly queuedBytes: number;
  send(pcm: Buffer): void;
  close(): Promise<void>;
}
export interface SessionOptions {
  key: string; liveUrl: string; authHeader?: string | undefined; backendModel?: string | undefined;
  voice?: string; instructions?: string; conversationId?: string | undefined; managed?: boolean;
  waitForAnswer?: boolean | undefined; testGreeting?: boolean | undefined; seconds?: number;
  report: (event: Record<string, unknown>) => void;
  onFailure: () => void; onClosing: () => void;
  onTranscript?: ((event: Record<string, unknown>) => void) | undefined;
  connect?: (url: string, options: WebSocket.ClientOptions) => WebSocket;
  commandTimeoutMs?: number;
}
/** One independent model session; no OS, HTTP server, dialing or process ownership. */
export function createAISession(gateway: DuplexAudio, options: SessionOptions) {
  const { key, liveUrl, report, managed } = options;
  const voice = validateVoice(options.voice);
  const seconds = options.seconds ?? 0;
  const taskInstructions = options.instructions ?? '';
  const telemetry = new Telemetry(diagnostic => report({ diagnostic }));
  const commands = new Commands(options.conversationId, report, options.commandTimeoutMs);
  let model: WebSocket | undefined;
  let connectionStart = 0;
  let modelAudio: ModelAudioWriter | undefined;
  let closing = false;
  let finalized = false;
  let started = false;
  let duration: NodeJS.Timeout | undefined;
  let finalization: NodeJS.Timeout | undefined;
  let inputBytes = 0;
  let outputBytes = 0;
  let transcript = '';
  let inputTranscriptCharacters = 0;
  let outputTranscriptCharacters = 0;
  let answered = !options.waitForAnswer;
  let greetingSent = false;
  const stats = setInterval(() => telemetry.emit('audio_stats', {
    ...telemetry.snapshot(), inputBytes, outputBytes,
    modelQueuedBytes: modelAudio?.queuedBytes ?? 0,
    modelPeakQueuedBytes: modelAudio?.peakQueuedBytes ?? 0,
    gatewayQueuedBytes: gateway.queuedBytes,
  }), 10000);
  const activity = setInterval(() => telemetry.sampleQuiet(), 100);
  const startup = setTimeout(() => fail('startup_timeout'), options.waitForAnswer ? 120000 : 45000);
  telemetry.emit('worker_start');

  function beginConversation(): void {
    if (!started || !answered || closing || greetingSent) return;
    greetingSent = true;
    clearTimeout(startup);
    if (seconds > 0) duration = setTimeout(close, seconds * 1000);
    if (options.waitForAnswer)
      model!.send(
        JSON.stringify({
          type: 'session.commentary.append',
          delegation_id: null,
          content:
            managed ? 'The caller has answered. Introduce yourself as an AI assistant and begin the telephone task. Do not claim the task is complete without evidence.' : 'The caller has answered this authorized telephone audio test. Speak now in Mandarin Chinese. Introduce yourself as an AI voice assistant connected through a Bluetooth phone gateway. Ask whether they can hear you clearly, then listen and have a short conversation. Do not make other calls or claim tool actions.',
        })
      );
  }

  function close(): void {
    if (closing) return;
    closing = true;
    commands.close();
    clearTimeout(startup);
    clearTimeout(duration);
    clearInterval(stats);
    clearInterval(activity);
    telemetry.flush();
    telemetry.stage('input_queue_clear', 0, modelAudio?.queuedBytes ?? 0);
    modelAudio?.close();
    telemetry.flush();
    options.onClosing();
    void gateway.close();
    if (started && model?.readyState === WebSocket.OPEN) {
      model.send(JSON.stringify({ type: 'session.close' }));
      finalization = setTimeout(() => {
        options.onFailure();
        report({ incompleteFinalization: true });
        model?.terminate();
      }, 15000);
    } else model?.terminate();
  }
  function fail(code: string): void {
    telemetry.emit('bridge_failure', { failureCode: code });
    options.onFailure();
    report({ bridgeError: code });
    close();
  }

  gateway.on('failure', fail);
  gateway.on('ended', close);
  gateway.on('outputWritten', (bytes: number, elapsedMs: number, queuedBytes: number) => {
    if (!closing) telemetry.stage('private_audio_send', elapsedMs, bytes, queuedBytes);
  });
  gateway.on('pcm', (pcm: Buffer) => {
    // Startup audio is discarded, not queued into a stale conversation.
    if (!started || !answered || closing || model?.readyState !== WebSocket.OPEN) return;
    telemetry.audio('input', pcm);
    modelAudio?.write(pcm);
  });
  gateway.once('ready', () => {
    if (closing) return;
    report({ scoReady: true });
    telemetry.emit('sco_ready');
    const header = options.authHeader;
    connectionStart = performance.now();
    model = (options.connect ?? ((url, settings) => new WebSocket(url, settings)))(liveUrl, {
      headers: header ? { [header]: key } : { Authorization: `Bearer ${key}` },
      handshakeTimeout: 10000,
      followRedirects: false,
      maxPayload: 2 * 1024 * 1024,
      perMessageDeflate: false,
    });
    modelAudio = new ModelAudioWriter(
      model,
      (bytes, delay) => {
        telemetry.stage("model_input_send", delay, bytes, modelAudio?.queuedBytes ?? 0);
        inputBytes += bytes;
      },
      fail
    );
    model.on('open', () => {
      telemetry.stage('model_connect', performance.now() - connectionStart);
      telemetry.emit('model_connected');
      model!.send(
        JSON.stringify({
          type: 'session.start',
          session: {
            model: 'gpt-live-1',
            store: false,
            instructions:
              taskInstructions || 'You are a concise telephone voice assistant. State that you are an AI assistant. Speak the same language as the caller. Do not claim to perform actions you have not performed.',
            audio: { format: { type: 'audio/pcm', rate: 24000 }, output: { voice } },
            delegation: {
              type: 'responses',
              responses: { model: options.backendModel?.trim() || 'gpt-6-luna' },
            },
          },
        })
      );
    });
    model.on('message', raw => {
      try {
        const event = z
          .object({ type: z.string() })
          .passthrough()
          .parse(JSON.parse(raw.toString()));
        telemetry.observe(event);
        if (commands.observe(event)) return;
        if (event.type === 'session.started' && !closing) {
          telemetry.stage('model_session_setup', performance.now() - connectionStart);
          started = true;
          if (answered) clearTimeout(startup);
          report({ sessionStarted: true });
          beginConversation();
          if (options.testGreeting)
            model!.send(
              JSON.stringify({
                type: 'session.commentary.append',
                delegation_id: null,
                content:
                  'Speak immediately. Say exactly: Raspberry Pi phone audio is connected. Then pause and listen.',
              })
            );
        } else if (event.type === 'session.output_audio.delta' && !closing) {
          if (!answered) return;
          const delta = z
            .string()
            .regex(/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/)
            .parse(event.delta);
          const pcm = Buffer.from(delta, 'base64');
          telemetry.audio('output', pcm);
          const handoff = performance.now();
          gateway.send(pcm);
          if (closing) return; // A transport can reject overload through its failure event.
          telemetry.stage("model_output_handoff", performance.now() - handoff, pcm.length, gateway.queuedBytes);
          outputBytes += pcm.length;
        } else if (event.type === 'session.input_transcript.delta' || event.type === 'session.output_transcript.delta') {
          // Text must never fail or wait for the primary audio session.
          try { options.onTranscript?.(event); } catch { /* The private text sink is optional. */ }
          if (typeof event.delta === 'string') {
            if (event.type === 'session.input_transcript.delta') inputTranscriptCharacters += event.delta.length;
            else {
              outputTranscriptCharacters += event.delta.length;
              if (options.testGreeting) transcript = (transcript + event.delta).slice(-1000);
            }
          }
        } else if (event.type === 'session.closed') {
          finalized = true;
          closing = true;
          commands.close();
          clearTimeout(startup);
          clearTimeout(duration);
          clearTimeout(finalization);
          clearInterval(stats);
          clearInterval(activity);
          telemetry.flush();
          modelAudio?.close();
          options.onClosing();
          void gateway.close();
          const matched = /raspberry pi phone audio is connected/i.test(transcript);
          const callAudioObserved = answered && started && inputBytes > 0 && outputBytes > 0;
          telemetry.emit('session_end', { ...telemetry.snapshot(), inputBytes, outputBytes, finalized: true,
            modelPeakQueuedBytes: modelAudio?.peakQueuedBytes ?? 0 });
          report({
            sessionClosed: true,
            reason: event.reason,
            inputBytes,
            outputBytes,
            modelAudioPeakQueuedBytes: modelAudio?.peakQueuedBytes ?? 0,
            ...(options.waitForAnswer
              ? { inputTranscriptCharacters, outputTranscriptCharacters, callAudioObserved }
              : {}),
            ...(options.testGreeting ? { expectedOutputMatched: matched } : {}),
          });
          if (
            event.reason === 'connection_lost' ||
            (options.waitForAnswer && !callAudioObserved) ||
            (options.testGreeting && (!matched || !outputBytes))
          )
            options.onFailure();
          model!.close();
        } else if (event.type === 'error') fail('model_api');
      } catch {
        fail('invalid_model_event');
      }
    });
    model.on('error', () => fail('model_connection'));
    model.on('unexpected-response', (_request, response) => {
      response.resume();
      fail(`model_http_${response.statusCode}`);
    });
    model.on('close', () => {
      closing = true;
      commands.close();
      clearTimeout(startup);
      clearTimeout(duration);
      clearTimeout(finalization);
      clearInterval(stats);
      clearInterval(activity);
      telemetry.flush();
      modelAudio?.close();
      options.onClosing();
      void gateway.close();
      if (!finalized) {
        telemetry.emit('session_end', { ...telemetry.snapshot(), inputBytes, outputBytes, finalized: false,
          modelPeakQueuedBytes: modelAudio?.peakQueuedBytes ?? 0 });
        report({ incompleteFinalization: true });
        options.onFailure();
      }
    });
  });
  if (options.waitForAnswer) {
    gateway.on('callState', (state: string) => {
      if (closing) return;
      if (state === 'ended') { close(); return; }
      if (state === 'active' && !answered) {
        answered = true;
        report({ callAnswered: true });
        telemetry.emit('call_answered');
        beginConversation();
      }
      // Unknown only concerns control observation. Never stop healthy audio for it.
    });
  }
  return {
    close,
    command(command: { conversationId: string; id: string; content: string }): void {
      commands.send(command, model, started && !closing);
    },
  };
}
