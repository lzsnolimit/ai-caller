English | [简体中文](CONTRIBUTING.zh-CN.md)

# Contributing

Thank you for your help. This document tells you how to set up the project, run the checks and send a pull request.

## Before you start

- For a bug, open an issue. Give the platform (Mac or Linux), the versions of the OS, Python, Node.js and PipeWire/BlueZ, the phone type, the steps and the error code.
- For a new feature or a large change, open an issue first and discuss it.
- For a security problem, do not open a public issue. Follow [SECURITY.md](SECURITY.md).

## Set up

You need Python 3.11+ and Node.js 22.13+. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-test.txt
npm --prefix server/common/ai ci
```

To run the gateway on a real phone, see [Quick start](README.md#quick-start).

## Run the checks

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check.py
```

This command runs all Python suites, the Node tests, the TypeScript check and the client syntax check. All checks must pass before you send a pull request. The tests are offline. They do not turn on Bluetooth, read model keys or make calls. The real-worker tests need `openssl`.

There is no hosted CI at this time. Write the result of `scripts/check.py` in the pull request.

## Coding notes

- Keep changes small and in scope. Fix the layer that causes the problem. Do not add special cases for one sample.
- Shared behavior goes in `server/common/`. Platform behavior goes in `server/mac/` or `server/pi/`.
- The gateway has one call line. Do not add parallel calls back without a design discussion. See [Bluetooth multi-line exploration](docs/bluetooth-multiplexing-exploration.md).
- Do not log phone numbers, task text, transcripts, audio or keys. Read the key only from the environment.
- Do not commit keys, personal data, real Bluetooth addresses, phone numbers or local paths. Use `AA:BB:CC:DD:EE:FF` and `+123456` in examples.
- Add or update offline tests for changed behavior. Tests must not need a real phone, Bluetooth or a model key.
- Write new code and code comments in English.
- If you change user-facing text in the console, update both languages in `client/assets/messages.js`.
- If you change behavior that a README describes, update both the English and the Chinese file.

## Pull requests

- One topic for each pull request.
- Describe what changed and why, and how you tested it.
- Tell which platforms you tested on real devices (Mac, Linux, Android, iPhone), and which you did not test.
- Do not mix unrelated formatting changes.

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE).
