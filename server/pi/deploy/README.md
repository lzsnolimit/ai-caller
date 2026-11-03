English | [简体中文](README.zh-CN.md)

# Linux Persistent Deployment (Raspberry Pi)

This directory contains systemd and WirePlumber files to run the gateway as a persistent user service on Linux. The examples use the host name `raspberrypi.local` and the service user `<user>`. Replace them with your own values.

## Status

`phone-call-gateway.service` runs the current server (task/AI console). It was tested on a Raspberry Pi 4 with an Android phone: outbound AI call, two-way audio, saved transcript and hang-up. Inbound auto-answer is not yet tested on the Pi.

To run the server as a service, you need:

- The full repository layout (with `server/` and `client/`).
- A Python environment, Node.js 22.13+ and `server/common/ai/node_modules`.
- Your own OpenAI API key in a private env file. Copy `env.example` to `~/.config/phone-call-gateway/env` and run `chmod 600` on it. Do not copy real keys into the repository.

The process entry is `python -m server --host raspberrypi.local --port 8765` in the repository root. Replace `raspberrypi.local` with a name or address that resolves to this host's LAN address. Do not use `0.0.0.0`: the API has no authentication, and `0.0.0.0` also exposes it on VPN and container networks.

## Tested platform

The configuration was tested on a Raspberry Pi 4 with Debian 12 aarch64, PipeWire 1.2.7 and WirePlumber 0.4.13. It loads only a separately built PipeWire 1.4.11 BlueZ SPA. It does not replace the system PipeWire and does not install a second WirePlumber. The Lua configuration does not apply to WirePlumber 0.5.

You must build the BlueZ SPA yourself. This repository contains no binaries. The SHA256 values of the tested files:

```text
747af4713b97630f1195700e384d50b4c676523b02bd1353b4e4a8cd87e3e07d  libspa-bluez5.so
5b47022206f7b2114c49aadae660d076649cd7bddc5eb0db832d267a6e09f594  libspa-codec-bluez5-faststream.so
696962f643faa33aa54c2ed3f662c1ca4f9c292b046bc7b5cbfd8418f0c280ee  libspa-codec-bluez5-sbc.so
51f78c5944554958a3c1d6624a27809427c405dbbaa0e8b63a1fab1d7b905368  bluez-hardware.conf
```

## File locations

`~` is the home directory of the service user.

- `~/phone-call-gateway/`: the full repository.
- `~/phone-call-gateway/.venv/`: runtime environment created from `server/requirements.txt`. Also run `npm --prefix server/common/ai ci`.
- `~/.config/phone-call-gateway/env`: private env file (mode 600) from `env.example`.
- `~/.local/lib/phone-call-gateway/runtime/bluez5/`: the three BlueZ shared libraries and `bluez-hardware.conf`.
- `runtime/spa-0.2/bluez5` and `runtime/spa-data/bluez5` point to `../bluez5`. The other SPA directories point to the system directories in `/usr/lib/aarch64-linux-gnu/spa-0.2/`. Do not mix in older BlueZ codec libraries.
- `~/.config/systemd/user/phone-call-gateway.service`: the file with the same name in this directory.
- `~/.config/systemd/user/wireplumber.service.d/90-phone-gateway.conf`: `wireplumber-phone-gateway.conf` in this directory.
- `~/.config/wireplumber/bluetooth.lua.d/89-phone-gateway-headless.lua`: the file with the same name in this directory. It must load after the system `50-bluez-config.lua` and before `90-enable-all.lua`, so that the logind module is not registered.
- `~/.config/wireplumber/bluetooth.lua.d/89-phone-gateway-routing.lua`: the file with the same name in this directory. It matches only the phone HFP AG streams and stops automatic connection to local Pi audio. The gateway connects the private PCM nodes explicitly. It does not match Bluetooth headphones or A2DP music nodes.

Persistent files must not point to temporary directories. Keep the existing Bluetooth configuration and other services. Restart WirePlumber only when the phone is idle.

Tests with an iPhone showed that without the routing rule, WirePlumber connects the mono call stream to local ALSA playback and monitor. This makes stereo ports and a phone echo. The gateway accepts only the MONO call port of the selected phone, so it does not treat this wrong link as ready. The rule matches the HFP AG role. It does not use a fixed phone address, and it does not hide wrong routing by matching more audio ports.

## Service management

After you install the code, the runtime environment, the libraries and the configuration above, run as the service user:

```sh
systemctl --user daemon-reload
systemctl --user restart wireplumber.service
systemctl --user enable --now phone-call-gateway.service
systemctl --user status phone-call-gateway.service --no-pager
```

The service user must have linger enabled. The system `bluetooth.service` and the user PipeWire/WirePlumber services must be enabled. To check:

```sh
loginctl show-user <user> -p Linger
systemctl is-enabled bluetooth.service
systemctl --user is-enabled pipewire.service pipewire-pulse.service wireplumber.service phone-call-gateway.service
```

The gateway binds to the LAN address that `raspberrypi.local` resolves to. This does not use `127.0.0.1:8765`, which another service can already use. The address resolves when the process starts. If the LAN address changes, restart the gateway. If DNS is not ready or the process fails, the service tries again every 3 seconds. If WirePlumber fails, it tries again every 2 seconds. When the phone service is not available for a short time, the API returns the real capability state. It does not dial automatically. Clients still use the `connect` endpoint to connect a paired phone.

The endpoint is `http://raspberrypi.local:8765/v1/capabilities`. There is no authentication. Use it only on a trusted LAN. Do not open it to the internet. Access logs are off. The Bluetooth components log only errors, so that normal dial logs do not contain phone numbers.

To accept a deployment, check the API, the native phone capability, the phone connection, process failure recovery and two-way SCO audio separately. Checks of service enablement, linger and failure recovery do not replace a real restart test. Do not restart a whole machine that runs other services only for this test.
