English | [简体中文](SECURITY.zh-CN.md)

# Security Policy

## Security model

The gateway has **no authentication**. The HTTP API, the WebSocket endpoints and the web console accept all requests that reach the port.

Any device that can reach the port can:

- Dial and hang up calls on the connected phones.
- Pair, connect and remove phones.
- Listen to the call audio.
- Read saved conversations and transcripts.
- Change the System Prompt and the inbound prompts.

The control lease (`X-Controller-Lease`) only prevents conflicts between clients. It is not a permission check.

Because of this:

- The default address is `127.0.0.1`. On the Mac, the gateway binds only to `127.0.0.1`.
- On Linux, use `--host` only with an address on a trusted LAN.
- Do not expose the port to the internet or to an untrusted network. Do not forward it through a router or a public tunnel.
- If you need remote access, put the gateway behind your own authenticated access layer, such as a VPN.

## API key

- You supply your own OpenAI API key.
- The gateway reads the key only from the environment of the gateway process (`OPENAI_API_KEY`, or the key variable name that the optional `~/.codex/config.toml` sets).
- The gateway does not write the key to a file, does not log it and does not send it to the web console.
- Do not put keys in the repository, in task files or in prompts.

## Stored data

The default data directory is `~/.local/share/ai-caller/` (change it with `PHONE_DATA_DIR`). Directories have mode 700 and files have mode 600.

| Data | Saved | Location |
| --- | --- | --- |
| Tasks (purpose, outcome, details) and prompt snapshots | Yes | `tasks/` |
| System Prompt and inbound prompts | Yes | data directory |
| Conversation records (direction, phone Bluetooth address, dialed or caller number, times, state) | Yes | `conversations/<id>/meta.json` |
| Full transcripts | Yes | `conversations/<id>/transcript.jsonl` |
| Diagnostic logs (no numbers, text, audio or keys) | Yes | `logs/calls.jsonl` |
| Phone numbers in logs, transcripts, task files or the device call list | No | — |
| Call audio | Only when `PHONE_RECORD_CALLS=1` | `conversations/<id>/call.wav` |
| API key | No | — |

On the Mac, Bluetooth pairing keys are saved in `<data dir>/devices/pair-keys.json` (mode 600). This file is outside the repository. Do not commit or share it.

Transcripts contain conversation content and are sensitive. There is no automatic cleanup and no delete API. Remove files manually when necessary.

The inbound guard prompt decreases the risk that the AI discloses information to callers. It cannot guarantee that the model always obeys it.

## Report a vulnerability

Do not report security problems in public issues.

Open a private security advisory on the repository (Security > Advisories > Report a vulnerability). Give:

- A description of the problem and its effect.
- Steps to reproduce it.
- The affected version or commit, and the platform.

Do not include real API keys, phone numbers or transcripts in the report.

## Supported versions

This project is experimental. Only the latest code on the default branch receives security fixes.
