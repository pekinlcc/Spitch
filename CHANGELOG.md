# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

## [0.5.2] — 2026-05-05

应用菜单 launcher——装完之后 Spitch 会出现在 GNOME Activities / KDE 应用程序菜单里，带图标，点击直接打开控制台的"设置" tab。之前需要终端跑 `spitch-console`，对非命令行用户不友好。

### 新增

- **`data/spitch.desktop.in` 模板**：标准 freedesktop.org `.desktop` 文件。中英双语 `Name` / `GenericName` / `Comment` / `Keywords`（支持搜"Spitch"、"语音输入"、"听写"、"voice"、"dictation"等）。`Categories=Utility;Audio;AudioVideo;` 让它在分类菜单里也能找到。
- **`spitch-console --tab {history,log,settings}` 参数**：默认 `history`（终端跑保持原有行为）；`.desktop` 文件指定 `--tab settings`，从应用菜单点开就直接看到设置面板（开机自动启动 checkbox + 各项可调参数）。
- **`install.sh` 自动装 launcher**：
  - 图标 → `~/.local/share/icons/hicolor/scalable/apps/spitch.svg`
  - .desktop → `~/.local/share/applications/spitch.desktop`（绝对路径替换占位符，避免 DE 启动时 `$PATH` 不全）
  - 触发 `gtk-update-icon-cache` + `update-desktop-database` 让 GNOME / KDE / Cinnamon 立即看到，无需 logout
- **窗口图标绑定**：`spitch-console` 调 `Gtk.Window.set_icon_name("spitch")`，alt-tab / overview 都显示一致的图标
- **`uninstall.sh` 同步清理** .desktop + icon + 触发数据库刷新

### 测试

141 个单元测试全部通过（与 v0.5.1 相同——本次改动是打包 / UI 入口，不需要新测试）。

### 兼容性

无破坏。已经手动用过 `spitch-console`（无 `--tab`）的用户不受影响。重新跑 `./scripts/install.sh` 会幂等安装新的图标 + .desktop。

---

## [0.5.1] — 2026-05-04

控制台设置 tab 加了"开机自动启动 daemon"的 checkbox——把 `docs/INSTALL.md` 里手动的 `systemctl --user enable` 那一节做成图形界面。

### 新增

- **`spitch.autostart` 模块**：封装 systemd user unit 的写入 / `daemon-reload` / `enable --now` / `disable --now` 逻辑。提供 4 个函数：
  - `is_supported()` — 这台机器有没有可用的 systemd `--user` 实例（非 systemd 发行版返回 False，控制台据此把 checkbox 灰掉 + 显示 tooltip）
  - `is_enabled()` — 当前是不是 enabled
  - `enable()` / `disable()` — 切换状态，返回 `(ok, 中文 message)` 给 UI 直接展示
  - 单元文件位置：`~/.config/systemd/user/spitch.service`，由 Spitch 拥有，可随时手动编辑或 `disable --now` 后删除
- **控制台设置 tab 的 "开机自动启动 daemon" checkbox**：
  - 启动时显示当前实际状态
  - toggle 失败时（如缺 input 组、launcher 不存在）自动回滚到真实状态，并在状态行显示具体错误
  - 重入保护：错误回滚时 `set_active()` 不会再次触发 handler

### 测试

141 个单元测试全部通过（v0.5.0 是 123 个）。新增 `tests/test_autostart.py`：18 个测试，覆盖 unit 文件渲染（必含 sections / 绝对路径 / graphical-session.target / Restart=on-failure）、XDG path 解析、`systemctl` 不存在 / 超时 / user bus offline 各种 fallback、enable / disable 的 idempotent 性质（mock subprocess.run 不依赖真实 systemctl）。

### 兼容性

无破坏性变更。已经手动跑过 `systemctl --user enable spitch.service` 的用户：checkbox 启动时会自动反映"已启用"状态；下次 toggle 也直接 work（unit 文件会被重写为新内容，`After=graphical-session.target` 等内容跟手动版本完全一致）。

### 注意

- Auto-start 跑起来后**仍然要求用户在 `input` 组**（读 `/dev/input/event*`）。日志里会出现 `no readable keyboard devices` 错误。如果遇到，先 `sudo usermod -aG input $USER` 然后重登。
- 非 systemd 发行版（Devuan / Alpine OpenRC 等）不支持，checkbox 会自动灰掉。

---

## [0.5.0] — 2026-05-04

**控制台 / 历史 / 重粘**——把 daemon 从"只能按住说话"扩成"还能管理已识别的内容"。本版前所有失败的转写（被截断、注入到错应用、识别错）都只能再说一遍；从 v0.5 起每段都进历史，托盘 / 命令行 / GTK 三种方式都能补救。

### 新增

- **`HistoryRing`** (`src/spitch/history.py`)：daemon 内存环形缓冲（默认 50 条），同步持久化到 `~/.local/state/spitch/history.jsonl`（chmod 600，跨重启保留）。每段转写自动写入：时间、内容、识别耗时、注入是否成功、目标窗口名。
- **Unix socket 命令通道** (`src/spitch/cmdsock.py`)：daemon 在 `$XDG_RUNTIME_DIR/spitch.sock` (chmod 600) 监听 JSON-line 协议，支持 `ping` / `list` / `repaste` / `delete` / `clear`。
- **`spitch-cli`** 命令行工具：
  ```bash
  spitch-cli list           # 列历史
  spitch-cli repaste        # 重粘最近一次
  spitch-cli repaste --index 3
  spitch-cli delete 5
  spitch-cli clear
  ```
  设计目的：把 `spitch-cli repaste` 绑到 GNOME Settings 的自定义快捷键（推荐 `Super+Z`），失败/再发一遍一键补救。
- **`spitch-console`** GTK 三 tab 控制台：
  - **历史** tab：行选 → 复制 / 重粘 / 删除；双击重粘
  - **日志** tab：实时 tail `~/.local/state/spitch/daemon.log`，自动滚动 + 复制按钮
  - **设置** tab：热键 / 粘贴键 / 还原延迟 / 预缓冲 / final 等待 等常用配置图形化（凭据不动，仍走 spitch-config）
- **托盘菜单**新增两项："打开控制台"、"重粘最近一次"
- **`history.capacity`** 配置项（默认 50）
- **`target_app` 字段**：用 `xdotool getactivewindow getwindowname` best-effort 抓焦点窗口名（X11/XWayland），写进历史，方便在控制台里看"这段是粘到 Claude Code 的还是粘到飞书的"

### 内部

- daemon 启动时构造 `HistoryRing` 加载持久化历史 + 启动 `CmdServer` 后台线程
- `_finalize_and_inject` 抽出 `_inject_text_locked()` helper 复用给 cmdsock 的 repaste 路径
- 注入完成（成功或失败）都 append 到 history（失败的反而更需要重粘）

### 测试

123 个单元测试全部通过（v0.4.8 是 95 个）。新增：
- `tests/test_history.py`：19 个测试，覆盖 ring 容量 / 持久化原子性 / chmod 600 / 损坏行容忍 / 并发安全
- `tests/test_cmdsock.py`：9 个测试，端到端 socket round-trip / 错误处理 / chmod / 默认路径

### 兼容性

- 0.4.x 用户的 `~/.config/spitch/config.json` 不需要改动——`history.capacity` 缺失时取默认 50
- 老 daemon 没有 cmd socket，新装的 `spitch-cli` 会报 `daemon not running`；先重启 daemon 即可

---

## [0.4.8] — 2026-05-04

**真正修好了**长句中段被截断。0.4.6 用 `extract_full_text` 拼接当前帧的 `utterances[]` 数组，**还是不够**——豆包在 utterance 标 `definite=true` **之后的下一帧里把它从数组中完全移除**，只留正在生成的当前段。无论 `result.text` 还是 `utterances[]` 在那之后都看不到那段了。

### 真实抓到的现象（0.4.7 daemon log）

```
22:54:43,871 partial: …现在说话你能听见吗？好的，我现在想让你提几个关于
22:54:43,895 partial: …好的，我现在想让你提几个关于    ← "现在说话你能听见吗？" 整段消失！
```

间隔只有 24 ms 的两帧——前一帧 `utterances=[{现在说话你能听见吗？, def:true}, {好的..., def:false}]`，后一帧 `utterances=[{好的..., def:false}]`。server 把已 finalize 的段从数组里"打扫"掉了。

### 修复

`controller._consume()` 自己维护 `confirmed_finals: list[str]` 累积所有看到过的 `def=true` utterance（用"末尾去重"避免一个 utterance 在 N 帧里 def=true 时被重复 append）。每帧报告：

```python
"".join(confirmed_finals) + current_in_progress_utterance
```

server 之后从 wire 里移除掉已确认段，client 这边仍然完整保留。

### 测试

95 个单元测试全部通过。新增 `test_doubao_drops_finalized_utterances_across_frames` —— 用模拟豆包"下一帧丢弃 def=true utterance"的 fake client 跑完整 4 帧，断言最终 final 包含全部 3 段。

---

## [0.4.7] — 2026-05-04

修复 **WebSocket 冷连接 5 秒导致短句完全没反应**的问题。0.4.6 修了 server 给到的累积全文，但**冷启动时连不上 server**根本拿不到累积全文。

### 真实抓到的现象（0.4.6 daemon log）

```
22:42:08,269  press: session started
22:42:08,270  session: connecting to ASR endpoint
22:42:13,379  session: connected, starting stream    ← 5.1 秒后才连上
22:42:13,580  release  ← 用户已经松开了
22:42:18,582  WARNING: no final transcript within 5.0s
```

冷启动 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` 第一次连：DNS resolution + TCP handshake + TLS handshake + WS upgrade 全无 cache，**单次能花 5 秒+**。如果用户的 push-to-talk 时长比这个还短（说一句话通常 1-3 秒），按下时全在等连接，松开时连接刚好建立、audio 立刻 EOS，server 收到的几乎是空 audio + 立即关闭，于是什么 partial / final 都不返回。后续按下也时好时坏——connect < 1 秒能成功，> 2 秒就丢词。

### 修复

加 `_network_warmup_loop` 后台守护线程：

- **daemon 启动时立即跑一次** `DoubaoClient.__aenter__()` + `__aexit__()` —— 把 DNS、TCP、TLS session ticket、WS upgrade 全部预热进 OS / Python 层 cache
- **每 4 分钟重连一次**保持网络路径热乎；OS 在这个间隔内 DNS 不过期、TLS resumption ticket 仍然有效
- 实测预热后 connect 时间从 5 秒级降到 < 500 ms，即使非常短的 push-to-talk 也能稳定收到 partial/final
- 日志里能看到 `network warmup: 0.43s`，方便观察实际效果

### 测试

93 个单元测试全部通过；warmup 是 daemon 层逻辑，单元测试不需要联网就能跑。

---

## [0.4.6] — 2026-05-04

**真正修好了**长句中段被截断的问题。0.4.5 走的方向对（不要在第一段 final 时退 session）但实现错——以为 server 会在段切换点发出 `is_final=True` 让我们 append 到 `final_segments`。

### 真实抓到的现象（0.4.5 daemon log）

```
25,290 partial: …能改进用户体验的建议的话，你会怎么提？呃，按照就是用户体验提升价值大小来排序
25,332 partial: …呃，按照就是用户体验提升价值大小来排序    ← reset，前半段消失
…
final:   '呃，按照就是用户体验提升价值大小来排序，不要提太多。'
```

豆包在段切换点发的 payload 是这样：

```json
{"result": {
  "text": "呃，按照…",                    ← 只有当前段
  "utterances": [
    {"text": "…你会怎么提？", "definite": true},   ← 已 finalized
    {"text": "呃，按照…",     "definite": false}   ← 当前正在生成
  ]
}}
```

`is_final = all(definite=true)` → False（第二段还没 definite）→ 0.4.5 把这个当普通 partial 处理 → `current_text` 被覆盖成第二段 → 第一段从此找不回。

### 修复

引入新函数 `extract_full_text(payload)`：直接拼接 `utterances[].text`（包括所有 definite 的 + 当前 in-progress 的），不依赖那个误导性的 `is_final` boolean。

- `doubao.py:stream()` 改用 `extract_full_text` — 现在 `evt.text` 永远是从用户开始说到目前为止的**累积全文**
- `controller.py:_consume()` 也大幅简化——既然 `evt.text` 已经是完整全文，就不需要 segment 累积逻辑了，直接 `last_text = evt.text`、stream 关闭时 commit
- `extract_text()` 保留不变（向后兼容 + 单 utterance 快路径），但加了 docstring 警告它的 multi-utterance 缺陷

### 测试

93 个单元测试全部通过。新增 5 个 `ExtractFullTextTests`，包括用真实从 daemon.log 复制下来的 multi-utterance payload 验证累积正确。

---

## [0.4.5] — 2026-05-04

修复**长句中带停顿时只 inject 最后一段**的核心 bug。0.4.1 的修复方向（不在第一个 `definite=true` 退 session）是对的，但实现有缺陷——以为 server 给的 `result.text` 是累积全文，实际是**每段 utterance 独立**的。

### 真实抓到的现象（0.4.4 daemon log）

```
46,816 partial: …这个产品如果要做一些改进...你觉得有什么建议吗？尤其是那种对用户体验提升特别明显的
46,853 partial: …尤其是那种对用户体验提升特别明显的    ← 前面"这个产品..."消失了！
…
final:   '尤其是那种对用户体验提升特别明显的，建议你按顺序给我排出来。'
```

豆包在多 utterance 模式下，第一段 `definite=true` 之后**下一段开始时 `result.text` 重置**——只包含当前正在生成的 utterance，不再包含之前 finalize 过的内容。

### 修复

`controller.py:_consume()` 改为段累积：

- 收到 `is_final=True` → append 到 `final_segments` list、清空 `current_text`
- 收到普通 partial → 覆盖 `current_text`
- 始终把累积值 `"".join(final_segments) + current_text` 报给 `on_partial` / 写进 `latest_text` / 在 stream EOS 时作为 final commit

这样 tray label 跨段不会"跳回"，inject 出来的也是从用户开始说到最后的完整文本。

### 测试

88 个单元测试全部通过；现有的 `test_voice_controller` mock client 在第一个 `is_final` 后就 `__aiter__` 自然结束，覆盖的恰好是 single-segment 路径，新累积逻辑对它行为不变。

---

## [0.4.4] — 2026-05-04

诊断日志补丁——把 press / release / session-connect / stream-start 的关键节点加进 `daemon.log`，遇到"按下没反应、partial 不出来"这种安静失败能精确定位是 hotkey 没收到、controller 没启动、ws 没连上、还是 server 没回 partial。

### 新增日志

- `press: session started (state=...)` — 热键被接受、controller 进入 RECORDING
- `press: voice not idle (state=...)` — 已经有 session 在跑，新按下被拒绝
- `release: voice.state=..., scheduling inject` — 松键时控制器实际状态、inject 线程已派
- `release: ignored (no accepted press)` — 松键事件无对应 press（cancel / 启动期老 modifier）
- `session: starting client_factory` / `session: connecting to ASR endpoint` / `session: connected, starting stream` — 让 ws 连接卡住与"连上但 server 不回 partial"两种故障在日志里能区分

### 测试

88 个单元测试全部通过。

---

## [0.4.3] — 2026-05-04

补丁版本——0.4.2 改的 `restore_delay_ms` 默认值**没生效**。原因：值通过 `config.py:DEFAULT_CONFIG` 注入到运行时配置，daemon 用 `inject_cfg.get(key, 800)` 读它的时候 key 已经存在（值是 300），fallback 800 永远走不到。

### 修复

- **`config.py:DEFAULT_CONFIG.inject.restore_clipboard_delay_ms`**：300 → **800**
  - 这是真正生效的默认值。daemon.py 和 inject_text 里的 800 fallback 仅在 key 缺失时启用，但 `_deep_merge` 总是会把 `DEFAULT_CONFIG` 的字段合并进来，所以那两个 fallback 实际是死代码。
  - 现有用户的 `~/.config/spitch/config.json` 如果 `inject` section 是空的（默认情况）会自动获得 800；如果有人手动写过 300，需要删掉那行或改成 800。

### 测试

88 个单元测试全部通过。

---

## [0.4.2] — 2026-05-04

修复了**长文本粘贴只粘到一部分**的 race。0.4.1 修对了"server 给的 final 是完整全文"，但**剪贴板 → 粘贴**这一步本身存在两个时序漏洞，导致目标应用读到的不是我们写入的文本，而是旧剪贴板或者半截内容。

### 修复

- **wl-copy / xclip 异步注册剪贴板的 race**：这两个工具默认 fork 到后台，`subprocess.run` 在父进程退出时立即返回，但**子进程**（真正持有剪贴板的那个）注册 selection offer 给 compositor 是异步的。原来代码紧接着就合成 `Ctrl+Shift+V`，应用拿到 paste 信号请求 clipboard 时子进程还没 register，读到的是**旧的** selection（saved 内容或空）。新增 80 ms settle 让子进程 ready 再发按键。
- **Electron / Chromium 类应用异步读取 clipboard 被 restore 截胡**：Claude Code、VS Code、Slack、Feishu、Discord 等基于 Chromium 的应用在收到粘贴键后会 schedule 一个 `clipboard.readText()` Promise，长 CJK 文本下这个异步 read 可能要 300–700 ms 才完成。原默认 `restore_clipboard_delay_ms=300` 太激进——还原线程已经把剪贴板写回 saved，应用 Promise 才 resolve，于是应用读到的是 saved 不是我们的转写文本。默认改成 **800 ms**，覆盖目前测试过最慢的 Electron 应用。
- **inject 链路全程加详细日志**：`daemon.log` 里现在会写每一步：`inject: prep text len=… preview=…`、`_copy ok=…`、`keystroke ok=…`、`clipboard restored`、`inject: result ok=…`。下次再出诡异的"粘出来不全"问题可以一眼定位是 server / queue / clipboard / keystroke / restore 哪一步丢的。
- **`wait_quiescent` 超时时显式告警**：用户长时间不松开 modifier 时（>2 秒）日志会写 `synthesized paste will fight the held modifiers`，而不是默默继续合成会被 modifier 干扰的按键。

### 配置

- `inject.restore_clipboard_delay_ms` 默认值 300 → **800**。如果你只用同步剪贴板的"传统"应用（gedit、vim）想要更快还原，把它调到 200–300 也能 work。

### 测试

88 个单元测试全部通过；新增的 `time.sleep(0.08)` 不在单测路径上（单测打 stub 跳过 `subprocess`）。

---

## [0.4.1] — 2026-05-04

修复了**长句中段被截断**的关键 bug — 用户按住 Ctrl+Alt 说一段较长、中间有自然停顿的句子时，最后注入的文本只有第一段，停顿之后说的内容全部丢失。

### 修复

- **RECORDING 期间收到 `definite=true` 提前关 session 导致后半截丢失**：豆包对长句会按"语义停顿"分段，每段稳定就给 `definite=true`。原来 `controller.py:_consume()` 看到第一个 `is_final=True` 就立刻 `on_final` 并退出 session；`doubao.py:stream()` 也会在那帧之后 `return`，关掉 WebSocket。结果用户**还在按着**热键继续说的内容完全收不到。
  - `stream()` 移除 `is_final` 时的 `return`，session 由 audio EOS（用户松键发出 `NEG_WITH_SEQUENCE`）自然终止
  - `_consume()` 在 RECORDING 期间收到 `is_final` 改为缓存 `last_final_text` 并继续读，直到 stream 关闭再触发一次 `on_final(last_final_text)`
  - 同时把每段 `definite=true` 也透传给 `on_partial`，tray label 跨段不会"卡死"在第一段的尾巴

### 测试

88 个单元测试全部通过；fake streaming client 在发完 `is_final=True` 后通过 `__aiter__` 自然耗尽来模拟新的"audio EOS 才结束"语义，行为兼容。

---

## [0.4.0] — 2026-05-04

托盘 UX 重做：把"录音中 / 转写中"的反馈从 Ubuntu 顶部弹的桌面通知 popup 搬进右上角 tray label，并加了 About 对话框看版本号。配置文件层面**完全向后兼容**——没有新增配置字段。

### 新增

- **tray label 实时显示识别片段**：录音时状态栏 tray 图标旁边出现 `🎙 你好世界今天…`，server 推 partial 就刷新一下，最长截到末尾 15 个字。FINALIZING 期间显示 `✍ <最新文本>`，会话结束后保留 1.5 秒 `✓ <识别结果>` 让用户看到最终结果再淡出。出错显示 `⚠ 出错`
- **About Spitch 菜单项**：右键 tray → 第二个新菜单项，弹 `Gtk.AboutDialog`，显示 `spitch.__version__`、项目描述、网站、license、bundled 的 idle 图标作 logo。版本号唯一事实源就是 `__init__.py` 的 `__version__`

### 修复

- **录音/转写不再弹桌面通知 popup**：原本每次按下 / 松开都会从屏幕顶上弹两条系统通知，现在 tray label 直接承载状态反馈，安静且更易扫到
- **headless 模式行为不变**：缺 AppIndicator 包或 D-Bus 不可达时（自动 fallback 到 headless），原来的 notify-send 通知保留，用户至少能看到反馈

### 测试

88 个单元测试全部通过。新增 12 个覆盖 `compose_label` / `_tail` 纯函数：每个 State + 有/无 partial 的标签格式、末尾截断的 ellipsis 行为、超长时保留尾部而非头部。

---

## [0.3.0] — 2026-05-04

修复了**前半截语音被吃掉**这个长期问题，并把第二轮代码审查（含一次自审）发现的 P0/P1 全部修完。配置文件层面**完全向后兼容**——新增字段都有默认值，老配置文件加载后自动获得它们；CLI 命令不变。

### 新增

- **常驻麦克风 + 环形预缓冲**（修首字丢失）：daemon 启动后立刻打开麦克风并把 PCM 持续灌入 500 ms 的环形缓冲；按下 `Ctrl+Alt` 时先把这段缓冲推进会话队列，再继续推新数据。原本 PortAudio/ALSA 启动期 50–500 ms 的"麦克风没在录音但用户已经在说"那段话不再丢
- **`audio.prebuffer_ms`**：新配置项，默认 500。设为 0 退回旧的"按下才开麦"行为，对常驻麦克风有顾虑的用户可以这样关
- **`inject.restore_clipboard_delay_ms`**：粘贴后恢复用户原剪贴板的延迟，默认 300 ms。飞书/微信等慢 Electron 应用之前需要改源码里的 `time.sleep(0.3)`，现在改配置就行
- **`HotkeyListener.wait_quiescent()`**：事件驱动的"等所有修饰键松开"，替代原本 50 Hz 的 busy-poll；空闲 CPU 从 50 唤醒/秒降到 0
- **arecord 启动失败立即报错**：捕获 stderr，启动后 50 ms 内 arecord 已退出（设备占用、PCM 不存在、ALSA 配错）就直接抛 `AudioCaptureError` 弹通知，而不是傻等 5 秒"no final transcript"

### 修复

- **RECORDING 期间收到 `definite=true` 导致整段丢词**：豆包对短句、停顿够长的句子会在用户还没松键时就发 final，控制器随即转 IDLE，旧 daemon 的 `_on_release` 因为 `state != RECORDING` 直接 return，转写被丢。改用 `_press_accepted` 标记取代 state 比较
- **ERROR 路径上新旧 session 互相踩 mic**：错误后立刻重按 → 新 session 调 `audio.start()` 重开麦 → 旧 session `finally` 里的 `audio.stop()` 把**新会话**的 stream 关了，新录音瞬间断流。控制器把状态转换严格放到 `audio.stop()` **之后**
- **uninstall 杀不掉 daemon**：`pgrep -f "python3? -m spitch( |$|--)"` 在 BRE 下一个进程也匹配不到。改成字面量 `-m spitch`
- **UInput 创建后第一批事件被吞**：udev/libinput 加 seat 需要 10–50 ms，原代码立刻就发 EV_KEY，部分机器上首次粘贴看似"完全没反应"。`UInput()` 之后加 30 ms settle
- **install.sh 在 X11 上误报**：装了 xclip 仍提示"missing wl-clipboard"。改为接受 wl-copy / xclip / xsel 三选一

### 加固

- **arecord stderr 64 KB 管道阻塞风险**：早退检查通过后启 daemon 线程持续 drain stderr，避免长录音里 arecord 写 stderr 卡住反过来饿死 stdout
- **UInput 上的 settle sleep 资源安全**：移到 `try/finally` 之内，信号打断不会泄露虚拟键盘
- **拒绝单 modifier 热键**：`HotkeyListener.__init__` 和 `daemon.run()` 双重把关，配 `Ctrl` 之类只会让 daemon 起来后乱触发，直接拒绝并给修复提示
- **`_detect_backend` 一次 inject 只调一次**：原本 `_paste`、`_copy`、还原 `_copy` 各调一次（每次 `shutil.which` 都是 PATH 遍历），改为入口检测一次传下去

### 测试

75 个单元测试，13 个新增，全部通过。覆盖：

- 控制器 ERROR 路径 audio race（断言 `audio.stop()` 时刻状态仍是 RECORDING）
- daemon `_on_press / _on_release / _on_cancel / _on_final` 完整生命周期，包括 RECORDING-final 与 FINALIZING-window 两种时序
- HotkeyListener 构造校验、`parse_combo`、`wait_quiescent` 的 ~30 ms 唤醒延迟
- 预缓冲 replay、容量、跨会话独立、`close()` 清空、`start()` 与 callback 并发竞态

### 打包

- 0.3.0 sdist + wheel 上传到 GitHub release，`pip install spitch==0.3.0` 可以直接装

---

## [0.2.1] — 2026-05-04

第一轮代码审查后的修复 + 健壮性强化版。**用户层面没有破坏性变更**，配置文件和 CLI 命令完全兼容 0.2.0；建议所有用户升级。

### 新增

- **X11 剪贴板支持**：除了 `wl-copy`/`wl-paste`，现在自动检测并使用 `xclip` 或 `xsel`。Wayland 与 X11 用户无需任何配置切换
- **托盘图标包内化**：图标 SVG 移到 `src/spitch/tray/icons/`，`pip install` 安装后也能正常显示状态指示
- **可配置的 final 等待时长**：`config.json` 新增 `inject.final_wait_seconds`（默认 5 秒）
- **更具体的注入失败提示**：粘贴失败的桌面通知现在会写明真实原因（缺 wl-clipboard / `/dev/uinput` 不可写 / 键名错误等），而不是统一的"check uinput permissions"

### 修复

- **快速重按导致转写丢失**：FINALIZING 阶段又按一次热键时，新按下会替换 pending 队列引用，导致上一段的 final 转写写到没人读的队列里、整段丢失。改为只有 `voice.press()` 真正成功后才换队列
- **并发 inject 线程争抢剪贴板和 `/dev/uinput`**：连续两次快速按下时两个 inject 线程会同时操作剪贴板与合成键盘事件，导致粘贴的文本被对方覆盖或错位。新增 daemon 级串行锁
- **音频采集泄漏**：当 server 在 RECORDING 阶段就发出 `definite=true`（罕见），session 结束时麦克风不会被关闭，下次按下前一直占着采集设备。`_run_session` 的 finally 现在无条件 `audio.stop()`
- **`controller.press()` 异常路径过窄**：只 catch 了 `AudioCaptureError`，其他异常（设备消失、线程派生失败等）会让状态机卡死在 RECORDING。改为 catch `Exception`，并对 worker 线程派生失败也加保护
- **托盘指示器构造失败时 daemon 崩溃**：D-Bus session bus 不可达时 `AppIndicator3.Indicator.new` 会抛非 `ValueError/ImportError`。`try_create` 现在用 `except Exception` 兜底，自动退到 headless
- **X11 上误用 wl-copy**：装了 wl-clipboard 但运行在 X11 时，原 fallback 会调用 wl-copy 然后 hang。剪贴板后端选择改为严格按 session type 走
- **GTK 配置对话 Test 按钮卡死**：worker 线程未捕异常时整个 UI 永远 disable。worker 顶层加了 try/except
- **UInput 写入异常无提示**：`ui.write` / `ui.syn` 抛 OSError 时异常冒泡到 daemon 线程顶层，用户看不到通知。catch 后转为正常的 `(False, reason)` 返回
- **剪贴板还原失败**：粘贴中途失败时不会还原原剪贴板。改为 `try/finally` 保证还原
- **`wl-paste --no-newline` 丢尾换行**：保存→还原过程不再丢失原剪贴板的尾部换行
- **GTK 模式下 Gtk 变量作用域**：显式预声明 `Gtk = GLib = None`，不再依赖隐式控制流
- **AppIndicator typelib 缺失但 GTK 可用**：之前会卡在隐藏的 GTK 主循环里，只能 SIGTERM 退出。现在自动退到带信号处理的 headless 模式

### 安全

- **日志路径 symlink race**：launcher 不再把日志写到固定的 `/tmp/spitch.log`（多用户共享目录有 symlink race 风险）。改写到 `${XDG_STATE_HOME:-~/.local/state}/spitch/daemon.log`，目录权限 700

### 打包

- `sounddevice`、`numpy` 移到可选依赖 `audio-sd`：默认安装不再强拉 PortAudio + numpy；缺失时自动 fallback 到 `arecord`（alsa-utils 在 Ubuntu 上预装）
- 托盘图标加入 `package-data`，`pip install` 后路径正确

### 测试

51 个单元测试全部通过。新增覆盖：剪贴板后端选择、并发 inject 串行化、controller 异常路径。

---

## [0.2.0] — 2026-05-03

首个语音通路 release。

### 新增

- 全局热键监听（evdev / `/dev/input/event*`）
- 豆包 BigModel 实时 ASR 客户端 + 二进制帧编解码
- AudioCapture：sounddevice 优先、arecord 兜底
- VoiceController 推到说状态机（IDLE / RECORDING / FINALIZING / ERROR）
- 剪贴板 + 合成 `Ctrl+Shift+V` 的注入路径（仅 Wayland）
- GTK 配置对话框 + CLI fallback
- libayatana-appindicator 系统托盘
- 配置原子写 + chmod 600 + 凭据指纹 verified gating

[0.4.0]: https://github.com/pekinlcc/Spitch/releases/tag/v0.4.0
[0.3.0]: https://github.com/pekinlcc/Spitch/releases/tag/v0.3.0
[0.2.1]: https://github.com/pekinlcc/Spitch/releases/tag/v0.2.1
[0.2.0]: https://github.com/pekinlcc/Spitch/releases/tag/v0.2.0
