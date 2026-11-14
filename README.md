English | [简体中文](README.zh-CN.md)

# AI Caller

[![CI](https://github.com/lzsnolimit/ai-caller/actions/workflows/ci.yml/badge.svg)](https://github.com/lzsnolimit/ai-caller/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Node.js 22.13+](https://img.shields.io/badge/Node.js-22.13%2B-339933)
![Platforms: macOS | Linux](https://img.shields.io/badge/Platforms-macOS%20%7C%20Linux-lightgrey)

Connect a real mobile phone to an AI voice agent over Bluetooth hands-free (HFP). The AI can place calls for you and answer incoming calls.

A Mac (built-in Bluetooth controller) or a Linux computer such as a Raspberry Pi (BlueZ + PipeWire) acts as a Bluetooth hands-free unit for the phone. The gateway sends the call audio to an OpenAI realtime voice model and sends the model audio back into the call. A web console manages phones, calls, tasks and prompts.

## Main features

- 🤖 **The AI makes calls for you.** Write a task (who to call, what to do, what result you want). The AI dials from your real phone and talks to the other party.
- 📞 **The AI answers calls for you.** About 3 seconds after an incoming call starts to ring, the AI answers. It asks who is calling, what the call is about and if a call back is necessary, and records the answers. It does not answer questions or give out your information.
- 👂 **You stay in control during a call.** Read the live transcript of both sides, listen in the browser, send new instructions to the AI, or hang up.
- 📝 **Every call is saved.** The full transcript of each outbound and inbound call is kept, with the number that was dialed or the caller number when the phone provides one. Call audio is not saved.
- 📱 **Your own phone and number.** The gateway connects to an iPhone or Android phone over Bluetooth hands-free. Calls use the phone's own SIM. No cloud telephony service is necessary.
- 🖥️ **Runs on a Mac or a Raspberry Pi.** A web console manages phones, pairing and calls, in English or Chinese.

One call at a time. Many phones can pair and wait at the same time.

![Web console: line status and phones](docs/images/console.en.png)

> **Security notice:** The API and the web console have no authentication. Use the gateway only on `127.0.0.1` or on a trusted LAN. Do not expose the port to the internet. See [SECURITY.md](SECURITY.md).

## Contents

- [Main features](#main-features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Usage](#usage)
- [Configuration](#configuration)
- [Privacy and data](#privacy-and-data)
- [Status and limitations](#status-and-limitations)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## How it works

```text
Phone SIM call
    ↕ Bluetooth HFP / SCO
server/ —— Linux/Pi or Mac: Bluetooth, phone, tasks, prompts, Live worker
    ↕ HTTP control / WebSocket state and PCM
client/ —— Browser: device and call UI, holds no model credentials

Server ↔ GPT-Live (realtime listen and speak) ↔ GPT-6 Luna (reasoning on demand)
```

- The **server** connects to the phone, owns the single call line, saves tasks and conversations, and starts one AI worker process (Node.js) for each call.
- The **AI worker** streams 24 kHz PCM between the call and the realtime voice model `gpt-live-1`. The voice model can delegate reasoning to a backend model (default `gpt-6-luna`).
- The **client** is a static HTML/JS console. The server serves it at `/`. You can also serve it separately.

For the design, see [Architecture](docs/architecture.md).

## Requirements

If one of these items is missing, the gateway cannot complete a call.

### Hardware

- A mobile phone with a SIM card that supports Bluetooth hands-free (HFP). Android and iPhone can both work.
- A computer to run the gateway. Select one:
  - **Mac** with the built-in Bluetooth controller. Tested on macOS 26.4 with a BCM_4388C2 built-in controller. Other Mac models are not tested. To see your controller, run `system_profiler SPBluetoothDataType | grep -i chipset`.
  - **Linux / Raspberry Pi** with BlueZ and PipeWire (native Telephony). See [Linux deployment](server/pi/deploy/README.md).
- Windows is not supported.

### Software

| Dependency | Version | Purpose |
| --- | --- | --- |
| Python | 3.11+ | Gateway server |
| Node.js | 22.13+ | AI voice worker |
| Xcode Command Line Tools | Mac | Build the native Bluetooth bridge (`xcode-select --install`) |
| blueutil | Mac, `/opt/homebrew/bin/blueutil` | Turn off system Bluetooth while the gateway runs, and turn it on again at exit (`brew install blueutil`) |
| BlueZ, PipeWire, WirePlumber, `pw-record`/`pw-play`/`pw-link` | Linux | Bluetooth and call audio |
| openssl | Tests only | Temporary certificates for the local model stand-in |

### AI key

The gateway uses the OpenAI realtime voice API. This project does not supply an API key. Use your own OpenAI API key. The gateway reads the key only from the environment of the gateway process. It does not write the key to a file and does not send it to the web console.

- Set `OPENAI_API_KEY` before you start the gateway.
- Optional: to use a different OpenAI-compatible endpoint, set the provider, `base_url` and key variable name in the Codex configuration file `~/.codex/config.toml`. The gateway uses these values. The endpoint must be a plain HTTPS URL.
- For the models, see [AI configuration](server/common/ai/README.md).

## Quick start

Run all commands from the repository root.

### Mac

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-mac.txt
npm --prefix server/common/ai ci
.venv/bin/python server/mac/native/build.py
export OPENAI_API_KEY=<your-key>
.venv/bin/python -m server --port 8765
```

Notes:

- While the gateway runs, it uses the built-in Bluetooth controller. **System Bluetooth is turned off.** Bluetooth keyboards, mice and headphones on the Mac disconnect. At a normal exit, the gateway turns system Bluetooth on again.
- The terminal that starts the gateway must be able to run `node`. If you use nvm, run `node -v` in that terminal first. The AI worker uses the same `node` as the gateway.
- On the Mac, the gateway binds only to `127.0.0.1`.
- Pair the phone in the console (Phones > Add phone). A new pairing can replace the phone's existing pairing with the Mac system. See [Mac notes](server/mac/native/README.md).

### Linux / Raspberry Pi

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
npm --prefix server/common/ai ci
export OPENAI_API_KEY=<your-key>
.venv/bin/python -m server --port 8765
```

Notes:

- Start the gateway as the same user that runs PipeWire. The `PATH` of that user must contain `node`.
- The gateway needs the full repository, not only `server/`.
- You must build the BlueZ SPA libraries yourself. This repository contains no binaries. See [Linux deployment](server/pi/deploy/README.md).
- To serve a trusted LAN, add `--host <lan-address>`. The default is `127.0.0.1`.

### Open the console

Open `http://127.0.0.1:8765/`. You can also run the console as a separate static site:

```sh
python3 client/run.py
```

Then open `http://127.0.0.1:5173/` and enter the gateway address on the page. See [client](client/README.md).

## Usage

The console has three tabs: **Phones**, **Calls** and **Call instructions**. The top of the page shows the line state. Use the **中文 / EN** switch at the top right to change the language.

### 1. Add and connect a phone

1. On the phone, open the Bluetooth settings page, so that the phone is discoverable.
2. In the console, open **Phones** and click **Add phone**, then **Scan for phones**.
3. Select the phone and click **Start pairing**. If the phone and the console show the same number, confirm on both.
4. Go back to the list and click **Connect**. When the phone shows **Connected, ready to dial and answer**, you can use it.

On a Mac, disconnect all phones before you scan. You only need to pair once; next time, click **Connect**.

### 2. Make a call

1. On the phone card, click **Dial**.
2. Type the **Phone number**, the **Purpose of the call** and the **Expected result**.
3. In **Task for the AI**, write the full instructions: who the AI speaks for, what to say, what to ask and when to end the call. You can also load a `.txt` or `.md` file.
4. Optional: select an **AI voice**. Click a voice to hear a sample.
5. Click **Dial**. The gateway saves the task, then dials.

Example task:

```text
Call the restaurant and book a table for 2 people this Friday at 7 pm, name "Alex".
If 7 pm is not available, accept any time between 6 pm and 8 pm.
Do not give any other personal information. Say goodbye and end the call when the booking is confirmed.
```

### 3. During a call

Click **View this call** at the top, or **View call** on the phone card.

- **Live conversation:** the text of both sides, 1 to 2 seconds behind the audio.
- **Instructions to the AI:** type a new instruction and click **Send**, for example "Ask for a window table". The AI receives it during the call. Sending does not guarantee that the AI complies.
- **Listen:** click **Start listening** to hear both sides in the browser. Your microphone is not used.
- **Hang up:** ends the call. The AI cannot hang up by itself, so end the call when the task is complete.

### 4. Let the AI answer incoming calls

Incoming calls are answered automatically after about 3 seconds when the line is idle and the phone is connected. If the line is busy, the phone rings normally and you answer it yourself.

To change what the AI says, open the **⋯** menu on the phone card and select **Incoming call greeting**. Each phone has its own greeting. The AI only asks who is calling, what the call is about and if a call back is necessary. It does not answer questions. The read-only **System safety rules** always have priority.

### 5. Find past calls

Open **Calls** > **History**. Each outbound and inbound call keeps its saved conversation text. The files are in the data directory (see [Privacy and data](#privacy-and-data)).

The **Call instructions** tab holds the shared instructions that every outbound call uses, for example the AI's tone and language rules.

## Configuration

Set these environment variables in the environment of the gateway process.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | none (required) | Your OpenAI API key. If `~/.codex/config.toml` sets a different key variable name, the gateway reads that variable. |
| `OPENAI_BACKEND_MODEL` | `gpt-6-luna` | Backend reasoning model for calls. |
| `PHONE_DATA_DIR` | `~/.local/share/ai-caller/` | Data directory for tasks, prompts, conversations and logs. |
| `PHONE_RECORD_CALLS` | unset | Set to `1` to save a private stereo WAV of each call. Any other value leaves call audio unsaved. |
| `PHONE_NODE` | `node` from `PATH` | Node.js binary for the AI worker. The Mac launcher sets it to its own `node`. |
| `PHONE_CLIENT_ORIGINS` | `localhost` / `127.0.0.1` on port 5173 | Extra allowed origins (comma-separated) for a separately served console. |
| `PHONE_MAC_PYTHON` | `python3` | Mac only. Python used by `server/run-mac.cjs`. `python -m server` sets it. |
| `PHONE_MAC_NATIVE_DIR` | `~/.cache/ai-caller/mac-native/` | Mac only. Build output of the native Bluetooth bridge. |

Command-line options of `python -m server`:

- `--host` (default `127.0.0.1`). The Mac accepts only `127.0.0.1`.
- `--port` (default `8765`).

## Privacy and data

The default data directory is `~/.local/share/ai-caller/`. Directories are created with mode 700 and files with mode 600.

The gateway saves:

- Task files and prompts (private JSON).
- For each conversation (outbound and inbound), a record and the full transcript under `conversations/<id>/`. The record includes the number that was dialed, or the caller number when the phone reports a numeric caller ID.
- A call recording, only when `PHONE_RECORD_CALLS=1`. The file is `conversations/<id>/call.wav`: stereo PCM16 at 24 kHz, left channel from the phone and right channel from the AI.
- Diagnostic logs under `logs/calls.jsonl`. The logs do not contain phone numbers, task text, transcripts, audio or keys.

The gateway does not save:

- Call audio, unless `PHONE_RECORD_CALLS=1`.
- The API key.

Transcripts contain conversation content. Treat them as sensitive data. There is no automatic cleanup and no delete API. Remove files from the data directory manually when necessary.

## Status and limitations

This project is experimental.

- **One call at a time.** When the line is busy, a new call returns `line_busy`. When the line is recovering, it returns `line_recovering`. When the target phone's call state needs a check, it returns `device_recovering`. Many phones can still pair and connect.
- **Real-device tests:**

  | Gateway | Phone | Outbound AI call | Inbound auto-answer |
  | --- | --- | --- | --- |
  | Mac, macOS 26.4, BCM_4388C2 | iPhone | Passed | Passed |
  | Mac, macOS 26.4, BCM_4388C2 | Android | Passed | Not tested |
  | Raspberry Pi 4, Debian 12, PipeWire 1.2.7 | Android | Passed | Not tested |
  | Raspberry Pi 4, Debian 12, PipeWire 1.2.7 | iPhone | Earlier version only | Not tested |

- **Mac:** the gateway keeps only one phone connected. The gateway starts the connection; the phone cannot connect to the Mac gateway by itself. Other Mac controllers are not tested.
- **The AI cannot hang up.** End the call in the console or through the API.
- **Not fully tested:** Listening to both sides in the console, the effect of text instructions, and long task calls. The first listen session can have noise; the cause is not known.
- **Audio:** The gateway uses 20 ms frames and bounded queues. This does not guarantee audio without gaps.
- **Prompt safety:** The guard prompt decreases the risk of data leaks. It cannot guarantee that the model always obeys it.
- **No authentication.** See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| A call fails immediately with `audio_peer_closed` | The AI worker did not start: `node` was not found, or the key is missing | Make sure that the terminal can run `node -v` and that the key is set. Find `worker_exit` in `~/.local/share/ai-caller/logs/calls.jsonl`. |
| Start fails with `system Bluetooth did not release the controller` | blueutil is not in `/opt/homebrew/bin/`, or system Bluetooth cannot turn off | Install blueutil. Close apps that use Bluetooth, then try again. |
| On the Mac, the phone shows "not connected" | The Mac gateway does not accept connections that the phone starts. The gateway starts the connection. | In the console, select the phone and click Connect. |
| Dial returns `ai_provider_not_configured` | The gateway process has no API key | Set `OPENAI_API_KEY` in the environment that starts the gateway, then restart it. |
| Dial returns `line_busy` | Another call is in progress. The gateway has one call line. | Wait until the current call ends, or hang it up. |
| Scan finds no phone | The phone is not discoverable | Keep the phone's Bluetooth settings page open during the scan. On a Mac, disconnect all phones first. |
| Linux: Connect returns `phone_operation_timeout`, or `bluetoothctl connect` says `Operation already in progress` | bluetoothd is stuck on an earlier connection attempt | Run `sudo systemctl restart bluetooth`. If it continues, connect once from the phone's Bluetooth settings. |
| An incoming call is not answered | The line is busy, the phone is not connected, or the phone is not ready for calls | Check that the phone shows **Connected, ready to dial and answer** and that no other call is active. |
| A call connects but the AI is silent | The AI worker cannot reach the model | Check the key and the network. Look for `operation_failure` in `logs/calls.jsonl`. |

## Testing

All offline checks use one command. The tests do not turn on Bluetooth, read model keys or make phone calls. The real-worker tests use a local TLS model stand-in. They need `openssl` and write temporary certificates only to the system temporary directory.

```sh
.venv/bin/python -m pip install -r server/requirements-test.txt
npm --prefix server/common/ai ci
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check.py
```

This command runs all Python suites, the Node tests, the TypeScript check and the client syntax check in parallel. If one check fails, a dependency is missing or a Python suite is empty, the command fails. GitHub Actions runs the same command on Ubuntu and macOS for each push and pull request.

To run one suite:

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests -v
# After you install the Mac dependencies:
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests_mac -v
npm --prefix server/common/ai run typecheck
npm --prefix server/common/ai test
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/common/ai/tests -p 'test_*.py' -v
```

## Project layout

```text
server/               Gateway server (python -m server)
  common/             Shared API, tasks, pairing, conversations, monitor, inbound auto-answer
    ai/               Node.js AI worker (GPT-Live session) and its launcher
  mac/                Mac adapter: built-in HFP/SCO, pairing
    native/           Native Bluetooth bridge and build script
  pi/                 Linux adapter: BlueZ, PipeWire Telephony
    deploy/           systemd and WirePlumber files for a persistent Linux service
  phone_gateway/      Compatibility layer for the older CLI
  tests/, tests_mac/  Offline tests
client/               Static web console (index.html, assets/, run.py)
scripts/              check.py (all offline checks), voice sample tool
docs/                 Architecture and design notes
```

## Documentation

- [Server and API](server/README.md)
- [Web console](client/README.md)
- [AI configuration](server/common/ai/README.md)
- [Mac built-in Bluetooth](server/mac/native/README.md)
- [Linux deployment](server/pi/deploy/README.md)
- [Architecture](docs/architecture.md)
- [Bluetooth multi-line exploration](docs/bluetooth-multiplexing-exploration.md)

## Contributing

Contributions are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. To report a security problem, follow [SECURITY.md](SECURITY.md). All participants must follow the [Code of Conduct](CODE_OF_CONDUCT.md). Changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## License

[MIT](LICENSE)
