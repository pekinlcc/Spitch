# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 风格，版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

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
