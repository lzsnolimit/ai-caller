[English](README.md) | 简体中文

# Server AI 配置

电话的语音模型和推理模型分开：

- `gpt-live-1`：持续接收和生成通话音频，默认 voice 为 `marin`。
- `gpt-6-luna`：默认的 Responses 推理后端。

两种启动方式使用同一默认值：`run-gpt-live.py` 启动器，以及已经提供模型连接环境变量的直接 Node worker。

## API 密钥与服务地址

请使用你自己的 OpenAI API 密钥。密钥只从环境读取，不保存或打印。

- 在 Server 进程环境中设置 `OPENAI_API_KEY`。
- 设置 `OPENAI_BACKEND_MODEL` 可更换电话的推理模型，默认 `gpt-6-luna`。
- 可选：启动器可以读取 Codex 配置 `~/.codex/config.toml`（或 `$CODEX_HOME/config.toml`），使用其中的 API provider、`base_url` 和密钥变量名（`env_key`）。`base_url` 必须是普通 HTTPS URL。没有该文件时，使用 OpenAI 默认地址和 `OPENAI_API_KEY`。不使用 Codex 的 `model` 值。
- 受管 worker 使用环境变量 `PHONE_NODE` 指定的 `node`（Mac 启动器自动设置为它自己的 `node`），未设置时从 `PATH` 查找。

```sh
cd server/common/ai
npm ci
# 默认 gpt-6-luna；不需要设置模型环境变量。
python3 run-gpt-live.py --gateway http://127.0.0.1:8765 --address AA:BB:CC:DD:EE:FF
```

上面的命令只连接既有通话的音频，不主动拨号。普通管理控制台由仓库根目录的 `python -m server` 启动，每次拨号前保存 task。

项目没有设置 `reasoning.effort` 或服务等级。GPT-6 Luna 文档中的默认推理强度为 `medium`；你的 provider 实际使用的配置需要实测。更换模型不保证每个任务更快。

## 诊断日志

日志默认开启，保存于 Server 的 `PHONE_DATA_DIR/logs/calls.jsonl`；未设置数据目录时为 `~/.local/share/phone-call-gateway/logs/calls.jsonl`。每个文件最多约 2MB，保留 3 个备份；目录 700，文件 600。后台线程写盘，256 条有界队列满时丢弃诊断而不阻塞电话，`droppedLogs` 显示丢失和写入失败。日志不是任务正文或通话录音。

按 `conversationId` 查找，同进程用 `sequence` 和 `elapsedMs` 排序；跨进程只用 `timestampMs` 粗略关联，不能当作精确时延。记录：模型连接/就绪、接听、后端响应开始/完成/失败与耗时、粗略输入/输出音量活动、每 10 秒的字节/缓冲统计、终态及 worker 退出。

- `resolvedBackendModel` 只证明配置回显，不证明调用。
- `backend_response` 的 `observedBackendModel` 来自实际嵌套的 `response.event`；`backendResponseIndex` 可对齐开始和结束。`observedBackendModels`、`backendCreated/Completed/Failed` 为累计摘要。
- `finalized: true` 表示收到模型终态，不代表任务目标完成。异常断线或 `droppedLogs > 0` 时证据可能不完整；缺少事件不能直接当作“没有调用”。
- `audio_activity` 是 100ms 节流的音量阈值采样，不是 VAD、准确的说完时间或手机实际播放时间。`deliveryGap: true` 表示至少 300ms 没收到该方向的采样，不证明手机已停止播放。不得据此自动切断语音。

`/v1/conversations` 与详情 API 也返回 `diagnostics` 摘要。只记录许可字段，不保存号码、设备地址、模型原始 ID、任务/Prompt、转写、音频或密钥；不打印 provider 的原始错误消息。

参考：[GPT-6 Luna](https://developers.openai.com/api/docs/models/gpt-6-luna)、[GPT-Live 推理委派](https://developers.openai.com/api/docs/guides/live-delegation)。

## 公共 AI 与主音频通道

`session.ts` 的 `createAISession(audio, options)` 不负责 OS、蓝牙、拨号、HTTP 服务或进程。宿主提供 `DuplexAudio`：发出 `ready`、`pcm`、`callState`、`ended`、`failure` 事件，并实现 `send`、`close`、`queuedBytes`。PCM 为 24 kHz 单声道 PCM16LE。返回对象接受文字指令并关闭会话。模型、Prompt、接听门控和终态处理共用同一实现。

Mac 与 Linux 的受管入口都经 `Conversations.spawn_worker` 使用 `WorkerAudio`。每个会话有独立的进程、socketpair、线路所有权和 CallState 订阅。Python 直接调用平台的 start/read/write/monitor/close；主音频不经过公共 HTTP 音频路由、WebSocket 帧编解码和两层 API 音频队列。进程间的一次二进制复制仍然存在。

两个方向由不同任务执行。私有输出使用有界 FIFO，每次只提交一个 960 字节以下的帧，完成回调后立即续发，没有固定发送等待。待发送 PCM（含正在写的帧）最多 48000 字节，即一秒音频；单次写入 2 秒未完成则失败。这与模型输入 writer 的预算一致，用于吸收短时突发，不依赖操作系统 socket 的瞬时积压大小。持续过载会明确结束音频，不丢弃主 PCM 来维持表面正常。操作系统和平台另有各自的缓冲，这些限制不等于端到端延迟保证。Client 每个监听最多 5 个副本包；发送超过 100ms 时只结束该监听。内部线路所有权随进程和音频清理释放，不靠网络租约续期；公共租约不能抢占该线路。

## 如何定位延迟

`audio_stage` 每 10 秒按阶段汇总，结束时补写剩余统计。字段包括 `frames`（观察次数）、`bytes`、`meanMs`、`maxMs`、`peakDepth`。它们是区间统计，不是分位数。Node 输出还带累计的 `audioSequence`；后端响应沿用匿名的 `backendResponseIndex`。所有日志由 `conversationId` 归属。

| 阶段 | 实际测量边界 |
| --- | --- |
| `hfp_capture_process` | Mac 收到 SCO PCM 后，重采样、入队与准备返回音频的主机执行时间 |
| `hfp_capture_queue` | Mac 捕获帧入队到 common 读走；深度单位为帧 |
| `capture_transport` | common 写入私有通道到本地 drain 返回；深度单位为字节，不代表 Node 或模型已收到 |
| `model_input_send` | Node 接收输入帧到模型 WS send 回调，含本地模型发送队列等待 |
| `model_connect` / `model_session_setup` | 创建模型连接到 WS open / session.started |
| `model_first_output` | session.started 到该会话首次 output_audio.delta；包括等接听、开场和模型等待，不是每轮推理耗时 |
| `backend_response` | 同一匿名后端响应从 created 到 completed/failed 等终态，已有模型事件证据 |
| `model_output_gap` | 相邻模型音频 delta 的到达间隔，也包括正常静音，不自动解释为故障 |
| `model_output_handoff` | Node 收到模型 PCM 后调用音频 transport.send 的执行时间；不是传输完成 |
| `private_audio_send` | 模型输出入私有 FIFO 到本地 socket 写完成回调，含该 FIFO 等待；不代表平台已写入或手机已播放 |
| `playback_handoff` | common 收到私有 PCM 帧到平台 write 返回；含平台背压等待 |
| `hfp_playback_queue` | Mac 已转换的播放段入队到由 SCO 回调取走；深度为剩余 8 kHz 字节 |
| `hfp_host_send` | Mac SCO 回调中，准备输出到 host.send_sco_sdu 返回；不是无线发送确认或手机出声 |
| `hfp_underrun` | Mac 输出不足时填充的静音字节，正常静音也可能出现 |
| `monitor_enqueue/drop/send/failure` | 监听副本入队、丢弃、WS send 完成耗时或失败 |
| `input_queue_clear` / `playback_queue_clear` | 关闭时本地输入/播放队列被清除的字节 |

Python `clockDomain=python_monotonic`，Node `clockDomain=node_performance`。同进程用单调时钟测持续时间；不得将两个进程的 `elapsedMs` 直接相减。`timestampMs` 是 Python 收到或写入元数据的墙上时间，只用于粗略关联，不用于推算跨进程的精确单向延迟。目前没有逐帧的跨进程时间校准，不能把以上各均值拼成端到端时间。

手机采集到主机收到、无线传输、provider 内部推理、手机实际播放均未测量。Linux 上可用 common 交付统计；PipeWire 内部队列和实际设备发送没有额外观测。需要这些时间时，可用同步的手机时间戳或外部声学参考独立测量。

没有独立的 provider 打断/清空命令，音频与关闭语义保持不变，项目不宣称打断功能或每话轮响应延迟。关闭时队列的清空记录不等于模型打断确认。

日志写盘使用 256 条有界后台队列；Node stdout 待写超过 64KiB 时丢弃诊断记录，以 `workerDroppedLogs` 计数；Python 队列或写盘丢失使用 `droppedLogs`，两者分开保存。日志输出失败或监听挂起不能阻塞主音频。日志全部失效时，无法保证最后的计数落盘；运行时的 `droppedLogs` 和已落盘记录需一起看。

私有输出的真实过载分别记录为 `private_audio_queue_full`、`private_audio_send_timeout` 或 `private_audio_send`，不归为模型事件格式错误。私有通道上的 `model_output_handoff.peakDepth` 和 `gatewayQueuedBytes` 指待发送的 PCM 字节（包含在途帧，不含 5 字节帧头），不只是 socket 的瞬时 `writableLength`。

## 通话中的操作员指令

操作员指令使用 GPT-Live `session.instructions.append`，`delegation_id=null`，`event_id=operator_<commandId>`。问候等可播报的内容仍使用 commentary。依据：[服务端控制](https://developers.openai.com/api/docs/guides/voice-server-controls) 及 [Live 事件参考](https://developers.openai.com/api/reference/resources/live/sideband-websocket/)。`session.instructions.appended.client_event_id` 确认指令进入上下文；`error.client_event_id` 关联单次拒绝。确认不保证模型遵从、具体话语或音频已播放。指令保留在当前对话历史中并进入后续 backend delegation，不修改已保存的任务快照。

Server 先登记命令再写给 worker，API 保留 ID、`acceptedAtMs`、`updatedAtMs` 和状态：`accepted`（服务接收）、`written`（模型连接写出）、`injected`（模型确认注入）、`rejected`、`unknown`。发送失败时可能无法确定模型是否收到，因此记录 `unknown`。worker 等待回执最多 10 秒，Server 对缺失的 worker 状态最多等待 12 秒；结束时清理等待。晚到和重复的回执不改终态，也不触发重发。单次指令被拒绝不会结束通话或双向音频。

本地输入限制为非空且最多 4096 字符，不按字节数推算 token，也不截断。文档中的内容限制为 500 tokens，由 provider 校验；收到关联错误时仅将该命令标记为 `rejected`，不终止通话、不自动重发。每个会话最多 128 条命令、同时最多 32 条等待，超过返回 `command_limit`。CLI 与实际发送均沿用 4096 字符上限；写出仅标记 `written`，必须收到关联回执才标记 `injected`。

`operator_command` 元数据只记录会话、命令/事件 ID、状态、时间、延迟及固定的错误分类，不写指令内容、任务、转写或 provider 原始错误。`durationMs` 使用 Server 单调时钟，`workerElapsedMs` 使用 worker 单调时钟；不同进程的起点不能相减。这里不测量模型是否遵从，也不测量对方实际听到的时间。日志仍走已有的有界非阻塞队列。

## 按通话选择音色

Client 在拨号前读取 `GET /v1/voices`。`POST /v1/conversations` 可添加 `"voice": "quartz"`；省略时使用设备在本进程内的选择，初始为 `marin`。Server 在任何拨号或占线前拒绝未知值、空值或错误类型。所选值写入私有任务快照，再经 managed init 传给 `createAISession` 和 `session.start.audio.output.voice`。已有通话的音色不变。

名称、语言和地区来自 [GPT-Live 官方音色资料](https://developers.openai.com/api/docs/guides/live-conversations#voice-options)。项目支持已保存样本的 13 个音色：`marin`、`quartz`、`ripple`、`vesper`、`willow`、`stone`、`gleam`、`meridian`、`bossa`、`tempo`、`beacon`、`delta`、`cinder`，默认 `marin`。这是项目支持的子集，官方目录中还有更多音色。其他值在拨号、试听和设备偏好 API 中都会在产生副作用前返回 `invalid_voice`。生成工具使用同一目录，项目样本清单与这 13 项一致。语言和地区表示声音风格，不限制对话语言，也不保证中文口音。目录中的 `providerVerification` 为 `bundled_sample` 时表示有已验证格式与哈希的项目样本；`preview_succeeded` 表示本进程生成过样本；`not_verified` 表示两者均无。项目样本不证明你的账号、所有语言或所有手机的效果。

`GET/PUT /v1/devices/{address}/voice` 读取或更新已配对设备在当前 Server 进程内的默认音色，PUT 内容为 `{"voice":"quartz"}`。它不会拨号、占用线路或永久保存。创建通话时省略 voice 则使用此值，明确提供的有效 voice 优先；任务快照创建后不再变化。删除设备清除选择，服务重启恢复 `marin`。

`POST /v1/voices/{id}/preview` 返回 `audio/wav`。优先读取 `server/common/voice-samples` 中校验通过的项目样本；重启后、离线或没有 provider 配置时也可播放，不调用云服务。缺少样本时才通过服务端凭据创建独立的 `gpt-live-1` 短会话，会产生 Live 费用和网络等待。它不创建自定义音色，不使用手机、HFP、线路租约、当前通话的 task 或客户端麦克风。只有用户点击才会播放；浏览器阻止播放时显示手动播放器。

试听总时限为 20 秒，音频最多 8 秒。连续音频达到 8 秒时返回截取的片段并关闭试听会话，不等待语音自然结束。收到音频后若 1 秒没有下一块，则关闭试听会话；这是短片段截取策略，不是 provider 的语音完成信号。试听可能只包含句子的一部分。Server 同时只生成一个音色；同音色请求合用一次生成，其他请求收到 `voice_preview_busy`。生成缓存只在 Server 私有内存中；项目样本保留在磁盘。每个样本至多 384,044 字节。失败后 30 秒内不重复生成。停止 Client 播放或取消请求不会中断其他用户共用的生成；后台任务仍受 20 秒上限控制。服务关闭会取消生成。provider 拒绝、超时或音频错误会明确返回失败，不会换用另一个音色。音频、模型密钥和 provider 原始错误不进入普通日志。

需要补充缺失的项目样本时，运行 `python scripts/generate-voice-samples.py --server http://127.0.0.1:8765`（端口改为你的 Server 端口）。该工具按 API 的单个生成槽位串行请求，每个缺失音色只尝试一次，跳过已有的有效样本。可用 `--voices quartz` 限定目标；失败详情写入 `~/.cache/phone-call-gateway/voice-sample-generation.json`。工具只保存固定通用试听句的样本，不读取通话或任务音频；清单记录请求文本、来源、哈希和时长，不保证模型逐字说完。成功的样本由工具原子写入固定的音色文件；普通播放不会修改项目文件。测试使用私有临时样本目录；`PHONE_VOICE_SAMPLE_DIR` 可指定该目录，避免读取或改写交付的样本。设备默认音色只保存在 Server 进程内，不随项目音频持久化。

离线测试覆盖实际 Server → managed worker → 本地 TLS 模型的音色传递和两会话隔离；试听 TLS 测试同时验证原通话的双向 PCM。替身只证明接线和边界，不证明你的 API 账号支持目录中的全部音色，也不证明真实听感。
