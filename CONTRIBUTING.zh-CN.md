[English](CONTRIBUTING.md) | 简体中文

# 参与贡献

感谢你的帮助。本文说明如何准备开发环境、运行检查和提交 pull request。

## 开始之前

- 报告 bug 请提交 issue，并写明平台（Mac 或 Linux）、操作系统、Python、Node.js 和 PipeWire/BlueZ 版本、手机类型、操作步骤和错误码。
- 新功能或较大的改动，请先提交 issue 讨论。
- 安全问题不要提交公开 issue，请按 [SECURITY.zh-CN.md](SECURITY.zh-CN.md) 操作。

## 准备环境

需要 Python 3.11+ 和 Node.js 22.13+。在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r server/requirements-test.txt
npm --prefix server/common/ai ci
```

要连接真实手机运行网关，见 [快速开始](README.zh-CN.md#快速开始)。

## 运行检查

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check.py
```

该命令运行全部 Python 测试、Node 测试、TypeScript 检查和 Client 语法检查。提交 pull request 前，所有检查必须通过。测试是离线的，不会打开蓝牙、读取模型密钥或拨打电话。真实 worker 测试需要 `openssl`。

目前没有云端 CI。请在 pull request 中写明 `scripts/check.py` 的结果。

## 编码说明

- 改动保持小而聚焦。在产生问题的那一层修复，不为单个样本添加特殊处理。
- 共用行为放在 `server/common/`；平台行为放在 `server/mac/` 或 `server/pi/`。
- 网关只有一条通话线路。没有设计讨论，不要重新加入并行通话。见 [蓝牙多路通话复用探索记录](docs/bluetooth-multiplexing-exploration.zh-CN.md)。
- 不要记录电话号码、任务正文、对话文字、音频或密钥。密钥只从环境读取。
- 不要提交密钥、个人信息、真实蓝牙地址、电话号码或本机路径。示例中使用 `AA:BB:CC:DD:EE:FF` 和 `+123456`。
- 行为有变化时，添加或更新离线测试。测试不得依赖真实手机、蓝牙或模型密钥。
- 新代码和代码注释默认使用英文。
- 修改控制台中面向用户的文字时，同时更新 `client/assets/messages.js` 中的两种语言。
- 修改 README 描述的行为时，同时更新英文和中文文件。

## Pull request

- 每个 pull request 只处理一个主题。
- 说明改了什么、为什么改、如何测试。
- 写明在哪些平台上做过真机测试（Mac、Linux、Android、iPhone），哪些没有测试。
- 不要混入无关的格式改动。

提交贡献即表示你同意按 [MIT 许可证](LICENSE) 授权你的贡献。
