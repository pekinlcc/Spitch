"""Spitch 控制台 — three-tab GTK window for managing the running daemon.

Tabs:
  * 历史 — recent transcripts; per-row [复制] [重粘] [删除] buttons.
  * 日志 — tail of ``daemon.log`` with auto-scroll, [清空] [打开文件].
  * 设置 — common config knobs as widgets, [应用 + 重启 daemon].

All daemon RPC goes through :mod:`spitch.cmdsock` (history list /
repaste / clear). Config save uses :mod:`spitch.config` directly and
asks the user to restart the daemon afterwards (we don't yet support
hot-reload of voice controller).

Falls back gracefully when GTK / PyGObject is missing — emits a
short hint to stderr and exits 1, suggesting the CLI alternatives
(``spitch-cli list`` / ``spitch-cli repaste``).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from .. import __version__
from ..cmdsock import call as cmd_call


def _state_log_path() -> Path:
    """Mirror of the launcher script's log location."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "spitch" / "daemon.log"


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - GUI
    try:
        import gi  # type: ignore
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk, GLib, Pango  # type: ignore
    except (ValueError, ImportError) as exc:
        sys.stderr.write(
            f"spitch-console: GTK unavailable ({exc}). The console UI "
            "needs python3-gi. Use the command-line equivalents:\n"
            "  spitch-cli list           # see history\n"
            "  spitch-cli repaste        # re-paste latest\n"
        )
        return 1

    win = Gtk.Window(title=f"Spitch — 控制台  v{__version__}")
    win.set_default_size(720, 480)
    win.set_border_width(8)

    notebook = Gtk.Notebook()
    win.add(notebook)

    # ---- History tab ------------------------------------------------
    notebook.append_page(_build_history_tab(Gtk, GLib, Pango), Gtk.Label(label="历史"))

    # ---- Log tab ----------------------------------------------------
    notebook.append_page(_build_log_tab(Gtk, GLib, Pango), Gtk.Label(label="日志"))

    # ---- Settings tab -----------------------------------------------
    notebook.append_page(_build_settings_tab(Gtk, GLib), Gtk.Label(label="设置"))

    win.connect("destroy", lambda _w: Gtk.main_quit())
    win.show_all()
    Gtk.main()
    return 0


# ---------------------------------------------------------------------------
# History tab
# ---------------------------------------------------------------------------


def _build_history_tab(Gtk, GLib, Pango):  # pragma: no cover
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

    # ListStore: [index, time_str, ok_flag, app, text]
    store = Gtk.ListStore(int, str, str, str, str)
    view = Gtk.TreeView(model=store)
    view.set_headers_visible(True)

    def add_col(title, col_id, expand=False, ellipsize=False):
        renderer = Gtk.CellRendererText()
        if ellipsize:
            renderer.set_property("ellipsize", Pango.EllipsizeMode.END)
        col = Gtk.TreeViewColumn(title, renderer, text=col_id)
        col.set_resizable(True)
        col.set_expand(expand)
        view.append_column(col)
        return col

    add_col("时间", 1)
    add_col("✓", 2)
    add_col("应用", 3)
    add_col("内容", 4, expand=True, ellipsize=True)

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.add(view)
    scroller.set_vexpand(True)
    box.pack_start(scroller, True, True, 0)

    # Action row
    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    btn_refresh = Gtk.Button(label="刷新")
    btn_repaste = Gtk.Button(label="重粘")
    btn_copy = Gtk.Button(label="复制")
    btn_delete = Gtk.Button(label="删除")
    btn_clear = Gtk.Button(label="清空全部")
    actions.pack_start(btn_refresh, False, False, 0)
    actions.pack_start(btn_repaste, False, False, 0)
    actions.pack_start(btn_copy, False, False, 0)
    actions.pack_start(btn_delete, False, False, 0)
    actions.pack_end(btn_clear, False, False, 0)
    box.pack_start(actions, False, False, 0)

    status = Gtk.Label(label="", xalign=0.0)
    box.pack_start(status, False, False, 0)

    def set_status(msg: str, ok: bool = True):
        prefix = "✓ " if ok else "⚠ "
        status.set_text(prefix + msg)

    def refresh(*_):
        store.clear()
        try:
            resp = cmd_call("list")
        except ConnectionError as exc:
            set_status(f"连不上 daemon: {exc}", ok=False)
            return
        if not resp.get("ok"):
            set_status(resp.get("error", "list failed"), ok=False)
            return
        entries = resp.get("entries") or []
        for i, e in enumerate(entries):
            store.append([
                i,
                time.strftime("%H:%M:%S", time.localtime(e.get("timestamp", 0))),
                "✓" if e.get("inject_ok", True) else "✗",
                e.get("target_app") or "-",
                e.get("text", ""),
            ])
        set_status(f"共 {len(entries)} 条")

    def selected_index() -> int | None:
        sel = view.get_selection()
        model, it = sel.get_selected()
        if it is None:
            return None
        return int(model[it][0])

    def on_repaste(_b):
        idx = selected_index()
        if idx is None:
            set_status("请先选一条记录", ok=False)
            return
        try:
            resp = cmd_call("repaste", index=idx)
        except ConnectionError as exc:
            set_status(f"连不上 daemon: {exc}", ok=False)
            return
        if resp.get("ok"):
            preview = resp.get("text_preview", "")
            set_status(f"已触发重粘：{preview}")
        else:
            set_status(resp.get("error", "repaste 失败"), ok=False)

    def on_copy(_b):
        idx = selected_index()
        if idx is None:
            set_status("请先选一条记录", ok=False)
            return
        try:
            resp = cmd_call("list")
        except ConnectionError as exc:
            set_status(f"连不上 daemon: {exc}", ok=False)
            return
        entries = resp.get("entries") or []
        try:
            text = entries[idx]["text"]
        except (IndexError, KeyError):
            set_status("找不到这条记录", ok=False)
            return
        clip = Gtk.Clipboard.get(_x_selection_clipboard(Gtk))
        clip.set_text(text, -1)
        clip.store()
        set_status(f"已复制 {len(text)} 个字符到剪贴板")

    def on_delete(_b):
        idx = selected_index()
        if idx is None:
            set_status("请先选一条记录", ok=False)
            return
        try:
            resp = cmd_call("delete", index=idx)
        except ConnectionError as exc:
            set_status(f"连不上 daemon: {exc}", ok=False)
            return
        if resp.get("ok"):
            refresh()
            set_status(f"已删除条目 {idx}")
        else:
            set_status(resp.get("error", "delete 失败"), ok=False)

    def on_clear(_b):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="清空所有历史记录？",
        )
        dialog.format_secondary_text("这个操作不可撤销。")
        resp = dialog.run()
        dialog.destroy()
        if resp != Gtk.ResponseType.OK:
            return
        try:
            r = cmd_call("clear")
        except ConnectionError as exc:
            set_status(f"连不上 daemon: {exc}", ok=False)
            return
        if r.get("ok"):
            refresh()
            set_status("历史已清空")
        else:
            set_status(r.get("error", "clear 失败"), ok=False)

    btn_refresh.connect("clicked", refresh)
    btn_repaste.connect("clicked", on_repaste)
    btn_copy.connect("clicked", on_copy)
    btn_delete.connect("clicked", on_delete)
    btn_clear.connect("clicked", on_clear)
    view.connect("row-activated", lambda *_: on_repaste(None))

    refresh()
    return box


def _x_selection_clipboard(Gtk):  # pragma: no cover
    """Get the standard system clipboard atom; differs slightly between
    GTK versions."""
    from gi.repository import Gdk
    return Gdk.SELECTION_CLIPBOARD


# ---------------------------------------------------------------------------
# Log tab
# ---------------------------------------------------------------------------


def _build_log_tab(Gtk, GLib, Pango):  # pragma: no cover
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

    buf = Gtk.TextBuffer()
    view = Gtk.TextView(buffer=buf)
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_monospace(True)
    view.modify_font(Pango.FontDescription("Monospace 9"))

    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.add(view)
    scroller.set_vexpand(True)
    box.pack_start(scroller, True, True, 0)

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    btn_clear = Gtk.Button(label="清空显示")
    btn_open = Gtk.Button(label="打开日志文件")
    auto_scroll = Gtk.CheckButton(label="自动滚动")
    auto_scroll.set_active(True)
    actions.pack_start(btn_clear, False, False, 0)
    actions.pack_start(btn_open, False, False, 0)
    actions.pack_end(auto_scroll, False, False, 0)
    box.pack_start(actions, False, False, 0)

    log_path = _state_log_path()

    state = {"fh": None, "pos": 0}

    def open_log():
        try:
            fh = open(log_path, "r", encoding="utf-8", errors="replace")
            fh.seek(0, os.SEEK_END)
            tail = max(0, fh.tell() - 8192)  # last ~8KB
            fh.seek(tail)
            initial = fh.read()
            buf.insert(buf.get_end_iter(), initial)
            state["fh"] = fh
            state["pos"] = fh.tell()
        except OSError as exc:
            buf.insert(buf.get_end_iter(),
                       f"(无法打开日志：{exc})\n请确认 daemon 跑过至少一次。")

    def poll_log():
        fh = state["fh"]
        if fh is None:
            return True
        try:
            fh.seek(state["pos"])
            chunk = fh.read()
            if chunk:
                buf.insert(buf.get_end_iter(), chunk)
                state["pos"] = fh.tell()
                if auto_scroll.get_active():
                    end = buf.get_end_iter()
                    view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        except OSError:
            pass
        return True

    def on_clear(_b):
        buf.set_text("")

    def on_open(_b):
        import subprocess
        try:
            subprocess.Popen(["xdg-open", str(log_path)])
        except OSError:
            pass

    btn_clear.connect("clicked", on_clear)
    btn_open.connect("clicked", on_open)

    open_log()
    GLib.timeout_add(500, poll_log)
    return box


# ---------------------------------------------------------------------------
# Settings tab
# ---------------------------------------------------------------------------


def _build_settings_tab(Gtk, GLib):  # pragma: no cover
    from ..config import (
        config_path, default_config, is_complete, load_config, save_config,
    )
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    box.set_border_width(8)

    cfg = load_config()
    h = dict(cfg.get("hotkey") or {})
    a = dict(cfg.get("audio") or {})
    inj = dict(cfg.get("inject") or {})

    grid = Gtk.Grid(column_spacing=8, row_spacing=8)
    box.pack_start(grid, False, False, 0)

    def add_row(row: int, label: str, widget):
        grid.attach(Gtk.Label(label=label, xalign=0.0), 0, row, 1, 1)
        grid.attach(widget, 1, row, 2, 1)

    e_talk = Gtk.Entry()
    e_talk.set_text(h.get("talk_key", "Ctrl+Alt"))
    e_talk.set_hexpand(True)
    add_row(0, "热键 (talk_key)", e_talk)

    e_paste = Gtk.Entry()
    e_paste.set_text(inj.get("paste_keystroke", "Ctrl+Shift+V"))
    e_paste.set_hexpand(True)
    add_row(1, "粘贴键 (paste_keystroke)", e_paste)

    e_restore = Gtk.SpinButton.new_with_range(0, 5000, 50)
    try:
        e_restore.set_value(int(inj.get("restore_clipboard_delay_ms", 800)))
    except (TypeError, ValueError):
        e_restore.set_value(800)
    add_row(2, "粘贴后还原剪贴板延迟 (ms)", e_restore)

    e_prebuf = Gtk.SpinButton.new_with_range(0, 3000, 100)
    try:
        e_prebuf.set_value(int(a.get("prebuffer_ms", 500)))
    except (TypeError, ValueError):
        e_prebuf.set_value(500)
    add_row(3, "预缓冲 (ms)", e_prebuf)

    e_final_wait = Gtk.SpinButton.new_with_range(1, 30, 1)
    try:
        e_final_wait.set_value(float(inj.get("final_wait_seconds", 5)))
    except (TypeError, ValueError):
        e_final_wait.set_value(5)
    add_row(4, "等 final 最长秒数", e_final_wait)

    info = Gtk.Label(
        label=(
            f"配置文件：{config_path()}\n"
            f"凭据：{'✓ 已配置 + 已验证' if is_complete(cfg) else '✗ 未配置（先跑 spitch-config）'}\n"
            f"修改后点'保存并提示重启' — daemon 不会自动 reload，"
            f"得手动 kill 然后启动 spitch-daemon。"
        ),
        xalign=0.0,
    )
    info.set_line_wrap(True)
    box.pack_start(info, False, False, 0)

    actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
    btn_save = Gtk.Button(label="保存")
    btn_open_config = Gtk.Button(label="打开 spitch-config（凭据 / 测试连接）")
    actions.pack_start(btn_save, False, False, 0)
    actions.pack_end(btn_open_config, False, False, 0)
    box.pack_start(actions, False, False, 0)

    status = Gtk.Label(label="", xalign=0.0)
    box.pack_start(status, False, False, 0)

    def on_save(_b):
        new_cfg = default_config()
        new_cfg.update(cfg)  # preserve doubao + verified_at
        new_cfg["hotkey"] = dict(new_cfg.get("hotkey") or {})
        new_cfg["hotkey"]["talk_key"] = e_talk.get_text().strip() or "Ctrl+Alt"
        new_cfg["inject"] = dict(new_cfg.get("inject") or {})
        new_cfg["inject"]["paste_keystroke"] = e_paste.get_text().strip() or "Ctrl+Shift+V"
        new_cfg["inject"]["restore_clipboard_delay_ms"] = int(e_restore.get_value())
        new_cfg["inject"]["final_wait_seconds"] = float(e_final_wait.get_value())
        new_cfg["audio"] = dict(new_cfg.get("audio") or {})
        new_cfg["audio"]["prebuffer_ms"] = int(e_prebuf.get_value())
        try:
            path = save_config(new_cfg)
            status.set_text(f"已保存 → {path}\n手动重启 daemon 后生效：pkill -f 'python3? -m spitch' && spitch-daemon &")
        except Exception as exc:
            status.set_text(f"保存失败：{exc}")

    def on_open_config(_b):
        import shutil, subprocess
        if shutil.which("spitch-config"):
            subprocess.Popen(["spitch-config"])
        else:
            status.set_text("spitch-config 不在 PATH 上，参考 docs/INSTALL.md")

    btn_save.connect("clicked", on_save)
    btn_open_config.connect("clicked", on_open_config)
    return box


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
