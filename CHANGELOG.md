# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

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

[0.2.1]: https://github.com/pekinlcc/Spitch/releases/tag/v0.2.1
[0.2.0]: https://github.com/pekinlcc/Spitch/releases/tag/v0.2.0
