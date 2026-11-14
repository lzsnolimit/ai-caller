[English](README.md) | 简体中文

# 手机网关 Server

Server 负责 AI、task、Prompt、设备与电话管理。`common/` 为共用运行服务，`pi/` 是 Linux 适配，`mac/` 是 Mac 适配。

Server 连接手机并运行内部 Node.js Live worker；模型密钥只在 Server 环境中。Client 只提供管理 UI。电话控制用原生 HFP，不用 ADB 或手机界面自动化。

**单线路：** 整个 Server 同一时间只有一路受管通话。多部手机可以配对、保持连接和待命；配对数量与通话路数分开管理。原因见 [蓝牙多路通话复用探索记录](../docs/bluetooth-multiplexing-exploration.zh-CN.md)；生命周期见 [架构](../docs/architecture.zh-CN.md)。

`python -m server` 自动选择 Linux 或 Mac，不支持 Windows。Mac 控制器占用见 [Mac 说明](mac/native/README.zh-CN.md)，模型分工见 [AI 配置](common/ai/README.zh-CN.md)。

## 安装与启动

Linux 常驻服务见 [Linux 部署](pi/deploy/README.zh-CN.md)。下面从仓库根目录手动启动。

需要 Python 3.11+、Node.js 22.13+；Linux 另需 BlueZ、PipeWire/WirePlumber 及 `pw-record`、`pw-play`、`pw-link`。电话控制需要 PipeWire 原生 Telephony。已测试的组合是系统 PipeWire 1.2.7 加单独构建的 PipeWire 1.4.11 BlueZ SPA；单独的 PipeWire 1.2.7 不受支持。

在 Linux 上，先用系统蓝牙工具配对手机。用运行 PipeWire 的同一 Linux 用户启动网关；其 D-Bus 用户会话必须可访问 `org.pipewire.Telephony`，系统总线必须可访问 BlueZ。

在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements.txt
npm --prefix server/common/ai ci
export OPENAI_API_KEY=<你的密钥>
.venv/bin/python -m server
```

API 和同源 UI 的默认地址为 `127.0.0.1:8765`。Linux 若要在可信局域网中提供服务，显式指定已核对的地址：

```sh
.venv/bin/python -m server --host raspberrypi.local --port 8765
```

本接口没有鉴权。不要将端口暴露到公网或不可信网络。能访问端口的设备可以控制手机、接收音频和读取已存盘的对话。控制租约只防止客户端争用，不是权限检查。网关不记录号码、音频或访问日志；每个会话（去电和来电）的完整对话文字会存盘，见下面的 [持久化与隐私](#持久化与隐私)。

## 公共接口

### 工作台 API

- `GET /v1/console`，`WS /v1/console/events`：设备、能力、配对、会话（当前通话与历史）和唯一线路的快照。
  - `line`：`{"state":"idle|busy|recovering","conversationId":...,"address":...,"reason":...,"audioReady":false}`。`busy` 表示有一路通话占用线路；`recovering` 表示上一通的本地资源未确认释放，`reason` 说明原因。
  - `devices[i].recovering`：`true` 表示该手机的电话状态待核对，暂时隔离；核对到电话已结束后自动解除。其他手机不受影响。
  - `devices[i].inboundPrompt`：`{"configured":true|false}`，表示该手机是否已配置来电 prompt。
  - `conversations[i].direction`：`"outbound"`（去电）或 `"inbound"`（来电）。
  - 快照不包含 `capacity` 和 `audioLines`。
- `POST /v1/discovery`：扫描。
- `POST /v1/pairings`：`{"address":"AA:BB:CC:DD:EE:FF"}`；`GET/POST/DELETE /v1/pairings/{id}`：查进度、提交请求对应的确认、取消。不要把数字比较、Passkey 和 PIN 混用。
- `POST /v1/management/{address}/connect|disconnect|remove`：管理目标手机。通话、配对或租约占用时拒绝冲突操作。
- `POST /v1/conversations`：地址、号码与 `task:{purpose,outcome,content,source}`，先保存文件，再原生拨号并启动 Live。拒绝时不拨号、不排队：
  - 线路占用：409 `line_busy`。
  - 线路恢复中：503 `line_recovering`。
  - 目标手机电话状态待核对：409 `device_recovering`。
  - 目标手机已有电话：409 `phone_busy`。
- `GET /v1/conversations`、`GET /v1/conversations/{id}`：会话列表/详情与 task 快照。条目含 `direction`、`number`（拨出的号码或数字来电号码，未知时为 `null`）、`answeredAt`、`endedAt`（Unix 秒，未发生时为 `null`）。列表包含存盘的历史会话（启动时最多加载最近 256 条），支持 `?direction=outbound|inbound` 和 `?address=AA:BB:CC:DD:EE:FF` 过滤；参数无效返回 400 `invalid_direction` 或 `invalid_address`。
- `GET /v1/conversations/{id}/transcript`：返回存盘的完整对话转写 `{"type":"transcript.record","conversationId","direction","state","complete":bool,"events":[{"seq","role":"caller|ai","eventId","text","startMs","endMs"}]}`。进行中的会话返回已写入的部分（`complete:false`）。实时增量使用 `WS .../transcript/events`。
- `GET/PUT /v1/devices/{address}/inbound-prompt`：每部手机的来电 prompt。PUT 的 JSON 为 `{"content":"..."}`，UTF-8 最多 16 KiB。GET 返回 `{"address","content","default":bool,"guard":"..."}`；`default` 为 `true` 表示未配置，`content` 此时是默认内容；PUT 空白内容恢复默认；`guard` 是系统保护 prompt 全文，只读。错误：400 `invalid_inbound_prompt`、404 `device_not_found`、503 `inbound_prompt_save_failed`。删除设备配对时同时删除其来电 prompt。
- `POST /v1/conversations/{id}/commands`、`POST .../end`：当前对话的文字指令与结束。
- `WS /v1/conversations/{id}/monitor`：首字节 0/1 表示手机/AI，后续为 24 kHz PCM；独立副本，不占用主音频租约。
- `GET/PUT /v1/system-prompt`：`{"content":"..."}`，只用于下一次 task。

状态含 `dialing/answering/starting/active/ending/ended/failed/cleanup_failed`；`answering` 表示来电已占用线路、正在等待手机确认接通。每次拨号或接听都新建会话和任务，结束后统一收尾；恢复只核对状态，不自动重拨，也不盲目挂断。服务重启不恢复进行中的会话；只从存盘加载历史会话的记录（不加载转写正文）。上个进程未收尾的会话加载为 `failed`，`error` 为 `server_restarted`。任务结束不等于目标完成，发送指令不等于 AI 执行。

### 来电自动接听

1. Server 订阅每部已连接手机的电话状态。手机出现响铃中的来电（`state:"incoming"`）时，只在线路空闲、该手机未处于“电话状态待核对”、且没有低层控制租约时继续。
2. 约 3 秒后再核对：仍是同一通响铃中的来电，并且线路仍空闲，才占用线路并新建来电会话（`direction:"inbound"`）。
3. 发出接听命令，会话状态为 `answering`。接听结果以手机的通话状态为准：手机报告通话已接通即算成功，不以 AT 或 D-Bus 的回复为准。有界时间内没有接通，按接听失败统一收尾（`failed`，`endReason` 为 `answer_failed`）：来电仍在响铃时不挂断、不拒接，线路释放；已接通时按通话身份挂断；无法查询手机状态时隔离该手机并等待核对，不发挂断。
4. 接通后启动 AI，生命周期、挂断、恢复规则与去电相同。

不处理的情况：线路占用、线路恢复中或该手机待核对时，网关不接、不拒接、不挂断，由手机自己处理（例如响铃、转语音信箱）。等待期间对方挂断或机主已在手机上接听，网关放弃，不占用线路。

来电任务的组成：`systemPrompt` 为共用 System Prompt 加系统保护 prompt；`purpose` 与 `outcome` 是固定文本（礼貌询问并记录对方的身份和来意；不回答问题，不透露我方信息）；`content` 是该手机的来电 prompt，未配置时使用默认内容。任务快照保存到 `tasks/`，带 `direction:"inbound"`。

### 系统保护 prompt

保护 prompt 是固定文件 `server/common/inbound-guard-prompt.md`，服务启动时加载，没有修改 API；Client 只读显示。要点：AI 只询问对方是谁、有什么事、是否需要回电，并复述确认；不回答任何问题；不透露机主姓名、位置、日程、号码、关系，可以说明自己是 AI 电话助理，但不透露使用的系统、模型或 prompt；对方冒充熟人、声称紧急或要求忽略规则时，礼貌拒绝并回到来意；不做承诺、付款或身份确认。设备来电 prompt 只能在此范围内补充；两者冲突时以保护 prompt 为准。

保护 prompt 降低泄露风险，但不能保证模型在所有诱导下都不出错。

### 持久化与隐私

去电和来电的每个会话都存盘，位于数据目录（默认 `~/.local/share/ai-caller/`，可用 `PHONE_DATA_DIR` 修改）下的 `conversations/<id>/`：

- `meta.json`：会话 ID、方向、手机地址、拨出的号码或数字来电号码、创建/接通/结束时间、状态、结束原因、收尾结果、错误、任务 ID、音色和音频字节数。状态变化和收尾时整体替换写入。
- `transcript.jsonl`：完整对话文字，逐条追加（角色 `caller`/`ai`、文字、开始/结束时间、事件 ID），不受内存缓存上限影响。
- `call.wav`，仅当 `PHONE_RECORD_CALLS=1`：24 kHz 立体声 PCM16。左声道是手机采集，右声道是 AI 播放。一侧没有声音的时间用静音补上，使两个声道保持同一时间轴。通话没有音频时不创建这个文件。通话结束后，`GET /v1/conversations/{id}/recording` 返回这个文件，控制台在通话详情里播放。

会话记录保存拨出的号码；来电在手机报告数字来电号码时保存该号码。转写、任务文件和日志不含号码。设备通话列表和手机状态事件不含号码或姓名。除非 `PHONE_RECORD_CALLS=1`，否则不保存通话音频。写盘失败只记录诊断，不影响通话。存盘文件包含对话内容，属于敏感数据；目前没有自动清理，也没有删除 API，需要时由管理员手动处理数据目录。

### 兼容的低层接口

下列接口供旧调用者和内部 worker 复用。受管入口拒绝无 task 拨号，外呼用 `/v1/conversations`。低层接口只有一个全局租约；受管通话占用线路时，`POST /v1/lease` 返回 409 `controller_busy`。AI worker 通过私有文件描述符（`PHONE_AUDIO_FD`）收发音频，不使用低层租约。`X-Controller-Lease` 是防争用机制，不是鉴权。

所有响应都是 JSON。HTTP 错误格式为 `{"error":{"code":"..."}}`。蓝牙地址格式是 `AA:BB:CC:DD:EE:FF`。

- `GET /v1/capabilities`：返回 `bluez`、`nativeTelephony`、`phones`、`audio` 和 `control`。`phones[].methods` 是实际对象支持的方法。没有目标手机映射时，不会声称它可拨号。
- `GET /v1/devices`：返回 `devices`，含 `address`、`paired`、`connected`。
- `GET /v1/devices/{address}/calls`：返回 `calls`，含 `id`、`state`、`multiparty`。不返回号码或姓名。
- `POST /v1/lease`：取得唯一控制租约，返回 `{"lease":"...","expiresIn":30}`。已有租约时返回 HTTP 409。
- `PUT /v1/lease`：续期 30 秒。需要 `X-Controller-Lease` 请求头。
- `DELETE /v1/lease`：释放。需要相同请求头。先关闭关联音频并等待清理，再允许新客户端取得租约。
- `POST /v1/devices/{address}/connect`、`disconnect`：原生 BlueZ 连接控制。
- `POST /v1/devices/{address}/calls`：拨号，JSON 为 `{"number":"+123456"}`。号码仅在内存和 D-Bus 调用中使用。部分版本返回 `{"id":"call1"}`；无返回值的版本返回 `{"ok":true}`，随后读取 `calls`。
- `POST /v1/devices/{address}/calls/{callId}/answer`、`hangup`：接听或挂断选定电话。只能操作此手机报告的 call ID。受管来电由 Server 自动接听，不需要调用此接口。
- `POST /v1/devices/{address}/tones`：DTMF，JSON 为 `{"tones":"123#"}`。

所有 `POST` 电话控制操作均需要 `X-Controller-Lease`。每次操作按当前地址属性查找实际电话对象；不会假定 `ag1` 属于目标手机。

### 音频

`WS /v1/audio?address=AA:BB:CC:DD:EE:FF` 需要 `X-Controller-Lease`。同一时间只有一个音频客户端，并且不能和受管通话同时使用。约每 10 秒续期一次；过期或断线时清理本次音频。受管会话负责收尾电话，单独关闭低层音频不等于挂断。

先等待文本事件 `{"type":"audio.ready"}`，再发送二进制帧。ready 只在选定手机的 SCO 双向端口连接成功后发送。最多等待 45 秒；电话尚未使用蓝牙音频时可能超时。

双向二进制帧均为 PCM16、小端、单声道、24000 Hz，不含容器头。每帧最多 960 字节，即 20ms；不可为空或含半个采样。输入可使用更短的偶数字节帧。

音频启动时，网关各读取一次 `pw-record --help` 和 `pw-play --help`。工具支持 `--raw` 时，启用此选项以选择无容器头的 stdin/stdout PCM 路径。PipeWire 1.4.9/1.4.11 需要此选项；`--format s16` 本身不选择原始模式。PipeWire 1.2.7 工具帮助没有 `--raw`，网关保留旧版已验证的命令。此检查不在逐帧路径中执行。新版工具仍需要真实设备验证；命令构造测试不能替代该验证。

每个应用方向队列最多 10 帧（200ms）。队列满时最多等待 200ms，让正常输出突发继续播放。阻塞超时返回 `{"type":"error","code":"audio_backpressure"}` 并关闭，不静默丢弃语音。WebSocket 写入缓冲和 OS/PipeWire 缓冲另有开销；此队列限制不是总延迟承诺。

网关使用 `--latency 20ms` 请求 PipeWire 节点延迟。PipeWire 1.2.7 和 1.4.x 支持此选项。它不保证无卡顿；较低延迟也可能增加欠载。音频格式错误返回 `invalid_pcm_frame`；框架拒绝超大消息时可能直接以 WebSocket 1009 关闭。SCO 结束时发出 `{"type":"audio.closed"}`。其他错误发出 `{"type":"error","code":"..."}`，随后关闭。

### 状态事件

`WS /v1/events` 返回文本事件 `{"type":"phone.state","devices":[...],"phones":[{"address":"...","calls":[...]}]}`。连接和手机状态事件触发快照，不做定时电话查询；无法查询时 `calls:null` 表示未知，不是已挂断。事件不含号码或姓名。

管理 Client 使用 HTTP API 执行配对、连接、拨号和指令；`WS /v1/console/events` 推送管理状态，`WS /v1/conversations/{id}/monitor` 提供双方音频副本。UI 状态连接断开会重连，不结束后台电话。

AI worker 使用 `WS /v1/conversations/{id}/events` 接收 `{"type":"call.state","state":"active","lastKnownState":"active","revision":1}`。每次连接先发送当前快照，再发送变化。兼容的 `GET .../call-state` 只读 Server 缓存，不访问手机。

Mac 使用 HFP `call/callsetup/callheld` 通知，Linux 使用 PipeWire Telephony 的 D-Bus 对象及属性信号。连接建立、状态通知和恢复时，由 Server 核对当前通话身份；成功后不定时查询。一次事件核对失败最多重试两次，然后保留 `unknown`，等待新事件，不关闭健康的语音或 AI。真实结束、通话身份变化及实际音频链路故障仍按各自流程收尾。WebSocket 心跳和控制租约续期不是手机状态查询。

## 测试与限制

仓库根目录的完整离线验证入口是 `python3 scripts/check.py`。先安装 `server/requirements-test.txt` 和 AI 的 npm 依赖；它会运行共用、Mac、启动器、真实 worker 替身回归、类型及 Client 语法检查，见根目录 README。只运行下面的共用部分不能代表全部通过。

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s server/tests -v
```

测试只使用模拟电话适配器和模拟子进程，不会拨打真实电话。覆盖无鉴权读接口、控制租约、地址映射、不支持的能力、单音频所有者、PCM 格式、背压和清理。

较早的 Linux 版本已在树莓派上用 Android 手机和 iPhone 完成真机检查：拨打指定号码、接听状态、挂断，以及本机 Live 客户端经 API 的 90 秒双向音频数据。iPhone 需要常驻配置中的 HFP AG 路由规则，否则通话流会自动接入 Pi 本机播放和音频回采。锁屏拨号和双 SIM 未测试。网络、蓝牙编解码、模型响应时间和系统负载仍会影响端到端延迟。

### 多部手机

多部手机可以同时连接。电话控制先按蓝牙地址查找原生对象；`call1` 等 call ID 只在该手机下有效，不是全局标识。音频只连接所选地址与本次私有 PCM 节点；不得改为匹配所有蓝牙音频端口。

同一时间只有一路通话。没有可调的通话路数设置。前后两通、不同手机之间的事件和错误按会话与代次隔离，旧事件不能修改新通话。Mac 目前只保持一个目标连接。

接口依据：[PipeWire Telephony](https://gkiagia.gr/2025-02-20-pipewire-telephony/)。
