English | [简体中文](README.zh-CN.md)

# Mac Built-in Bluetooth

This adapter uses the existing gateway API and Live worker. It does not rewrite the AI. HFP dialing, two-way SCO voice and AI calls passed tests on macOS 26.4 with a BCM_4388C2 PCIe built-in controller and an Android phone. Other Mac controllers, iPhone audio through the Mac and many calls at the same time are not tested.

You need Node.js 22.13+, Python 3.11+, Xcode Command Line Tools and `/opt/homebrew/bin/blueutil`. From the repository root:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-mac.txt
npm --prefix server/common/ai ci
.venv/bin/python server/mac/native/build.py
export OPENAI_API_KEY=<your-key>
.venv/bin/python -m server --port 8765
```

The build output goes to `~/.cache/phone-call-gateway/mac-native/` by default, not into the repository. Use `PHONE_MAC_NATIVE_DIR` to set a different private directory. On Python 3.13+, the `audioop-lts` dependency keeps the streaming sample-rate conversion interface.

This command binds the API and the console to `127.0.0.1:8765`. On the Mac, the gateway accepts only `127.0.0.1`. Select and connect the phone in the console, then save a task and dial. The service can start while the phone is not connected. The call state comes from native HFP, not ADB. The state does not show phone numbers. On the Mac, the gateway keeps only one target connection. The gateway starts the connection; the phone cannot connect to the Mac gateway by itself.

The server starts the AI worker internally. The default is Live + Luna. See [AI configuration](../../common/ai/README.md). The older CLI for one selected phone still works: `PHONE_MAC_PYTHON=.venv/bin/python node server/run-mac.cjs AA:BB:CC:DD:EE:FF`. It is not the entry for the task console. Cancelling a managed conversation cleans up the call. Closing the older low-level PCM alone does not hang up.

AI audio goes only between the phone and the model. It does not use the Mac sound card. SCO is 8 kHz PCM. The gateway keeps the conversion state and supplies 24 kHz PCM to the rest of the system. When there is no AI output, it sends silence. A continuous overload fails clearly. Speech is not dropped silently. The model key is not saved locally. Transcripts are saved by the shared server, as described in the [server README](../../README.md#persistence-and-privacy).

The separate Bluetooth stack must turn off system Bluetooth for a short time to release the built-in controller. While the service runs, system Bluetooth cannot use the controller at the same time. At exit, the gateway stops the native process first, then turns system Bluetooth on again and reads back the power state. Do not stop the power restore step of the parent process. SIGKILL (which cannot be caught) or a power loss cannot guarantee the restore.

New pairing keys are saved only in the private local file `<data dir>/devices/pair-keys.json` (mode 600, default data dir `~/.local/share/phone-call-gateway/`). The file is outside the repository; `.gitignore` also excludes `pair-keys.json`. A new separate pairing can replace the phone's existing pairing with the Mac system. When you go back to system Bluetooth, you may need to pair again. The gateway does not read, export or delete the pairing keys of other devices.

Mac offline tests:

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests_mac -v
```

These tests use phone and transport stand-ins. They do not dial, and do not open Bluetooth or local audio devices.
