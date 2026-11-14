English | [简体中文](README.zh-CN.md)

# Web Console (Client)

The client is a separate HTML/JS management UI. The AI and the model credentials are in `server/common/ai/`, not in the browser.

The server can serve the client at `/` (same origin). You can also serve it as a separate static site. The client uses only the HTTP/WS API. It does not control Bluetooth, ADB or the model directly.

## Run

```sh
python3 client/run.py
```

Run it from the repository root. Open `http://127.0.0.1:5173/` and enter the server address on the page. The default is the local port 8765. If the server uses a different port, change the address. After the server disconnects, click Connect to read the snapshot again.

To serve the client from a different origin, add that origin to `PHONE_CLIENT_ORIGINS` on the server. `localhost` and `127.0.0.1` on port 5173 are allowed by default. If a LAN host name does not resolve, check the LAN IP address.

## Interface

One page with three tabs: Phones, Calls and Call instructions. The line indicator is always at the top of the page: idle (green), in a call (amber, breathing animation) and recovering (red). Next to it are the phone of the current call, outbound/inbound, the phase, a live timer, the recovery reason, and "View this call / Hang up". With `prefers-reduced-motion`, the animation does not play.

- **Language:** The page has a Chinese/English switch. UI text is in `assets/messages.js`.
- **Theme:** A light paper theme and a dark night theme follow the system `prefers-color-scheme`. `?theme=dark|light` forces a theme for preview.
- **Offline:** The page uses only system fonts and inline resources, and works offline.

The address bar supports `#devices`, `#calls`, `#prompt`, `#calls/<conversationId>`, `#devices/pair` and `#devices/inbound/<address>`. They open the related content directly.

The console shows a readable message for each server error code (see `assets/labels.js`). For an unknown code, it shows "Operation failed (code)".

## Features and limits

- **Phones:** One row for each phone. The title shows the Bluetooth name `name` if available (on the Mac, only after the first connection). If there is no name, or an older server does not send it, the title shows the address. The address shows once in small monospace text. A one-line state: ready to dial and answer, connected but call function not ready, paired but not connected, call state needs a check, or in a call. Main button: not connected > Connect, connected > Dial, in a call > View call. Disconnect, Inbound greeting and Remove pairing are in the "⋯" menu of the row. Tags show a custom or default inbound greeting (hidden if an older server has no `inboundPrompt`) and a non-default voice.
- **Add phone:** Scan, enter an address and start pairing. Numeric comparison, passkey/PIN entry and cancel are all available.
- **Inbound greeting:** `GET/PUT /v1/devices/{address}/inbound-prompt`, UTF-8, at most 16 KiB, checked locally first. The system guard rules show folded and read-only. A save affects only later inbound calls.
- **Call history:** "Current" is at the top. The other records follow, newest first. Each item shows ↗ outbound / ↙ inbound, the phone name, the dialed or caller number when the server has one, the start time (time only for today), the duration (from answer to end, live while in progress) and the result (for example: hung up, other party hung up, phone did not confirm answer, not dialed, cleanup needs a check; see `labels.js`). If an older server has no `direction`, the item shows as outbound. If it has no `line`, the console infers the line from the conversation list and does not disable dialing. If it has no `number`, the row omits the number.
- **Call detail:** Title, the call's phone number, timeline and result. While in progress: live conversation (bubbles), instructions to the AI, listen (other party and AI volume), task, and saved conversation (button). After the end, the console reads the saved full conversation automatically (`GET /v1/conversations/{id}/transcript`). If `conversations/<id>/call.wav` exists, the detail shows a player for that recording (`GET /v1/conversations/{id}/recording`). Each finished call in the history also has a Listen button. Adjacent parts of the same speaker merge into one bubble. A conversation loaded from disk without in-memory detail shows only the record and the conversation.
- **Dial:** Phone, AI voice (preview/stop), number, purpose, expected outcome, task details, and load from file (UTF-8 `.txt/.md`, at most 64 KB). When the line is not idle, the form is disabled and shows the reason. If the selected phone needs a check, the form shows a warning but stays enabled.
- **Call instructions (shared System Prompt):** Saved on the server. It applies to the next call and does not change older tasks.
- When you change the conversation, the old listen session stops and the cache clears. Instruction drafts stay per conversation. Closing the page does not end the call on the server.

## Listen and instructions

Listen uses Web Audio to play a copy of the 24 kHz PCM. It does not capture the microphone and does not send audio back into the call. The server's listen queue is separate. A slow browser can lose copies or be disconnected from listening.

"Send" does not mean that the AI did the instruction. The instruction state shows: received by the server, written to the model connection, injection confirmed by the model, rejected, or result unknown. It also shows the command ID and the update time. An injection receipt does not prove that the model said the target words or that the other party heard them. The client keeps a local limit of 4096 characters for one non-empty instruction, with no truncation. The model has a separate limit of 500 tokens. If the model returns a related rejection, the console marks only that instruction as rejected and does not end the call. An unknown result is not sent again automatically. The detail title can still show an old `starting`. Use the live list and the line state at the top to see the real state.

The first listen session can have noise; the cause is not known. Listening to both sides, the effect of instructions, and switching between the current call and history on real devices are not fully tested.

There is no authentication. Use the API only on a trusted network. Do not write model data, phone numbers or task content to ordinary logs. The bounded queues of the main call do not guarantee zero delay. The short buffer of browser listening is not the delay between the phone and the model.

The AI CLI and its tests are in [server/common/ai](../server/common/ai/README.md). The client has no npm dependencies.

## Voices

The dial form offers 13 AI voices with project samples. You can select one and click to preview it. The default is `gleam`. A voice affects only the next call. The detail shows the voice in the task snapshot. Saved project samples play directly and do not call a cloud service. Only a missing sample needs the server to generate short audio, which can cost Live usage and take time. "Stop preview" stops playback and the wait on this page. The language/region in the list describes the voice style. It does not limit the language of the conversation. The samples are fixed short English sentences and can be cut off. They do not show the pronunciation of all languages. A preview does not dial or change a call.

When you select a voice, it is saved immediately as the default of the selected paired device. You do not need to dial or connect HFP. The server keeps it only in the current process. After a page refresh, a device change or in another client, the same selection shows. After a server restart, it goes back to `gleam`. Removing a device clears its selection. Calls in progress and task snapshots do not change. A save failure shows an error. Before the read completes, the page does not write its default back to the server.

## Live transcript

When you select a conversation, the page connects to `/v1/conversations/{id}/transcript/events` of that conversation. This does not start audio listening. The text of the other party and of the AI shows separately, with the original spaces and repeated text. The 5-second grouping and the approximate session time are only for display. They do not mean that a turn ended or that audio played.

The transcript goes from the AI worker to the server through a separate private file descriptor. It does not go to ordinary logs. Live text comes from a bounded memory cache: the server caches at most 64 conversations, and each conversation at most 512 updates or 128 Ki characters. When the cache drops data, the page shows a text gap. A reconnect uses `cacheId` and the `after` sequence number to continue. The server also saves the full conversation to disk, with no cache limit. After a server restart, "Open full transcript" can still read it. The call's phone number is on the conversation record, not in the transcript. Text and audio use different queues. A slow text consumer is disconnected and does not block the two-way audio.
