"""Spitch configuration dialog.

Two layers of operation:

1. **GTK UI** (default) — when PyGObject + GTK 3 are available, we
   pop a small dialog with the four required Doubao fields, audio
   sample rate, and the two hotkeys. A "Test connection" button runs
   :func:`spitch.ui.probe.probe_credentials` and surfaces success or
   the error from Doubao.

2. **Headless CLI** (``--cli``, or automatic when GTK is missing) —
   reads the same fields from stdin and runs the same probe. This
   lets us drive the auth flow from ``e2e_smoke.sh`` and from CI.

Either way, on success we ``mark_verified`` the saved config so the
engine's ``do_focus_in`` knows it's allowed to enable voice.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any

from ..config import (
    clear_verified,
    config_path,
    credentials_signature,
    default_config,
    is_complete,
    load_config,
    mark_verified,
    save_config,
)
from ..voice.doubao import DoubaoCredentials
from .probe import probe_credentials


def _prompt(label: str, default: str = "", *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    sys.stderr.write(f"{label}{suffix}: ")
    sys.stderr.flush()
    if secret:
        try:
            val = getpass.getpass("")
        except (EOFError, KeyboardInterrupt):
            return default
    else:
        try:
            val = sys.stdin.readline().rstrip("\n")
        except (EOFError, KeyboardInterrupt):
            return default
    return val.strip() or default


def run_cli(probe: bool = True) -> int:
    cfg = load_config()
    prior_signature = credentials_signature(cfg)
    d = dict(cfg.get("doubao") or {})
    sys.stderr.write(
        "Spitch — configure Doubao realtime ASR\n"
        "Press <Enter> to keep the value in [brackets].\n\n"
    )
    d["app_key"] = _prompt("X-Api-App-Key", d.get("app_key", ""))
    d["access_key"] = _prompt(
        "X-Api-Access-Key", d.get("access_key", ""), secret=True
    )
    d["resource_id"] = _prompt(
        "Resource ID", d.get("resource_id", "volc.bigasr.sauc.duration")
    )
    d["endpoint"] = _prompt(
        "WS endpoint",
        d.get("endpoint", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"),
    )
    cfg["doubao"] = d
    cfg["provider"] = "doubao"

    if credentials_signature(cfg) != prior_signature:
        cfg = clear_verified(cfg)

    if not is_complete(cfg):
        sys.stderr.write("\nIncomplete config — keys are required. Aborting.\n")
        return 1

    if probe:
        sys.stderr.write("\nProbing Doubao endpoint…\n")
        ok, msg = probe_credentials(
            DoubaoCredentials(
                app_key=d["app_key"],
                access_key=d["access_key"],
                resource_id=d["resource_id"],
                endpoint=d["endpoint"],
            )
        )
        sys.stderr.write(f"  → {msg}\n")
        if not ok:
            cfg = clear_verified(cfg)
            sys.stderr.write(
                "Saving config without verification — voice mode will stay "
                "disabled until a probe succeeds.\n"
            )
            save_config(cfg)
            return 2
        cfg = mark_verified(cfg)
    else:
        cfg = clear_verified(cfg)
        sys.stderr.write(
            "Probe skipped (--no-probe); voice mode stays disabled until "
            "you re-run spitch-config and the probe succeeds.\n"
        )

    path = save_config(cfg)
    sys.stderr.write(f"\nSaved {path}\n")
    return 0


def run_gtk() -> int:  # pragma: no cover - GUI is exercised manually
    import gi  # type: ignore
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib  # type: ignore

    cfg = load_config()
    d = dict(cfg.get("doubao") or {})
    h = dict(cfg.get("hotkey") or {})
    a = dict(cfg.get("audio") or {})

    win = Gtk.Window(title="Spitch — Configure Doubao")
    win.set_default_size(540, 380)
    win.set_border_width(16)

    grid = Gtk.Grid(column_spacing=8, row_spacing=10)
    win.add(grid)

    def add_row(row: int, label_text: str, entry_text: str = "", *, is_password: bool = False):
        label = Gtk.Label(label=label_text, xalign=0.0)
        entry = Gtk.Entry()
        entry.set_text(entry_text)
        if is_password:
            entry.set_visibility(False)
        entry.set_hexpand(True)
        grid.attach(label, 0, row, 1, 1)
        grid.attach(entry, 1, row, 2, 1)
        return entry

    e_app = add_row(0, "X-Api-App-Key", d.get("app_key", ""))
    e_access = add_row(1, "X-Api-Access-Key", d.get("access_key", ""), is_password=True)
    e_resource = add_row(2, "Resource ID", d.get("resource_id", "volc.bigasr.sauc.duration"))
    e_endpoint = add_row(3, "WS endpoint", d.get(
        "endpoint", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
    ))
    e_rate = add_row(4, "Audio sample rate", str(a.get("sample_rate", 16000)))
    e_talk = add_row(5, "Push-to-talk key", h.get("talk_key", "Ctrl+Alt"))

    status = Gtk.Label(label="", xalign=0.0)
    status.set_line_wrap(True)
    grid.attach(status, 0, 6, 3, 1)

    btn_test = Gtk.Button(label="Test connection")
    btn_save = Gtk.Button(label="Save")
    btn_close = Gtk.Button(label="Close")
    grid.attach(btn_test, 0, 7, 1, 1)
    grid.attach(btn_save, 1, 7, 1, 1)
    grid.attach(btn_close, 2, 7, 1, 1)

    def collect() -> dict[str, Any]:
        new_cfg = default_config()
        new_cfg.update(cfg)
        new_cfg["provider"] = "doubao"
        new_cfg["doubao"] = {
            "app_key": e_app.get_text().strip(),
            "access_key": e_access.get_text().strip(),
            "resource_id": e_resource.get_text().strip(),
            "endpoint": e_endpoint.get_text().strip(),
        }
        try:
            new_cfg["audio"] = dict(new_cfg.get("audio") or {})
            new_cfg["audio"]["sample_rate"] = int(e_rate.get_text().strip() or "16000")
        except ValueError:
            new_cfg["audio"]["sample_rate"] = 16000
        new_cfg["hotkey"] = dict(new_cfg.get("hotkey") or {})
        new_cfg["hotkey"]["talk_key"] = e_talk.get_text().strip() or "Ctrl+Alt"
        return new_cfg

    def set_status(msg: str, ok: bool | None = None) -> None:
        ctx = status.get_style_context()
        ctx.remove_class("success")
        ctx.remove_class("error")
        if ok is True:
            ctx.add_class("success")
            status.set_markup(f"<span foreground='#3b8632'>{GLib.markup_escape_text(msg)}</span>")
        elif ok is False:
            ctx.add_class("error")
            status.set_markup(f"<span foreground='#b00020'>{GLib.markup_escape_text(msg)}</span>")
        else:
            status.set_text(msg)

    # last_probe_ok["sig"] records the credentials signature that the
    # most recent successful probe verified. If the user edits the
    # entries after a successful probe, the saved config's signature
    # will not match and we treat the cached probe as stale.
    last_probe_ok = {"ok": False, "sig": None}

    def on_test(_btn):
        new_cfg = collect()
        if not is_complete(new_cfg):
            set_status("Fill in app_key + access_key + endpoint first.", ok=False)
            return
        set_status("Probing Doubao…")
        win.set_sensitive(False)
        sig = credentials_signature(new_cfg)

        def worker():
            try:
                d2 = new_cfg["doubao"]
                ok, msg = probe_credentials(DoubaoCredentials(
                    app_key=d2["app_key"],
                    access_key=d2["access_key"],
                    resource_id=d2["resource_id"],
                    endpoint=d2["endpoint"],
                ))
            except Exception as exc:
                # If probe_credentials itself blows up (e.g., asyncio
                # internals), don't let the thread die silently — the UI
                # was set insensitive by on_test and we'd leave it stuck.
                ok, msg = False, f"Probe crashed: {exc!r}"

            def done():
                last_probe_ok["ok"] = ok
                last_probe_ok["sig"] = sig if ok else None
                set_status(msg, ok=ok)
                win.set_sensitive(True)
                return False

            GLib.idle_add(done)

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def on_save(_btn):
        new_cfg = collect()
        if not is_complete(new_cfg):
            set_status("Cannot save: app_key, access_key, and endpoint are required.", ok=False)
            return
        sig = credentials_signature(new_cfg)
        verified_now = (
            last_probe_ok["ok"]
            and last_probe_ok["sig"] == sig
        )
        if verified_now:
            new_cfg = mark_verified(new_cfg)
        else:
            new_cfg = clear_verified(new_cfg)
        path = save_config(new_cfg)
        if verified_now:
            set_status(
                f"Saved → {path}\nVerified — voice mode is enabled.",
                ok=True,
            )
        else:
            note = (
                "Saved → {p}\nVoice mode stays disabled until ‘Test connection’ "
                "succeeds with the current values."
            ).format(p=path)
            set_status(note, ok=False)

    def on_close(_btn):
        Gtk.main_quit()

    btn_test.connect("clicked", on_test)
    btn_save.connect("clicked", on_save)
    btn_close.connect("clicked", on_close)
    win.connect("destroy", lambda _w: Gtk.main_quit())

    win.show_all()
    Gtk.main()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spitch-config", description="Configure Spitch")
    parser.add_argument("--cli", action="store_true", help="force CLI mode (no GTK)")
    parser.add_argument("--no-probe", action="store_true", help="skip Doubao probe")
    parser.add_argument("--print-path", action="store_true", help="print config path and exit")
    args = parser.parse_args(argv)
    if args.print_path:
        print(config_path())
        return 0
    if args.cli:
        return run_cli(probe=not args.no_probe)
    try:
        import gi  # noqa: F401
        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk  # type: ignore  # noqa: F401
    except Exception:
        sys.stderr.write("(GTK unavailable — falling back to CLI mode)\n")
        return run_cli(probe=not args.no_probe)
    return run_gtk()


if __name__ == "__main__":
    sys.exit(main())
