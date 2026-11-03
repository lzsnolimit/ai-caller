[English](README.md) | 简体中文

# Mac 内置蓝牙

此适配复用现有的网关 API 和 Live worker，不重写 AI。已在 macOS 26.4、BCM_4388C2 PCIe 内置控制器和一部 Android 手机上完成 HFP 拨号、SCO 双向语音和 AI 通话测试。其他 Mac 控制器、iPhone 经 Mac 的音频和多路同时通话未测试。

需要 Node.js 22.13+、Python 3.11+、Xcode Command Line Tools 和 `/opt/homebrew/bin/blueutil`。从仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-mac.txt
npm --prefix server/common/ai ci
.venv/bin/python server/mac/native/build.py
export OPENAI_API_KEY=<你的密钥>
.venv/bin/python -m server --port 8765
```

编译产物默认位于 `~/.cache/phone-call-gateway/mac-native/`，不写入仓库。`PHONE_MAC_NATIVE_DIR` 可指定其他私有目录。Python 3.13+ 使用依赖中的 `audioop-lts` 保留流式采样率转换接口。

该命令将 API 和控制台绑定到 `127.0.0.1:8765`；Mac 上网关只接受 `127.0.0.1`。在控制台中选中并连接手机，再保存 task 拨号；服务可在手机未连接时启动。接听状态来自原生 HFP，不用 ADB；状态不显示号码。Mac 上网关只保持一个目标连接。连接由网关发起，手机不能主动连接 Mac 网关。

AI 由 Server 内部 worker 启动，默认 Live + Luna，见 [AI 配置](../../common/ai/README.zh-CN.md)。旧的单手机 CLI 仍可用：`PHONE_MAC_PYTHON=.venv/bin/python node server/run-mac.cjs AA:BB:CC:DD:EE:FF`，但它不是任务控制台的入口。取消受管会话会收尾本次电话；单独关闭旧的低层 PCM 不等于挂断。

AI 音频只在手机和模型之间传输，不接 Mac 本机声卡。SCO 为 8 kHz PCM，网关保持转换状态，并对系统其他部分提供 24 kHz PCM。无 AI 输出时发送静音；持续过载会明确失败，不静默丢弃语音。本地不保存模型密钥。对话文字由共用 Server 存盘，见 [Server 说明](../../README.zh-CN.md#持久化与隐私)。

独立蓝牙栈需要暂时关闭系统蓝牙，释放内置控制器。服务运行期间，系统蓝牙不能同时使用该控制器。退出时先停止原生进程，再恢复并读回系统蓝牙电源。不要中断父进程的电源恢复流程。无法捕获的 SIGKILL 或机器掉电不能保证恢复。

新配对密钥只存入本地私有文件 `<数据目录>/devices/pair-keys.json`（权限 600，默认数据目录为 `~/.local/share/phone-call-gateway/`）。该文件在仓库之外，`.gitignore` 也排除了 `pair-keys.json`。新的独立配对可能替代手机原有的 Mac 系统配对；回到系统蓝牙时可能需要重新配对。不会读取、导出或删除其他设备的配对密钥。

Mac 离线测试：

```sh
PYTHONPATH=.:server PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s server/tests_mac -v
```

这些测试使用电话和传输替身，不拨号，不打开蓝牙或本机音频设备。
