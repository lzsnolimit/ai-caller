[English](README.md) | 简体中文

# Linux 常驻部署（树莓派）

本目录包含在 Linux 上把网关作为常驻用户服务运行的 systemd 和 WirePlumber 文件。示例使用主机名 `raspberrypi.local` 和服务用户 `<user>`，请替换为你自己的值。

## 状态

`phone-call-gateway.service` 运行当前的 Server（任务/AI 控制台）。已在树莓派 4 上用 Android 手机测试：AI 去电、双向语音、保存对话文字和挂断。来电自动接听尚未在树莓派上测试。

把 Server 作为服务运行，需要：

- 完整的仓库布局（含 `server/`、`client/`）。
- Python 环境、Node.js 22.13+ 与 `server/common/ai/node_modules`。
- 私有 env 文件中你自己的 OpenAI API 密钥。把 `env.example` 复制为 `~/.config/phone-call-gateway/env`，并执行 `chmod 600`。不要把实际密钥复制到仓库。

进程入口为仓库根目录的 `python -m server --host raspberrypi.local --port 8765`。把 `raspberrypi.local` 换成能解析到本机局域网地址的名称或地址。不要用 `0.0.0.0`：API 没有鉴权，`0.0.0.0` 还会把它暴露到 VPN 和容器网络。

## 已测试的平台

此配置在树莓派 4、Debian 12 aarch64、PipeWire 1.2.7 和 WirePlumber 0.4.13 上测试过。只加载单独构建的 PipeWire 1.4.11 BlueZ SPA，不替换系统 PipeWire，也不安装第二个 WirePlumber。Lua 配置不适用于 WirePlumber 0.5。

BlueZ SPA 需要自行构建，本仓库不包含二进制。已测试文件的 SHA256：

```text
747af4713b97630f1195700e384d50b4c676523b02bd1353b4e4a8cd87e3e07d  libspa-bluez5.so
5b47022206f7b2114c49aadae660d076649cd7bddc5eb0db832d267a6e09f594  libspa-codec-bluez5-faststream.so
696962f643faa33aa54c2ed3f662c1ca4f9c292b046bc7b5cbfd8418f0c280ee  libspa-codec-bluez5-sbc.so
51f78c5944554958a3c1d6624a27809427c405dbbaa0e8b63a1fab1d7b905368  bluez-hardware.conf
```

## 文件位置

`~` 为服务用户的主目录。

- `~/phone-call-gateway/`：完整仓库。
- `~/phone-call-gateway/.venv/`：按 `server/requirements.txt` 新建的运行环境。另需执行 `npm --prefix server/common/ai ci`。
- `~/.config/phone-call-gateway/env`：由 `env.example` 复制的私有 env 文件（权限 600）。
- `~/.local/lib/phone-call-gateway/runtime/bluez5/`：三个 BlueZ 动态库和 `bluez-hardware.conf`。
- `runtime/spa-0.2/bluez5` 和 `runtime/spa-data/bluez5` 指向 `../bluez5`；其余 SPA 目录指向 `/usr/lib/aarch64-linux-gnu/spa-0.2/` 的系统目录。不得混入旧版 BlueZ codec 库。
- `~/.config/systemd/user/phone-call-gateway.service`：本目录同名文件。
- `~/.config/systemd/user/wireplumber.service.d/90-phone-gateway.conf`：本目录的 `wireplumber-phone-gateway.conf`。
- `~/.config/wireplumber/bluetooth.lua.d/89-phone-gateway-headless.lua`：本目录同名文件。必须在系统 `50-bluez-config.lua` 之后、`90-enable-all.lua` 之前加载，避免注册 logind 模块。
- `~/.config/wireplumber/bluetooth.lua.d/89-phone-gateway-routing.lua`：本目录同名文件。仅匹配手机 HFP AG 收发流，禁止自动连接 Pi 本机音频；网关显式连接私有 PCM 节点。不匹配蓝牙耳机或 A2DP 音乐节点。

常驻文件不能指向临时目录。保留现有的蓝牙配置和其他服务。只在手机空闲时重启 WirePlumber。

iPhone 测试发现，缺少路由规则时 WirePlumber 会把单声道通话流连接到本机 ALSA 播放与 monitor，形成双声道端口及电话回声。网关只接受选定手机的 MONO 通话端口，因此不会把此错误链路当作已就绪。这条规则按 HFP AG 角色匹配，不固定手机地址，也不通过扩大音频端口匹配范围掩盖错误路由。

## 服务管理

安装代码、运行环境、动态库和上述配置后，以服务用户执行：

```sh
systemctl --user daemon-reload
systemctl --user restart wireplumber.service
systemctl --user enable --now phone-call-gateway.service
systemctl --user status phone-call-gateway.service --no-pager
```

服务用户必须启用 linger，系统 `bluetooth.service` 及用户 PipeWire/WirePlumber 服务必须启用。检查命令：

```sh
loginctl show-user <user> -p Linger
systemctl is-enabled bluetooth.service
systemctl --user is-enabled pipewire.service pipewire-pulse.service wireplumber.service phone-call-gateway.service
```

网关绑定 `raspberrypi.local` 解析出的局域网地址，不占用其他服务可能已使用的 `127.0.0.1:8765`。地址在进程启动时解析；局域网地址变化后须重启网关。DNS 尚未就绪或进程失败时，服务每 3 秒重试。WirePlumber 失败后每 2 秒重试。电话服务暂不可用时，API 返回真实的能力状态，不会自动拨号。客户端仍通过 `connect` 接口连接已配对的手机。

接口为 `http://raspberrypi.local:8765/v1/capabilities`。没有鉴权，只用于可信局域网，不要开放到公网。不启用访问日志；蓝牙组件只记录错误，避免正常拨号日志包含号码。

部署验收应分别检查 API、原生电话能力、手机连接、进程失败恢复和双向 SCO 音频。服务启用、linger 和失败恢复检查不能替代真实的重启测试。不要为此重启运行其他业务的整台机器。
