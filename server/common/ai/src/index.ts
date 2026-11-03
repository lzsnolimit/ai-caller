import { parseArgs } from 'node:util';
import { z } from 'zod';
import { GatewayClient } from './gateway.js';
import { createInterface } from 'node:readline';
import { createAISession } from './session.js';
import { PrivateAudio } from './private-audio.js';
import { validateVoice } from './voices.js';
import { transcriptWriter } from './transcripts.js';

const { values } = parseArgs({
  options: {
    gateway: { type: 'string', default: 'http://raspberrypi.local:8765' },
    address: { type: 'string' },
    seconds: { type: 'string', default: '0' },
    'test-greeting': { type: 'boolean', default: false },
    'call-test': { type: 'boolean', default: false },
    status: { type: 'boolean', default: false },
  },
});
const seconds = z.coerce.number().finite().min(0).parse(values.seconds);
const gateway = process.env.PHONE_AUDIO_FD ? new PrivateAudio(Number(process.env.PHONE_AUDIO_FD)) : new GatewayClient(values.gateway!);
let droppedLogs = 0;
const report = (event: Record<string, unknown>): void => {
  if (event.diagnostic && process.stdout.writableLength > 65536) { droppedLogs++; return; }
  if (event.diagnostic) Object.assign(event.diagnostic, { workerDroppedLogs: droppedLogs });
  process.stdout.write(JSON.stringify(event) + '\n');
};
const managed = process.env.PHONE_MANAGED === '1';
const input = managed ? createInterface({ input: process.stdin, crlfDelay: Infinity }) : undefined;
const messages = input?.[Symbol.asyncIterator]();
let taskInstructions = '';
let voice = 'marin';
if (messages) {
  const first = await messages.next();
  const init = z.object({ type: z.literal('init'), task: z.object({ purpose: z.string(), outcome: z.string(), content: z.string(), systemPrompt: z.string(), voice: z.string().optional() }) }).parse(JSON.parse(first.value ?? ''));
  voice = validateVoice(init.task.voice);
  taskInstructions = `${init.task.systemPrompt}\nTelephone task purpose: ${init.task.purpose}\nDesired outcome: ${init.task.outcome}\nTask reference:\n${init.task.content}`;
}

if (values.status) {
  if (!(gateway instanceof GatewayClient)) throw new Error('Status requires the network adapter.');
  report({ capabilities: await gateway.request('/v1/capabilities') });
} else {
  const key = process.env.OPENAI_API_KEY;
  const liveUrl = process.env.OPENAI_LIVE_URL;
  if (!key || !liveUrl)
    throw new Error('Use run-gpt-live.py or set model environment variables.');
  const parsed = new URL(liveUrl);
  if (
    parsed.protocol !== 'wss:' ||
    parsed.username ||
    parsed.password ||
    parsed.search ||
    parsed.hash
  )
    throw new Error('Model URL must be a plain WSS URL.');

  const text = transcriptWriter(managed ? process.env.PHONE_TRANSCRIPT_FD : undefined);
  const session = createAISession(gateway, {
    key, liveUrl, managed, voice, instructions: taskInstructions, seconds,
    authHeader: process.env.OPENAI_LIVE_AUTH_HEADER, backendModel: process.env.OPENAI_BACKEND_MODEL,
    conversationId: process.env.PHONE_CONVERSATION_ID,
    waitForAnswer: values['call-test'], testGreeting: values['test-greeting'], report,
    onFailure: () => { process.exitCode = 1; },
    onTranscript: event => text?.append(event),
    onClosing: () => { input?.close(); process.stdin.pause(); },
  });
  process.once('beforeExit', () => text?.close());
  process.on('SIGINT', session.close);
  process.on('SIGTERM', session.close);
  if (messages) void (async () => {
    for await (const line of { [Symbol.asyncIterator]: () => messages }) {
      try {
        const command = z.object({ type: z.literal('command'), conversationId: z.string(), id: z.string().regex(/^[a-f0-9]{32}$/), content: z.string().min(1).max(4096) }).parse(JSON.parse(line));
        session.command(command);
      } catch { report({ commandRejected: true }); }
    }
  })();
  if (gateway instanceof GatewayClient && values['call-test']) {
    const callId = z.string().regex(managed ? /^[A-Za-z0-9_]+$/ : /^call[0-9]+$/).parse(process.env.PHONE_TEST_CALL_ID);
    gateway.watchCall(managed ? process.env.PHONE_CONVERSATION_ID : undefined, values.address, callId);
  }
  try { await gateway.openAudio(values.address!); }
  catch { gateway.emit('failure', 'gateway_startup'); }
}
