English | [简体中文](README.zh-CN.md)

# Phone Call Gateway

[![CI](https://github.com/lzsnolimit/phone-call-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/lzsnolimit/phone-call-gateway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Node.js 22.13+](https://img.shields.io/badge/Node.js-22.13%2B-339933)
![Platforms: macOS | Linux](https://img.shields.io/badge/Platforms-macOS%20%7C%20Linux-lightgrey)

Connect a real mobile phone to an AI voice agent over Bluetooth hands-free (HFP). The AI can place calls for you and answer incoming calls.

A Mac (built-in Bluetooth controller) or a Linux computer such as a Raspberry Pi (BlueZ + PipeWire) acts as a Bluetooth hands-free unit for the phone. The gateway sends the call audio to an OpenAI realtime voice model and sends the model audio back into the call. A web console manages phones, calls, tasks and prompts.

> **Security notice:** The API and the web console have no authentication. Use the gateway only on `127.0.0.1` or on a trusted LAN. Do not expose the port to the internet. See [SECURITY.md](SECURITY.md).

## Contents

- [Features](#features)
- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Privacy and data](#privacy-and-data)
- [Status and limitations](#status-and-limitations)
- [Troubleshooting](#troubleshooting)
- [Testing](#testing)
- [Project layout](#project-layout)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)

## Features

- **Outbound calls by task.** Each call has its own saved task: purpose, expected outcome and details. You can type the task or load a UTF-8 `.txt` or `.md` file. The gateway saves the task before it dials.
- **Inbound auto-answer.** When the line is idle, the AI answers each incoming call on a connected phone after about 3 seconds of ringing. The AI asks who is calling, what they want and if they need a call back. It does not answer questions. Each phone can have its own inbound prompt. A fixed, read-only guard prompt has priority.
- **Native phone control.** Dial, answer, hang up and DTMF go through Bluetooth HFP. The gateway does not use ADB, phone UI automation or a cloud telephony service.
- **Web console.** Manage phones (scan, pair, connect, disconnect, remove), start calls, see the line state, read live and saved transcripts, listen to both sides, and send text instructions to the AI during a call. The console has a Chinese/English language switch and light and dark themes.
- **Voice selection.** 13 AI voices with bundled preview samples.
- **Shared System Prompt.** One editable prompt applies to the next calls.
- **Saved conversations.** The gateway saves call records and full transcripts to disk. It does not save phone numbers or call audio.
- **Bring your own key.** You supply your own OpenAI API key through an environment variable.
- **Mac and Linux.** One start command selects the platform. Windows is not supported.

The gateway has one call line. Many phones can pair, stay connected and wait. Only one call can be active at a time. For the reason, see [Bluetooth multi-line exploration](docs/bluetooth-multiplexing-exploration.md).

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
  - **Mac** with the built-in Bluetooth controller. Tested on macOS 26.4 with a BCM_4388C2 built-in controller. Other Mac models are not tested.
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

## Configuration

Set these environment variables in the environment of the gateway process.

| Variable | Default | Description |
| --- | --- | --- |
| `OPENAI_API_KEY` | none (required) | Your OpenAI API key. If `~/.codex/config.toml` sets a different key variable name, the gateway reads that variable. |
| `OPENAI_BACKEND_MODEL` | `gpt-6-luna` | Backend reasoning model for calls. |
| `PHONE_DATA_DIR` | `~/.local/share/phone-call-gateway/` | Data directory for tasks, prompts, conversations and logs. |
| `PHONE_NODE` | `node` from `PATH` | Node.js binary for the AI worker. The Mac launcher sets it to its own `node`. |
| `PHONE_CLIENT_ORIGINS` | `localhost` / `127.0.0.1` on port 5173 | Extra allowed origins (comma-separated) for a separately served console. |
| `PHONE_MAC_PYTHON` | `python3` | Mac only. Python used by `server/run-mac.cjs`. `python -m server` sets it. |
| `PHONE_MAC_NATIVE_DIR` | `~/.cache/phone-call-gateway/mac-native/` | Mac only. Build output of the native Bluetooth bridge. |

Command-line options of `python -m server`:

- `--host` (default `127.0.0.1`). The Mac accepts only `127.0.0.1`.
- `--port` (default `8765`).

## Privacy and data

The default data directory is `~/.local/share/phone-call-gateway/`. Directories are created with mode 700 and files with mode 600.

The gateway saves:

- Task files and prompts (private JSON).
- For each conversation (outbound and inbound), a record and the full transcript under `conversations/<id>/`.
- Diagnostic logs under `logs/calls.jsonl`. The logs do not contain phone numbers, task text, transcripts, audio or keys.

The gateway does not save:

- Phone numbers.
- Call audio.
- The API key.

Transcripts contain conversation content. Treat them as sensitive data. There is no automatic cleanup and no delete API. Remove files from the data directory manually when necessary.

The AI can tell callers that it is an AI phone assistant. Make sure that your use of call recording, transcription and AI agents obeys the laws where you and the other party are.

## Status and limitations

This project is experimental.

- **One call at a time.** When the line is busy, a new call returns `line_busy`. When the line is recovering, it returns `line_recovering`. When the target phone's call state needs a check, it returns `device_recovering`. Many phones can still pair and connect.
- **Mac:** Dialing, two-way AI voice and hang-up passed tests with an Android phone. On the Mac, the gateway keeps only one phone connected. The gateway starts the connection; the phone cannot connect to the Mac gateway by itself. Other Mac controllers and iPhone audio through the Mac are not tested.
- **Linux:** An earlier version passed real-device tests on a Raspberry Pi 4 with an Android phone and an iPhone. The current version has offline tests only on Linux and still needs real-device tests.
- **Not fully tested:** Listening to both sides in the console, the effect of text instructions, and long task calls. The first listen session can have noise; the cause is not known.
- **Audio:** The gateway uses 20 ms frames and bounded queues. This does not guarantee audio without gaps.
- **Prompt safety:** The guard prompt decreases the risk of data leaks. It cannot guarantee that the model always obeys it.
- **No authentication.** See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | Cause | Action |
| --- | --- | --- |
| A call fails immediately with `audio_peer_closed` | The AI worker did not start: `node` was not found, or the key is missing | Make sure that the terminal can run `node -v` and that the key is set. Find `worker_exit` in `~/.local/share/phone-call-gateway/logs/calls.jsonl`. |
| Start fails with `system Bluetooth did not release the controller` | blueutil is not in `/opt/homebrew/bin/`, or system Bluetooth cannot turn off | Install blueutil. Close apps that use Bluetooth, then try again. |
| On the Mac, the phone shows "not connected" | The Mac gateway does not accept connections that the phone starts. The gateway starts the connection. | In the console, select the phone and click Connect. |

## Testing

All offline checks use one command. The tests do not turn on Bluetooth, read model keys or make phone calls. The real-worker tests use a local TLS model stand-in. They need `openssl` and write temporary certificates only to the system temporary directory.

```sh
.venv/bin/python -m pip install -r server/requirements-test.txt
npm --prefix server/common/ai ci
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check.py
```

This command runs all Python suites, the Node tests, the TypeScript check and the client syntax check in parallel. If one check fails, a dependency is missing or a Python suite is empty, the command fails. There is no hosted CI at this time.

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
