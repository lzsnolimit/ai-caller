[English](README.md) | 简体中文

# 手机通话网关

[![CI](https://github.com/lzsnolimit/phone-call-gateway/actions/workflows/ci.yml/badge.svg)](https://github.com/lzsnolimit/phone-call-gateway/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Node.js 22.13+](https://img.shields.io/badge/Node.js-22.13%2B-339933)
![Platforms: macOS | Linux](https://img.shields.io/badge/Platforms-macOS%20%7C%20Linux-lightgrey)

通过蓝牙免提（HFP）把一部真实手机接到 AI 语音助手。AI 可以替你拨打电话，也可以接听来电。

Mac（内置蓝牙控制器）或 Linux 电脑（例如树莓派，使用 BlueZ + PipeWire）作为手机的蓝牙免提设备。网关把通话音频发给 OpenAI 实时语音模型，再把模型的声音送回通话。网页控制台管理手机、通话、任务和 Prompt。

> **安全提示：** API 和网页控制台没有鉴权。只在 `127.0.0.1` 或可信局域网中使用。不要把端口暴露到公网。见 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)。

## 目录

- [功能](#功能)
- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [配置](#配置)
- [隐私与数据](#隐私与数据)
- [状态与限制](#状态与限制)
- [常见问题](#常见问题)
- [测试](#测试)
- [项目结构](#项目结构)
- [文档](#文档)
- [参与贡献](#参与贡献)
- [许可证](#许可证)

## 功能

- **按任务拨号。** 每次拨号都有独立保存的任务：目的、预期结果和说明。可以直接输入，也可以读入 UTF-8 `.txt` 或 `.md` 文件。网关先保存任务，再拨号。
- **来电自动接听。** 线路空闲时，已连接手机的每个来电响铃约 3 秒后由 AI 接听。AI 询问对方是谁、有什么事、是否需要回电，不回答问题。每部手机可以有自己的来电 prompt。固定、只读的保护 prompt 优先。
- **原生电话控制。** 拨号、接听、挂断和 DTMF 都通过蓝牙 HFP。不使用 ADB、手机界面自动化或云端电话服务。
- **网页控制台。** 管理手机（扫描、配对、连接、断开、删除），发起通话，查看线路状态，阅读实时和已存盘的对话文字，监听双方，并在通话中给 AI 发文字指令。控制台支持中文/英文切换，以及浅色和深色主题。
- **音色选择。** 13 个 AI 音色，附带试听样本。
- **共用 System Prompt。** 一份可编辑的 prompt，对之后的通话生效。
- **对话存盘。** 网关把通话记录和完整对话文字存到磁盘。不保存电话号码和通话音频。
- **自带密钥。** 通过环境变量提供你自己的 OpenAI API 密钥。
- **Mac 和 Linux。** 同一个启动命令自动选择平台。不支持 Windows。

网关只有一条通话线路。多部手机可以配对、保持连接和待命，但同一时间只有一路通话。原因见 [蓝牙多路通话复用探索记录](docs/bluetooth-multiplexing-exploration.zh-CN.md)。

## 工作原理

```text
手机 SIM 通话
    ↕ Bluetooth HFP / SCO
server/ —— Linux/Pi 或 Mac：蓝牙、电话、任务、Prompt、Live worker
    ↕ HTTP 控制 / WebSocket 状态和 PCM
client/ —— 浏览器：设备与通话 UI，不保管模型凭据

Server ↔ GPT-Live（实时听说）↔ GPT-6 Luna（按需推理）
```

- **Server** 连接手机，管理唯一的通话线路，保存任务和对话，并为每通电话启动一个 AI worker 进程（Node.js）。
- **AI worker** 在通话和实时语音模型 `gpt-live-1` 之间传输 24 kHz PCM。语音模型可以把推理交给后端模型（默认 `gpt-6-luna`）。
- **Client** 是静态 HTML/JS 控制台。Server 在 `/` 提供它，也可以单独提供。

设计说明见 [架构](docs/architecture.zh-CN.md)。

## 环境要求

缺少下面任何一项，网关都不能完成通话。

### 硬件

- 一部有 SIM 卡、支持蓝牙免提（HFP）的手机。Android 和 iPhone 都可以。
- 一台运行网关的电脑，二选一：
  - **Mac**，使用内置蓝牙控制器。已在 macOS 26.4 和 BCM_4388C2 内置控制器上测试。其他 Mac 型号未测试。
  - **Linux / 树莓派**，使用 BlueZ 和 PipeWire（原生 Telephony）。见 [Linux 部署](server/pi/deploy/README.zh-CN.md)。
- 不支持 Windows。

### 软件

| 依赖 | 版本 | 用途 |
| --- | --- | --- |
| Python | 3.11+ | 网关服务 |
| Node.js | 22.13+ | AI 语音进程 |
| Xcode Command Line Tools | Mac | 构建原生蓝牙桥（`xcode-select --install`） |
| blueutil | Mac，`/opt/homebrew/bin/blueutil` | 运行时关闭系统蓝牙，退出时恢复（`brew install blueutil`） |
| BlueZ、PipeWire、WirePlumber、`pw-record`/`pw-play`/`pw-link` | Linux | 蓝牙与通话音频 |
| openssl | 仅测试 | 本地模型替身的临时证书 |

### AI 密钥

网关调用 OpenAI 实时语音接口。本项目不提供 API 密钥，请使用你自己的 OpenAI API 密钥。密钥只从网关进程的环境变量读取，不写入文件，不发给网页。

- 启动前设置 `OPENAI_API_KEY`。
- 可选：如果使用其他兼容 OpenAI 的服务地址，在 Codex 配置文件 `~/.codex/config.toml` 中设置 provider、`base_url` 和密钥变量名，网关会沿用。地址必须是普通 HTTPS URL。
- 模型说明见 [AI 配置](server/common/ai/README.zh-CN.md)。

## 快速开始

所有命令都在仓库根目录执行。

### Mac

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-mac.txt
npm --prefix server/common/ai ci
.venv/bin/python server/mac/native/build.py
export OPENAI_API_KEY=<你的密钥>
.venv/bin/python -m server --port 8765
```

注意：

- 运行期间，网关使用内置蓝牙控制器，**系统蓝牙会被关闭**，Mac 的蓝牙键盘、鼠标和耳机会断开。正常退出时，网关恢复系统蓝牙。
- 启动网关的终端必须能运行 `node`。使用 nvm 时，先在该终端执行 `node -v`。AI 进程使用与网关相同的 `node`。
- Mac 上网关只绑定 `127.0.0.1`。
- 在控制台中配对手机（手机 > 添加手机）。新配对可能替代手机原有的 Mac 系统配对。见 [Mac 说明](server/mac/native/README.zh-CN.md)。

### Linux / 树莓派

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
npm --prefix server/common/ai ci
export OPENAI_API_KEY=<你的密钥>
.venv/bin/python -m server --port 8765
```

注意：

- 用运行 PipeWire 的同一用户启动网关。该用户的 `PATH` 中必须有 `node`。
- 网关需要完整仓库，不能只复制 `server/`。
- BlueZ SPA 动态库须自行构建，本仓库不包含二进制。见 [Linux 部署](server/pi/deploy/README.zh-CN.md)。
- 要在可信局域网中提供服务，加 `--host <局域网地址>`。默认是 `127.0.0.1`。

### 打开控制台

访问 `http://127.0.0.1:8765/`。也可以把控制台作为单独的静态网站运行：

```sh
python3 client/run.py
```

然后访问 `http://127.0.0.1:5173/`，在页面中填写网关地址。见 [Client](client/README.zh-CN.md)。

## 配置

在网关进程的环境中设置下列环境变量。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 无（必填） | 你的 OpenAI API 密钥。如果 `~/.codex/config.toml` 设置了其他密钥变量名，网关读取那个变量。 |
| `OPENAI_BACKEND_MODEL` | `gpt-6-luna` | 通话的后端推理模型。 |
| `PHONE_DATA_DIR` | `~/.local/share/phone-call-gateway/` | 任务、Prompt、对话和日志的数据目录。 |
| `PHONE_NODE` | `PATH` 中的 `node` | AI worker 使用的 Node.js。Mac 启动器自动设为它自己的 `node`。 |
| `PHONE_CLIENT_ORIGINS` | 端口 5173 上的 `localhost` / `127.0.0.1` | 单独提供控制台时额外允许的来源（逗号分隔）。 |
| `PHONE_MAC_PYTHON` | `python3` | 仅 Mac。`server/run-mac.cjs` 使用的 Python。`python -m server` 会自动设置。 |
| `PHONE_MAC_NATIVE_DIR` | `~/.cache/phone-call-gateway/mac-native/` | 仅 Mac。原生蓝牙桥的编译输出目录。 |

`python -m server` 的命令行参数：

- `--host`（默认 `127.0.0.1`）。Mac 只接受 `127.0.0.1`。
- `--port`（默认 `8765`）。

## 隐私与数据

默认数据目录是 `~/.local/share/phone-call-gateway/`。目录权限为 700，文件权限为 600。

网关保存：

- 任务文件和 Prompt（私有 JSON）。
- 每个会话（去电和来电）的记录和完整对话文字，位于 `conversations/<id>/`。
- 诊断日志 `logs/calls.jsonl`。日志不含电话号码、任务正文、对话文字、音频或密钥。

网关不保存：

- 电话号码。
- 通话音频。
- API 密钥。

对话文字包含通话内容，属于敏感数据。目前没有自动清理，也没有删除 API。需要时手动删除数据目录中的文件。

AI 可以告诉对方自己是 AI 电话助理。请确认你使用通话转写和 AI 助手的方式符合你和对方所在地的法律。

## 状态与限制

本项目处于实验阶段。

- **同一时间只有一路通话。** 线路占用时，新拨号返回 `line_busy`；线路恢复中返回 `line_recovering`；目标手机的电话状态待核对时返回 `device_recovering`。多部手机仍可配对和连接。
- **Mac：** 拨号、AI 双向语音和挂断已用一部 Android 手机测试通过。Mac 上网关只保持一部手机连接。连接由网关发起，手机不能主动连接 Mac 网关。其他 Mac 控制器和 iPhone 经 Mac 的音频未测试。
- **Linux：** 较早版本已在树莓派 4 上用 Android 手机和 iPhone 完成真机测试。当前版本在 Linux 上只有离线测试，仍需真机测试。
- **未完整测试：** 控制台的双方监听、文字指令的效果和长任务通话。首次监听可能有噪音，原因未查明。
- **音频：** 网关使用 20ms 帧和有界队列，但不保证完全没有卡顿。
- **Prompt 安全：** 保护 prompt 降低泄露风险，但不能保证模型始终遵守。
- **没有鉴权。** 见 [SECURITY.zh-CN.md](SECURITY.zh-CN.md)。

## 常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 拨号后马上失败，错误为 `audio_peer_closed` | AI 进程没有启动：找不到 `node`，或没有密钥 | 确认终端能运行 `node -v`，并已设置密钥。在 `~/.local/share/phone-call-gateway/logs/calls.jsonl` 中查找 `worker_exit`。 |
| 启动报 `system Bluetooth did not release the controller` | blueutil 不在 `/opt/homebrew/bin/`，或系统蓝牙无法关闭 | 安装 blueutil。关闭占用蓝牙的应用后重试。 |
| Mac 上手机显示未连接 | Mac 网关不接受手机主动连接，连接由网关发起 | 在控制台中选择手机，点击“连接”。 |

## 测试

全部离线检查使用同一个命令。测试不会打开蓝牙、读取模型密钥或拨打电话。真实 worker 测试使用本地 TLS 模型替身，需要 `openssl`，临时证书只写入系统临时目录。

```sh
.venv/bin/python -m pip install -r server/requirements-test.txt
npm --prefix server/common/ai ci
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check.py
```

该命令并行运行全部 Python 测试、Node 测试、TypeScript 检查和 Client 语法检查。任一检查失败、缺少依赖或 Python 测试为空，命令都会失败。目前没有云端 CI。

单独运行某一部分：

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests -v
# 安装 Mac 依赖后：
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests_mac -v
npm --prefix server/common/ai run typecheck
npm --prefix server/common/ai test
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/common/ai/tests -p 'test_*.py' -v
```

## 项目结构

```text
server/               网关服务（python -m server）
  common/             共用 API、任务、配对、会话、监听、来电自动接听
    ai/               Node.js AI worker（GPT-Live 会话）及启动器
  mac/                Mac 适配：内置 HFP/SCO、配对
    native/           原生蓝牙桥及构建脚本
  pi/                 Linux 适配：BlueZ、PipeWire Telephony
    deploy/           Linux 常驻服务的 systemd 和 WirePlumber 文件
  phone_gateway/      旧 CLI 的兼容层
  tests/, tests_mac/  离线测试
client/               静态网页控制台（index.html、assets/、run.py）
scripts/              check.py（全部离线检查）、音色样本工具
docs/                 架构与设计说明
```

## 文档

- [Server 与 API](server/README.zh-CN.md)
- [网页控制台](client/README.zh-CN.md)
- [AI 配置](server/common/ai/README.zh-CN.md)
- [Mac 内置蓝牙](server/mac/native/README.zh-CN.md)
- [Linux 部署](server/pi/deploy/README.zh-CN.md)
- [架构](docs/architecture.zh-CN.md)
- [蓝牙多路通话复用探索记录](docs/bluetooth-multiplexing-exploration.zh-CN.md)

## 参与贡献

欢迎贡献。请先阅读 [CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)。报告安全问题请按 [SECURITY.zh-CN.md](SECURITY.zh-CN.md) 操作。所有参与者须遵守 [行为准则](CODE_OF_CONDUCT.md)。变更记录见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

[MIT](LICENSE)
