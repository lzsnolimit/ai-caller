English | [简体中文](README.zh-CN.md)

# Phone Gateway Server

The server owns AI, tasks, prompts, devices and phone control. `common/` is the shared service. `pi/` is the Linux adapter. `mac/` is the Mac adapter.

The server connects to the phone and runs an internal Node.js Live worker. The model key stays only in the server environment. The client supplies only the management UI. Phone control uses native HFP, not ADB or phone UI automation.

**Single line:** The server has one managed call at a time. Many phones can pair, stay connected and wait. The number of paired phones and the number of calls are managed separately. For the reason, see [Bluetooth multi-line exploration](../docs/bluetooth-multiplexing-exploration.md). For the lifecycle, see [Architecture](../docs/architecture.md).

`python -m server` selects Linux or Mac. Windows is not supported. For the Mac controller, see [Mac notes](mac/native/README.md). For the models, see [AI configuration](common/ai/README.md).

## Install and start

For a persistent Linux service, see [Linux deployment](pi/deploy/README.md). The steps below start the server manually from the repository root.

You need Python 3.11+ and Node.js 22.13+. Linux also needs BlueZ, PipeWire/WirePlumber, and `pw-record`, `pw-play` and `pw-link`. Phone control needs native PipeWire Telephony. A tested setup uses system PipeWire 1.2.7 with a separately built PipeWire 1.4.11 BlueZ SPA. PipeWire 1.2.7 alone is not supported.

On Linux, pair the phone with the system Bluetooth tools first. Start the gateway as the same Linux user that runs PipeWire. Its D-Bus user session must have access to `org.pipewire.Telephony`. The system bus must have access to BlueZ.

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
npm --prefix server/common/ai ci
export OPENAI_API_KEY=<your-key>
.venv/bin/python -m server
```

The default address for the API and the same-origin UI is `127.0.0.1:8765`. To serve a trusted LAN on Linux, set an address that you checked:

```sh
.venv/bin/python -m server --host raspberrypi.local --port 8765
```

This API has no authentication. Do not expose the port to the internet or to an untrusted network. Any device that can reach the port can control the phone, receive audio and read saved conversations. The control lease only prevents conflicts between clients. It is not a permission check. The gateway does not log phone numbers, audio or access. The full transcript of each conversation (outbound and inbound) is saved to disk. See [Persistence and privacy](#persistence-and-privacy).

## Public API

### Console API

- `GET /v1/console`, `WS /v1/console/events`: a snapshot of devices, capabilities, pairing, conversations (current and history) and the single line.
  - `line`: `{"state":"idle|busy|recovering","conversationId":...,"address":...,"reason":...,"audioReady":false}`. `busy` means that a call uses the line. `recovering` means that the local resources of the last call are not confirmed as released. `reason` gives the cause.
  - `devices[i].recovering`: `true` means that the phone's call state needs a check, and the phone is isolated for now. When the check shows that the call ended, the flag clears automatically. Other phones are not affected.
  - `devices[i].inboundPrompt`: `{"configured":true|false}`. This shows if the phone has an inbound prompt.
  - `conversations[i].direction`: `"outbound"` or `"inbound"`.
  - The snapshot does not contain `capacity` or `audioLines`.
- `POST /v1/discovery`: scan.
- `POST /v1/pairings`: `{"address":"AA:BB:CC:DD:EE:FF"}`. `GET/POST/DELETE /v1/pairings/{id}`: read progress, send the confirmation that the request needs, cancel. Do not mix numeric comparison, passkey and PIN.
- `POST /v1/management/{address}/connect|disconnect|remove`: manage the target phone. The server rejects conflicting operations while a call, a pairing or a lease is active.
- `POST /v1/conversations`: address, number and `task:{purpose,outcome,content,source}`. The server saves the file first, then dials natively and starts Live. On rejection, the server does not dial and does not queue:
  - Line busy: 409 `line_busy`.
  - Line recovering: 503 `line_recovering`.
  - Target phone call state needs a check: 409 `device_recovering`.
  - Target phone already has a call: 409 `phone_busy`.
- `GET /v1/conversations`, `GET /v1/conversations/{id}`: conversation list and detail with the task snapshot. Items contain `direction`, `number` (the dialed number or a numeric caller ID, `null` when unknown), `answeredAt` and `endedAt` (Unix seconds, `null` if not yet). The list includes saved history (at most the latest 256 at startup). Filters: `?direction=outbound|inbound` and `?address=AA:BB:CC:DD:EE:FF`. Invalid values return 400 `invalid_direction` or `invalid_address`.
- `GET /v1/conversations/{id}/transcript`: the saved full transcript `{"type":"transcript.record","conversationId","direction","state","complete":bool,"events":[{"seq","role":"caller|ai","eventId","text","startMs","endMs"}]}`. For a conversation in progress, it returns the part written so far (`complete:false`). Live updates use `WS .../transcript/events`.
- `GET/PUT /v1/devices/{address}/inbound-prompt`: the inbound prompt of each phone. The PUT body is `{"content":"..."}`, UTF-8, at most 16 KiB. GET returns `{"address","content","default":bool,"guard":"..."}`. `default: true` means not configured, and `content` is the default text. A PUT with blank content restores the default. `guard` is the full read-only guard prompt. Errors: 400 `invalid_inbound_prompt`, 404 `device_not_found`, 503 `inbound_prompt_save_failed`. When you remove a device pairing, its inbound prompt is also removed.
- `POST /v1/conversations/{id}/commands`, `POST .../end`: text instructions for the current conversation, and end.
- `WS /v1/conversations/{id}/monitor`: the first byte 0/1 means phone/AI, then 24 kHz PCM. This is a separate copy and does not use the main audio lease.
- `GET/PUT /v1/system-prompt`: `{"content":"..."}`. It applies only to the next task.

States: `dialing/answering/starting/active/ending/ended/failed/cleanup_failed`. `answering` means that an inbound call holds the line and the server waits for the phone to confirm the answer. Each dial or answer creates a new conversation and task. At the end, one cleanup path runs. Recovery only checks state. It does not redial and does not hang up blindly. A server restart does not resume conversations in progress. It loads only the records of saved conversations (not transcript text). A conversation that the last process did not clean up loads as `failed` with `error` `server_restarted`. The end of a task does not mean that the goal is complete. A sent instruction does not mean that the AI did it.

### Inbound auto-answer

1. The server follows the call state of each connected phone. When a phone has a ringing call (`state:"incoming"`), the server continues only if the line is idle, the phone does not need a call-state check, and no low-level control lease is active.
2. After about 3 seconds, the server checks again. Only if the same call still rings and the line is still idle does the server take the line and create an inbound conversation (`direction:"inbound"`).
3. The server sends the answer command. The state is `answering`. The phone's call state decides the result: when the phone reports that the call is active, the answer succeeded. The AT or D-Bus reply does not decide it. If the call is not active within a limited time, the server cleans up as an answer failure (`failed`, `endReason` `answer_failed`): if the call still rings, the server does not hang up or reject, and releases the line. If the call is active, it hangs up by call identity. If it cannot read the phone state, it isolates the phone and waits for a check, and does not send hang-up.
4. After the call is active, the server starts the AI. Lifecycle, hang-up and recovery rules are the same as for outbound calls.

Cases that the server does not handle: when the line is busy or recovering, or the phone needs a check, the gateway does not answer, reject or hang up. The phone handles the call (for example, it rings or goes to voicemail). If the caller hangs up during the wait, or the owner answers on the phone, the gateway stops and does not take the line.

The inbound task: `systemPrompt` is the shared System Prompt plus the guard prompt. `purpose` and `outcome` are fixed text (ask politely and record who the caller is and why they call; do not answer questions; do not disclose our information). `content` is the inbound prompt of the phone, or the default text if not configured. The task snapshot is saved to `tasks/` with `direction:"inbound"`.

### Guard prompt

The guard prompt is the fixed file `server/common/inbound-guard-prompt.md`. The server loads it at startup. No API can change it. The client shows it read-only. Summary: the AI only asks who is calling, what they want and if they need a call back, and repeats it to confirm. It does not answer questions. It does not disclose the owner's name, location, schedule, number or relationships. It can say that it is an AI phone assistant, but it does not disclose the system, model or prompt. If the caller pretends to be a known person, says that it is urgent, or asks the AI to ignore the rules, the AI politely refuses and goes back to the purpose. It makes no promises, payments or identity confirmations. A device inbound prompt can only add to this scope. If they conflict, the guard prompt has priority.

The guard prompt decreases the risk of leaks. It cannot guarantee that the model never makes a mistake under all kinds of manipulation.

### Persistence and privacy

Each conversation, outbound and inbound, is saved under `conversations/<id>/` in the data directory (default `~/.local/share/ai-caller/`, change it with `PHONE_DATA_DIR`):

- `meta.json`: conversation ID, direction, phone address, the dialed number or numeric caller ID, create/answer/end times, state, end reason, cleanup result, error, task ID, voice and audio byte counts. The server replaces the whole file when the state changes and at cleanup.
- `transcript.jsonl`: the full transcript, appended line by line (role `caller`/`ai`, text, start/end times, event ID). The in-memory cache limit does not apply.
- `call.wav`, only when `PHONE_RECORD_CALLS=1`: stereo PCM16 at 24 kHz. The left channel is the phone capture and the right channel is the AI playback. Silence fills the time when one side is quiet, so both channels stay on the same clock. The file is not created when the call has no audio. After the call ends, `GET /v1/conversations/{id}/recording` returns this file. The console plays it from the call detail.

The conversation record stores the number that was dialed, or the caller number when the phone reports a numeric caller ID. Transcripts, task files and logs do not contain the number. The device call list and phone-state events do not contain numbers or names. Call audio is not saved unless `PHONE_RECORD_CALLS=1`. A write failure only records a diagnostic and does not affect the call. The saved files contain conversation content and are sensitive. There is no automatic cleanup and no delete API. An administrator must manage the data directory manually.

### Compatible low-level API

These endpoints serve older callers and the internal worker. The managed entry rejects dialing without a task. For outbound calls, use `/v1/conversations`. The low-level API has one global lease. While a managed call holds the line, `POST /v1/lease` returns 409 `controller_busy`. The AI worker sends and receives audio through a private file descriptor (`PHONE_AUDIO_FD`) and does not use the low-level lease. `X-Controller-Lease` prevents conflicts. It is not authentication.

All responses are JSON. The HTTP error format is `{"error":{"code":"..."}}`. The Bluetooth address format is `AA:BB:CC:DD:EE:FF`.

- `GET /v1/capabilities`: returns `bluez`, `nativeTelephony`, `phones`, `audio` and `control`. `phones[].methods` are the methods that the real object supports. Without a mapping for the target phone, it does not claim that the phone can dial.
- `GET /v1/devices`: returns `devices` with `address`, `paired` and `connected`.
- `GET /v1/devices/{address}/calls`: returns `calls` with `id`, `state` and `multiparty`. It does not return numbers or names.
- `POST /v1/lease`: get the single control lease. Returns `{"lease":"...","expiresIn":30}`. If a lease exists, returns HTTP 409.
- `PUT /v1/lease`: renew for 30 seconds. Needs the `X-Controller-Lease` header.
- `DELETE /v1/lease`: release. Needs the same header. The server closes the related audio and waits for cleanup before a new client can get the lease.
- `POST /v1/devices/{address}/connect`, `disconnect`: native BlueZ connection control.
- `POST /v1/devices/{address}/calls`: dial. The JSON is `{"number":"+123456"}`. The number is used only in memory and in the D-Bus call. Some versions return `{"id":"call1"}`. Versions with no return value return `{"ok":true}`; then read `calls`.
- `POST /v1/devices/{address}/calls/{callId}/answer`, `hangup`: answer or hang up the selected call. You can only use call IDs that this phone reported. The server answers managed inbound calls automatically, so you do not need this endpoint for them.
- `POST /v1/devices/{address}/tones`: DTMF. The JSON is `{"tones":"123#"}`.

All `POST` phone control operations need `X-Controller-Lease`. For each operation, the server finds the real phone object from the current address property. It does not assume that `ag1` belongs to the target phone.

### Audio

`WS /v1/audio?address=AA:BB:CC:DD:EE:FF` needs `X-Controller-Lease`. Only one audio client is allowed at a time, and not at the same time as a managed call. Renew about every 10 seconds. On expiry or disconnect, the server cleans up this audio. A managed conversation cleans up the call. Closing low-level audio alone does not hang up.

Wait for the text event `{"type":"audio.ready"}` before you send binary frames. The server sends ready only after the SCO ports of the selected phone connect in both directions. The wait is at most 45 seconds. It can time out if the call does not use Bluetooth audio yet.

Binary frames in both directions are PCM16, little-endian, mono, 24000 Hz, with no container header. Each frame is at most 960 bytes (20 ms). A frame must not be empty or contain half a sample. Input frames can be shorter, with an even byte count.

At audio start, the gateway reads `pw-record --help` and `pw-play --help` once. If the tools support `--raw`, the gateway uses it to select the raw stdin/stdout PCM path. PipeWire 1.4.9/1.4.11 needs this option; `--format s16` alone does not select raw mode. The help of PipeWire 1.2.7 tools has no `--raw`, so the gateway keeps the older tested command. This check does not run per frame. Newer tools still need real-device checks; command construction tests cannot replace them.

Each application direction queue holds at most 10 frames (200 ms). When the queue is full, the server waits at most 200 ms so that normal output bursts can play. On a block timeout, it sends `{"type":"error","code":"audio_backpressure"}` and closes. It does not drop speech silently. WebSocket write buffers and OS/PipeWire buffers add more delay. This queue limit is not a total latency guarantee.

The gateway requests a PipeWire node latency of `--latency 20ms`. PipeWire 1.2.7 and 1.4.x support it. It does not guarantee audio without gaps. Lower latency can also cause underruns. A bad audio format returns `invalid_pcm_frame`. If the framework rejects a message that is too large, it can close with WebSocket 1009. When SCO ends, the server sends `{"type":"audio.closed"}`. For other errors it sends `{"type":"error","code":"..."}` and closes.

### State events

`WS /v1/events` sends text events `{"type":"phone.state","devices":[...],"phones":[{"address":"...","calls":[...]}]}`. Connection and phone state events cause a snapshot. There is no timed phone polling. If the server cannot read the state, `calls:null` means unknown, not ended. Events contain no numbers or names.

The management client uses the HTTP API for pairing, connection, dialing and instructions. `WS /v1/console/events` pushes management state. `WS /v1/conversations/{id}/monitor` supplies a copy of the audio of both sides. If the UI state connection drops, it reconnects. This does not end the call in the background.

The AI worker uses `WS /v1/conversations/{id}/events` to receive `{"type":"call.state","state":"active","lastKnownState":"active","revision":1}`. Each connection first gets the current snapshot, then the changes. The compatible `GET .../call-state` reads only the server cache. It does not query the phone.

The Mac uses HFP `call/callsetup/callheld` notifications. Linux uses the PipeWire Telephony D-Bus objects and property signals. At connection, on state notifications and during recovery, the server checks the current call identity. After a successful check, it does not poll. If one event check fails, the server tries again at most two times, then keeps `unknown` and waits for a new event. It does not close healthy voice or AI. A real end, a change of call identity and a real audio link failure each follow their own cleanup path. WebSocket heartbeats and control lease renewal are not phone state queries.

## Tests and limitations

The full offline check is `python3 scripts/check.py` at the repository root. First install `server/requirements-test.txt` and the AI npm dependencies. It runs the shared, Mac, launcher, real-worker stand-in, type and client syntax checks. See the root README. The shared suite below alone does not show that all checks pass.

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s server/tests -v
```

The tests use only fake phone adapters and fake subprocesses. They do not make real calls. They cover read endpoints without authentication, the control lease, address mapping, unsupported capabilities, the single audio owner, PCM format, backpressure and cleanup.

An earlier Linux version passed real-device checks on a Raspberry Pi with an Android phone and an iPhone: dialing a number, answer state, hang-up, and 90 seconds of two-way audio data from a local Live client through the API. The iPhone needs the HFP AG routing rule in the persistent configuration. Without it, the call stream connects to local playback and capture on the Pi. Locked-screen dialing and dual SIM are not tested. Network, Bluetooth codec, model response time and system load still affect end-to-end delay.

### Many phones

Many phones can connect at the same time. Phone control first finds the native object by Bluetooth address. A call ID such as `call1` is valid only for that phone. It is not a global ID. Audio connects only the selected address and the private PCM node of this call. Do not change it to match all Bluetooth audio ports.

Only one call is active at a time. There is no setting for the number of call lines. Events and errors of successive calls and of different phones are isolated by conversation and generation. An old event cannot change a new call. On the Mac, the gateway keeps only one target connection at this time.

API reference: [PipeWire Telephony](https://gkiagia.gr/2025-02-20-pipewire-telephony/).
