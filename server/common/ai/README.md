English | [简体中文](README.zh-CN.md)

# Server AI Configuration

The voice model and the reasoning model of a call are separate:

- `gpt-live-1`: receives and generates call audio continuously. The default voice is `marin`.
- `gpt-6-luna`: the default Responses reasoning backend.

Two start methods use the same default: the `run-gpt-live.py` launcher, and the direct Node worker when the model connection environment variables are already set.

## API key and endpoint

You supply your own OpenAI API key. The key is read only from the environment. It is not saved or printed.

- Set `OPENAI_API_KEY` in the server process environment.
- Set `OPENAI_BACKEND_MODEL` to change the reasoning model of calls. The default is `gpt-6-luna`.
- Optional: the launcher can read the Codex configuration `~/.codex/config.toml` (or `$CODEX_HOME/config.toml`). It uses the API provider, `base_url` and key variable name (`env_key`) from that file. The `base_url` must be a plain HTTPS URL. If the file does not exist, the launcher uses the OpenAI default endpoint and `OPENAI_API_KEY`. The Codex `model` value is not used.
- The managed worker uses the `node` binary in `PHONE_NODE`. The Mac launcher sets it to its own `node`. If it is not set, the worker finds `node` in `PATH`.

```sh
cd server/common/ai
npm ci
# The default is gpt-6-luna. You do not need to set a model variable.
python3 run-gpt-live.py --gateway http://127.0.0.1:8765 --address AA:BB:CC:DD:EE:FF
```

The command above only attaches to the audio of an existing call. It does not dial. For the normal console, start `python -m server` from the repository root. The console saves a task before each call.

The project does not set `reasoning.effort` or a service tier. The documented default reasoning effort of GPT-6 Luna is `medium`. The settings that your provider applies need a real check. A different model does not guarantee faster tasks.

## Diagnostic logs

Logs are on by default. They are saved in `PHONE_DATA_DIR/logs/calls.jsonl` on the server. If the data directory is not set, the path is `~/.local/share/phone-call-gateway/logs/calls.jsonl`. Each file is at most about 2 MB, with 3 backups. The directory mode is 700 and the file mode is 600. A background thread writes to disk. When the bounded queue of 256 entries is full, the logger drops diagnostics and does not block the call. `droppedLogs` shows lost entries and write failures. The logs are not task text or call recordings.

Search by `conversationId`. In one process, sort by `sequence` and `elapsedMs`. Across processes, use `timestampMs` only for rough correlation, not for precise delay. Logged items: model connect/ready, answer, backend response start/complete/fail with duration, rough input/output volume activity, byte/buffer statistics every 10 seconds, end state and worker exit.

- `resolvedBackendModel` only shows the configured value. It does not prove a call.
- `observedBackendModel` in `backend_response` comes from the real nested `response.event`. `backendResponseIndex` matches start and end. `observedBackendModels` and `backendCreated/Completed/Failed` are totals.
- `finalized: true` means that the model sent an end state. It does not mean that the task goal is complete. After an abnormal disconnect or with `droppedLogs > 0`, the evidence can be incomplete. A missing event does not prove that no call occurred.
- `audio_activity` is a volume threshold sample, throttled to 100 ms. It is not VAD, an exact end of speech, or the playback time on the phone. `deliveryGap: true` means that no sample came in that direction for at least 300 ms. It does not prove that the phone stopped playback. Do not cut speech automatically on this signal.

The `/v1/conversations` and detail APIs also return a `diagnostics` summary. Only allowed fields are logged. Phone numbers, device addresses, raw model IDs, tasks/prompts, transcripts, audio and keys are not saved. Raw provider error messages are not printed.

References: [GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna), [GPT-Live delegation](https://developers.openai.com/api/docs/guides/live-delegation).

## Shared AI and main audio channel

`createAISession(audio, options)` in `session.ts` does not handle the OS, Bluetooth, dialing, the HTTP service or processes. The host supplies `DuplexAudio`: it emits `ready`, `pcm`, `callState`, `ended` and `failure` events, and implements `send`, `close` and `queuedBytes`. PCM is 24 kHz mono PCM16LE. The returned object accepts text instructions and closes the session. The model, prompt, answer gate and end-state handling use one shared implementation.

The managed entries on Mac and Linux both use `WorkerAudio` through `Conversations.spawn_worker`. Each conversation has its own process, socketpair, line ownership and CallState subscription. Python calls the platform start/read/write/monitor/close directly. The main audio does not go through the public HTTP audio route, WebSocket frame encoding or two layers of API audio queues. One binary copy between processes remains.

The two directions run in different tasks. The private output uses a bounded FIFO. It sends one frame of at most 960 bytes at a time and sends the next frame immediately after the completion callback, with no fixed send wait. Pending PCM (including the frame being written) is at most 48000 bytes, which is one second of audio. If one write does not complete in 2 seconds, it fails. This matches the budget of the model input writer. It absorbs short bursts and does not depend on the momentary socket backlog of the OS. A continuous overload ends the audio clearly. The main PCM is not dropped to look normal. The OS and the platform have their own buffers. These limits do not guarantee end-to-end delay. The client accepts at most 5 copy packets for each listen session. If a send takes more than 100 ms, only that listen session ends. The internal line ownership is released with the process and the audio cleanup. It does not depend on network lease renewal. The public lease cannot take this line.

## How to find delay

`audio_stage` sums each stage every 10 seconds and writes the remaining statistics at the end. Fields: `frames` (count), `bytes`, `meanMs`, `maxMs` and `peakDepth`. They are interval statistics, not percentiles. Node output also has a running `audioSequence`. Backend responses use the anonymous `backendResponseIndex`. All logs belong to a `conversationId`.

| Stage | Measured boundary |
| --- | --- |
| `hfp_capture_process` | Host time on the Mac after it receives SCO PCM: resample, enqueue and prepare return audio |
| `hfp_capture_queue` | Mac capture frame enqueued until common reads it; depth in frames |
| `capture_transport` | common writes to the private channel until local drain returns; depth in bytes; does not mean that Node or the model received it |
| `model_input_send` | Node receives an input frame until the model WS send callback, including the local model send queue |
| `model_connect` / `model_session_setup` | Create model connection until WS open / session.started |
| `model_first_output` | session.started until the first output_audio.delta of the session; includes answer wait, greeting and model wait; not the reasoning time of each turn |
| `backend_response` | One anonymous backend response from created until an end state such as completed/failed, with model event evidence |
| `model_output_gap` | Interval between adjacent model audio deltas; includes normal silence; not automatically a fault |
| `model_output_handoff` | Time for Node to call the audio transport.send after it receives model PCM; not transfer complete |
| `private_audio_send` | Model output enters the private FIFO until the local socket write callback, including the FIFO wait; does not mean that the platform wrote it or the phone played it |
| `playback_handoff` | common receives a private PCM frame until the platform write returns; includes platform backpressure wait |
| `hfp_playback_queue` | Mac converted playback segment enqueued until the SCO callback takes it; depth is remaining 8 kHz bytes |
| `hfp_host_send` | In the Mac SCO callback, prepared output until host.send_sco_sdu returns; not a radio send confirmation or phone playback |
| `hfp_underrun` | Silence bytes that the Mac adds when output is short; can also occur during normal silence |
| `monitor_enqueue/drop/send/failure` | Listen copy enqueue, drop, WS send duration or failure |
| `input_queue_clear` / `playback_queue_clear` | Bytes cleared from the local input/playback queues at close |

Python uses `clockDomain=python_monotonic`. Node uses `clockDomain=node_performance`. In one process, monotonic clocks measure durations. Do not subtract `elapsedMs` values of two processes. `timestampMs` is the wall time when Python received or wrote the metadata. Use it only for rough correlation, not for precise one-way delay across processes. There is no per-frame clock alignment across processes. Do not add the averages above to get an end-to-end time.

These times are not measured: phone capture to host, radio transfer, provider internal reasoning and real playback on the phone. On Linux, common delivery statistics are available. The internal PipeWire queues and the real device send have no added measurement. To measure these times, use synchronized phone timestamps or an external acoustic reference.

There is no separate provider interrupt or clear command. The audio and close behavior stay the same. The project does not claim an interrupt feature or a per-turn response delay. A record of a cleared queue at close is not a confirmation of a model interrupt.

Log writes use the bounded background queue of 256 entries. When the Node stdout backlog is more than 64 KiB, it drops diagnostic records and counts them in `workerDroppedLogs`. Losses in the Python queue or disk writes use `droppedLogs`. The two counts are saved separately. A log output failure or a stalled listen session cannot block the main audio. If all logging fails, the last counts may not be saved. Read the runtime `droppedLogs` together with the saved records.

Real overload of the private output is recorded as `private_audio_queue_full`, `private_audio_send_timeout` or `private_audio_send`. It is not counted as a bad model event format. On the private channel, `model_output_handoff.peakDepth` and `gatewayQueuedBytes` mean pending PCM bytes (including the frame in transfer, not the 5-byte frame header). They are not only the momentary socket `writableLength`.

## Operator instructions during a call

Operator instructions use GPT-Live `session.instructions.append` with `delegation_id=null` and `event_id=operator_<commandId>`. Content that the AI can speak, such as the greeting, still uses commentary. References: [server controls](https://developers.openai.com/api/docs/guides/voice-server-controls) and [Live event reference](https://developers.openai.com/api/reference/resources/live/sideband-websocket/). `session.instructions.appended.client_event_id` confirms that the instruction entered the context. `error.client_event_id` links a single rejection. A confirmation does not guarantee that the model obeys, the exact words or that audio played. The instruction stays in the history of the current conversation and goes into later backend delegation. It does not change the saved task snapshot.

The server registers the command first and then writes it to the worker. The API keeps the ID, `acceptedAtMs`, `updatedAtMs` and the state: `accepted` (received by the server), `written` (written to the model connection), `injected` (injection confirmed by the model), `rejected` and `unknown`. If a send fails, the server may not know if the model received it, so it records `unknown`. The worker waits at most 10 seconds for a receipt. The server waits at most 12 seconds for a missing worker state. At the end, it clears the waits. Late or duplicate receipts do not change the end state and do not cause a resend. A single rejected instruction does not end the call or the two-way audio.

The local input limit is non-empty and at most 4096 characters. It does not estimate tokens from bytes and does not truncate. The documented content limit is 500 tokens, and the provider checks it. When a related error comes back, only that command is marked `rejected`. The call does not end and the command is not resent. Each conversation has at most 128 commands and at most 32 waiting at the same time. More returns `command_limit`. The CLI and the real send use the same 4096-character limit. A write marks only `written`. Only a related receipt marks `injected`.

The `operator_command` metadata records only the conversation, command/event IDs, state, times, delay and a fixed error category. It does not record the instruction text, the task, the transcript or raw provider errors. `durationMs` uses the server monotonic clock. `workerElapsedMs` uses the worker monotonic clock. Do not subtract the start points of different processes. This does not measure if the model obeyed or when the other party heard it. Logs still use the existing bounded non-blocking queue.

## Voice per call

The client reads `GET /v1/voices` before a call. `POST /v1/conversations` can add `"voice": "quartz"`. If it is not given, the server uses the device selection in this process, initially `marin`. The server rejects unknown, empty or wrong-type values before it dials or takes the line. The value goes into the private task snapshot, then through managed init to `createAISession` and `session.start.audio.output.voice`. The voice of an existing call does not change.

Names, languages and regions come from the [GPT-Live voice options](https://developers.openai.com/api/docs/guides/live-conversations#voice-options). The project supports 13 voices that have saved samples: `marin`, `quartz`, `ripple`, `vesper`, `willow`, `stone`, `gleam`, `meridian`, `bossa`, `tempo`, `beacon`, `delta` and `cinder`. The default is `marin`. This is a project subset. The official list has more voices. For other values, the dial, preview and device preference APIs return `invalid_voice` before any side effect. The generation tool uses the same list, and the project sample manifest matches these 13 voices. Language and region describe the voice style. They do not limit the conversation language and do not guarantee a Chinese accent. In the list, `providerVerification` `bundled_sample` means a project sample with a checked format and hash. `preview_succeeded` means that this process generated a sample. `not_verified` means neither. A project sample does not prove the result for your account, all languages or all phones.

`GET/PUT /v1/devices/{address}/voice` reads or updates the default voice of a paired device in the current server process. The PUT body is `{"voice":"quartz"}`. It does not dial, take the line or save permanently. When a call has no voice, the server uses this value. A valid voice in the request has priority. The task snapshot does not change after creation. Removing the device clears the selection. A server restart restores `marin`.

`POST /v1/voices/{id}/preview` returns `audio/wav`. It first reads a checked project sample from `server/common/voice-samples`. This works after a restart, offline, or without a provider configuration, and does not call a cloud service. Only for a missing sample does the server use its credentials to create a separate short `gpt-live-1` session. This costs Live usage and takes network time. It does not create a custom voice, and does not use the phone, HFP, the line lease, the task of the current call or the client microphone. Audio plays only when the user clicks. If the browser blocks playback, the page shows a manual player.

A preview has a total limit of 20 seconds and at most 8 seconds of audio. When continuous audio reaches 8 seconds, the server returns the clip and closes the preview session. It does not wait for the speech to end. After audio starts, if no next chunk comes in 1 second, it closes the session. This is a short-clip policy, not a speech-complete signal from the provider. A preview can contain only part of a sentence. The server generates only one voice at a time. Requests for the same voice share one generation. Other requests get `voice_preview_busy`. The generation cache is only in private server memory. Project samples stay on disk. Each sample is at most 384,044 bytes. After a failure, the server does not try again for 30 seconds. Stopping playback in the client or cancelling the request does not stop a generation that other users share. The background task is still limited to 20 seconds. Server shutdown cancels the generation. Provider rejections, timeouts and audio errors return a clear failure. The server does not change to a different voice. Audio, model keys and raw provider errors do not go into ordinary logs.

To add missing project samples, run `python scripts/generate-voice-samples.py --server http://127.0.0.1:8765` (use the port of your server). The tool sends requests one at a time through the single generation slot of the API. It tries each missing voice once and skips valid samples. Use `--voices quartz` to select targets. Failure details go to `~/.cache/phone-call-gateway/voice-sample-generation.json`. The tool saves only samples of a fixed general preview sentence. It does not read call or task audio. The manifest records the request text, source, hash and duration. It does not guarantee that the model said every word. The tool writes successful samples atomically to the fixed voice files. Normal playback does not change project files. Tests use a private temporary sample directory. `PHONE_VOICE_SAMPLE_DIR` sets this directory, so tests do not read or change the shipped samples. The default voice of a device stays only in the server process and is not saved with the project audio.

Offline tests cover voice transfer from the real server to the managed worker to a local TLS model, and isolation between two sessions. The preview TLS test also checks the two-way PCM of the original call. The stand-in only proves wiring and limits. It does not prove that your API account supports all voices in the list, or the real sound.
