# Spitch

> Linux 桌面下的全局热键中文语音输入工具，由豆包（火山引擎）实时 ASR 驱动。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Wayland | X11](https://img.shields.io/badge/display-Wayland%20%7C%20X11-green.svg)](#系统要求)

按住 **Ctrl + Alt** 说话，松开自动把带标点的中文（或任意 Unicode）粘进当前应用。**不依赖 IBus / fcitx5 等输入法框架**，与你已有的拼音 / 五笔输入法和平共存。Wayland、X11 都可用。

```
按住 Ctrl+Alt ──▶ 🎙 录音 ──▶ ✍ 转写 ──▶ 自动粘到光标位置
```

---

## 特性

- **全局热键**：默认 `Ctrl+Alt` 双键长按；按下第三键（如 `Ctrl+Alt+T`）自动取消，让系统快捷键正常生效
- **真·实时 ASR**：豆包 BigModel 实时端点，自带标点 + 数字归一（ITN），中文一次性输出
- **绕过 IM 框架**：转写结果走"剪贴板 + 合成 `Ctrl+Shift+V`"，在 GTK / Qt / Electron / 原生 Wayland 应用里都能粘——飞书、微信、VS Code、Chrome 地址栏、Slack 全都覆盖
- **系统托盘**：libayatana-appindicator，三态图标（空闲 / 录音中 / 正在转写）
- **配置 UI**：GTK 对话框；缺 PyGObject 时自动退到 CLI 提示
- **凭据安全**：配置文件 chmod 600 + 原子写；凭据指纹绑定 verified 状态，改了 key 自动失效
- **Wayland 与 X11 双栈**：自动选 `wl-copy` / `xclip` / `xsel`

## 工作原理

1. 用户态长进程 daemon 监听 `/dev/input/event*`，等配置好的修饰键组合
2. 按下时打开麦克风，PCM 流通过 WebSocket 推给豆包
3. 松开时等 server 给出 `definite=true` 的最终结果（最长 5 秒，可调）
4. 把结果写进剪贴板，等用户物理松开热键后，通过 `/dev/uinput` 合成 `Ctrl+Shift+V` 触发粘贴
5. 约 0.3 秒后还原原剪贴板，避免污染你下一次手动粘贴

整条链路与输入法框架无关——**不用换输入法、不用改 IBus 设置、不用 fcitx5 插件**。

## 系统要求

- Linux + Wayland 或 X11（已在 Ubuntu 24.04 / GNOME 46 上验证）
- Python 3.10+
- 系统包：`python3-evdev` + 剪贴板工具（Wayland 装 `wl-clipboard`，X11 装 `xclip` 或 `xsel`）
- 当前用户在 `input` 组（一次性 `sudo usermod -aG input $USER` + 重登）
- `/dev/uinput` 当前 session 可写（Ubuntu 24.04 logind 自动配 ACL；其他发行版可能要 udev 规则）
- 火山引擎 BigASR 的 `app_key` + `access_key`（[在控制台申请](https://www.volcengine.com/docs/6561/1354869)）

## 快速开始

```bash
git clone https://github.com/pekinlcc/Spitch.git
cd Spitch

# Wayland 用户
sudo apt-get install -y python3-evdev wl-clipboard
# X11 用户用下面这条
# sudo apt-get install -y python3-evdev xclip

./scripts/install.sh
spitch-config        # 填豆包凭据，点 "Test connection" 验证
sudo usermod -aG input $USER     # 一次性，然后重登
spitch-daemon &      # 按住 Ctrl+Alt 说话，松开后自动粘贴
```

完整安装流程（含 systemd 自启动、ACL 排错等）见 [`docs/INSTALL.md`](docs/INSTALL.md)。

## 配置

配置文件路径：`~/.config/spitch/config.json`（chmod 600）。常用字段：

| 字段 | 含义 | 默认值 |
|---|---|---|
| `doubao.app_key` | 火山引擎 BigASR 的 APP ID | — |
| `doubao.access_key` | 火山引擎 BigASR 的 Access Token | — |
| `doubao.endpoint` | WebSocket 接入点 | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| `audio.sample_rate` | 麦克风采样率 | 16000 |
| `hotkey.talk_key` | 按住说话的修饰键组合 | `Ctrl+Alt` |
| `inject.paste_keystroke` | 粘贴用的合成快捷键 | `Ctrl+Shift+V` |
| `inject.final_wait_seconds` | 等 server 出 final 的最长秒数 | `5.0` |

修改后重启 daemon 生效。

## 常见问题

**热键按下没反应？**
看 `~/.local/state/spitch/daemon.log`。最常见的原因是用户没在 `input` 组：`id | grep input` 验证；没有的话 `sudo usermod -aG input $USER` 后重登。

**粘贴失败？**
托盘通知会写明真实原因（缺 wl-clipboard / `/dev/uinput` 不可写 / 键名错误）。`getfacl /dev/uinput | grep $USER` 检查 ACL。

**剪贴板被乱填？**
Spitch 会在粘贴前保存原剪贴板，约 0.3 秒后还原。如果你的目标应用消费粘贴较慢，原剪贴板可能在被消费前覆盖回去；增大 `src/spitch/inject/text_injector.py` 里的 sleep。

**怎么换热键？**
`spitch-config` 的 *Push-to-talk key* 字段，或直接编辑 `config.json` 的 `hotkey.talk_key`。**目前只支持修饰键双键组合**（`Ctrl/Alt/Shift/Super` 任选两个），不支持单键 / 字母键作为长按热键。

**第三键取消是什么意思？**
按住 `Ctrl+Alt` 期间如果再按字母（比如要 `Ctrl+Alt+T` 开终端），录音自动作废、热键正常作为系统快捷键生效。让你不用为了用 Spitch 而避开常见系统快捷键。

**飞书 / 微信里粘出来是空的？**
极少数 Electron 应用对剪贴板 MIME 类型敏感。先确认 `wl-paste` 在那个应用聚焦时能拿到 Spitch 写的文本；不行的话开 Issue 贴上桌面环境信息。

## 开发

```bash
# 单元测试（stdlib unittest，零额外依赖）
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 端到端烟雾测（mock 豆包服务器 + 可选真实麦克风）
tests/e2e_smoke.sh
```

测试覆盖：
- 二进制帧编解码 (test_doubao_protocol.py)
- 配置读写 + verified 指纹 (test_config.py)
- WebSocket 流式协议 (test_doubao_client_mock.py)
- 推到说控制器状态机 (test_voice_controller.py)

## License

MIT — 见 [LICENSE](LICENSE)。

---

## English

**Spitch** is a global-hotkey voice input tool for Linux desktops, powered by Doubao (Volcano Engine) realtime ASR. Hold **Ctrl+Alt** to talk, release to commit punctuated text into the focused app via clipboard + synthetic `Ctrl+Shift+V` — bypassing the input-method framework entirely. Works on Wayland and X11 alike, in any GTK / Qt / Electron / native-Wayland app, and coexists with whatever IBus / fcitx5 setup you already have.

### Quick start

```bash
git clone https://github.com/pekinlcc/Spitch.git
cd Spitch
sudo apt-get install -y python3-evdev wl-clipboard   # or xclip on X11
./scripts/install.sh
spitch-config                                        # paste your Doubao credentials, click Test
sudo usermod -aG input $USER                         # one-time, then relogin
spitch-daemon &
```

### Status

v0.2.1 — voice path with Wayland + X11 clipboard support, hardened concurrency. See [`docs/INSTALL.md`](docs/INSTALL.md) for the full English setup guide and [`CHANGELOG.md`](CHANGELOG.md) for release history.

### License

MIT.
