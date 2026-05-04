"""System-tray status indicator backed by libayatana-appindicator.

The indicator surfaces the current daemon state in the GNOME / KDE
top-right status area:

* idle      — yellow microphone   (waiting for the hotkey)
* recording — red microphone, label shows the live partial transcript
* finalizing— blue microphone, label shows the latest text
* idle (post-final) — for ~1.5 s the label keeps "✓ <recognized>" so
                       the user gets to see the final recognition
                       before the tray goes dark again

A right-click on the indicator opens a small menu: status (read-only),
Configure…, About, Quit. The About item opens a Gtk.AboutDialog that
displays the package version (taken from ``spitch.__version__``) so
the user can see what's actually running.

Optional — ``try_create()`` returns ``None`` if the AppIndicator
typelib (``gir1.2-ayatanaappindicator3-0.1`` on Ubuntu) is not
available, and the daemon then runs without a tray icon.

All public methods are safe to call from any thread; the indicator
internally marshals to the GTK main thread via ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from ..voice import State

log = logging.getLogger("spitch.tray")


def _icon_dir() -> Path:
    """Locate the directory containing the state-icon SVGs.

    Prefers the package-bundled location (``src/spitch/tray/icons/`` →
    survives a pip install into site-packages) and falls back to the
    repo-relative ``data/icons/`` for running from a source checkout
    that hasn't bundled the icons yet.
    """
    here = Path(__file__).resolve()
    pkg_local = here.parent / "icons"
    if pkg_local.is_dir():
        return pkg_local
    return here.parents[3] / "data" / "icons"


_ICON_FOR_STATE = {
    State.IDLE:       "spitch-idle.svg",
    State.RECORDING:  "spitch-recording.svg",
    State.FINALIZING: "spitch-finalizing.svg",
    State.ERROR:      "spitch-idle.svg",
}


# Tray label visible-character budget. CJK glyphs are wide in the
# panel font, but Ubuntu's AppIndicator extension and KDE's tray both
# show ~30 narrow characters before truncation. 15 CJK glyphs (≈ 30
# narrow cells) keeps the label readable on every desktop we target.
_LABEL_TAIL_CHARS = 15

# How long after returning to IDLE we keep showing the last recognized
# text in the tray label, so the user gets to see the final result
# before the tray clears. After this expires the label goes empty.
_POST_FINAL_LINGER_MS = 1500


def _tail(text: str, n: int) -> str:
    """Return the last ``n`` characters of ``text`` with a leading
    ellipsis when the string was truncated."""
    if len(text) <= n:
        return text
    return "…" + text[-(n - 1):]


def compose_label(state: State, partial: str, max_chars: int = _LABEL_TAIL_CHARS) -> str:
    """Build the tray label string for a given state + partial text.

    Pure function — extracted from the indicator class so it can be
    unit-tested without GTK/AppIndicator.
    """
    if state == State.IDLE:
        if partial:
            return f"✓ {_tail(partial, max_chars)}"
        return ""
    if state == State.RECORDING:
        if partial:
            return f"🎙 {_tail(partial, max_chars)}"
        return "🎙 听写中…"
    if state == State.FINALIZING:
        if partial:
            return f"✍ {_tail(partial, max_chars)}"
        return "✍ 转写中…"
    if state == State.ERROR:
        return "⚠ 出错"
    return ""


def try_create(*, on_quit=None) -> Optional["SpitchIndicator"]:
    """Build a SpitchIndicator if AppIndicator + GTK are available, else None.

    Returns None on any failure — typelib missing, D-Bus session bus
    unreachable, AppIndicator3.Indicator.new failing, etc. Callers
    should fall back to a headless run loop.
    """
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator3
        except (ValueError, ImportError):
            gi.require_version("AppIndicator3", "0.1")
            from gi.repository import AppIndicator3
        from gi.repository import Gtk, GLib  # noqa: F401
    except (ValueError, ImportError) as exc:
        log.info("tray indicator unavailable (%s)", exc)
        return None
    try:
        return SpitchIndicator(AppIndicator3, on_quit=on_quit)
    except Exception as exc:
        # AppIndicator3.Indicator.new can raise generic GLib errors when
        # the session D-Bus is missing (e.g., daemon launched outside a
        # graphical session). Don't let it tank the whole daemon.
        log.warning("tray indicator init failed (%s) — running headless", exc)
        return None


class SpitchIndicator:
    """AppIndicator wrapper with a live label + state icon + small menu."""

    def __init__(self, AppIndicator3, *, on_quit=None):
        from gi.repository import Gtk, GLib
        self._Gtk = Gtk
        self._GLib = GLib
        self._on_quit = on_quit
        idle_path = str(_icon_dir() / _ICON_FOR_STATE[State.IDLE])
        self._ind = AppIndicator3.Indicator.new(
            "spitch",
            idle_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self._ind.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self._ind.set_title("Spitch")
        self._ind.set_label("", "")
        self._ind.set_menu(self._build_menu())
        self._state = State.IDLE
        self._partial = ""
        # GLib timeout source id for the post-final linger; we cancel
        # and reschedule it as state changes to avoid the linger
        # landing on top of a brand-new RECORDING.
        self._linger_id: int = 0
        self._set_icon_for(State.IDLE)

    # -- menu ----------------------------------------------------------

    def _build_menu(self):
        Gtk = self._Gtk
        menu = Gtk.Menu()
        item_status = Gtk.MenuItem(label="Spitch — idle")
        item_status.set_sensitive(False)
        menu.append(item_status)
        menu.append(Gtk.SeparatorMenuItem())
        item_config = Gtk.MenuItem(label="Configure…")
        item_config.connect("activate", self._on_open_config)
        menu.append(item_config)
        item_about = Gtk.MenuItem(label="About Spitch")
        item_about.connect("activate", self._on_about)
        menu.append(item_about)
        menu.append(Gtk.SeparatorMenuItem())
        item_quit = Gtk.MenuItem(label="Quit")
        item_quit.connect("activate", self._on_quit_clicked)
        menu.append(item_quit)
        menu.show_all()
        self._item_status = item_status
        return menu

    def _on_open_config(self, _widget):
        import shutil, subprocess
        if shutil.which("spitch-config"):
            subprocess.Popen(["spitch-config"])

    def _on_about(self, _widget):
        Gtk = self._Gtk
        # Local import — keep module-load light and avoid a circular
        # ..__init__ → ..voice → ..tray import chain.
        from .. import __version__
        dlg = Gtk.AboutDialog()
        dlg.set_program_name("Spitch")
        dlg.set_version(__version__)
        dlg.set_comments(
            "Linux 桌面下的全局热键中文语音输入工具，"
            "由豆包（火山引擎）实时 ASR 驱动。"
        )
        dlg.set_website("https://github.com/pekinlcc/Spitch")
        dlg.set_website_label("github.com/pekinlcc/Spitch")
        dlg.set_authors(["Spitch contributors"])
        try:
            dlg.set_license_type(Gtk.License.MIT_X11)
        except Exception:
            dlg.set_license("MIT — see LICENSE")
        # Best-effort logo from the bundled idle icon. If GdkPixbuf
        # rejects the SVG (rare), the dialog just shows no logo —
        # not worth bringing the whole daemon down for.
        try:
            from gi.repository import GdkPixbuf
            icon_path = _icon_dir() / _ICON_FOR_STATE[State.IDLE]
            if icon_path.is_file():
                pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(
                    str(icon_path), 96, 96,
                )
                dlg.set_logo(pixbuf)
        except Exception as exc:
            log.debug("about logo unavailable: %s", exc)
        dlg.run()
        dlg.destroy()

    def _on_quit_clicked(self, _widget):
        if callable(self._on_quit):
            self._on_quit()
        else:
            self._Gtk.main_quit()

    # -- public API (thread-safe) -------------------------------------

    def set_state(self, state: State) -> None:
        """Update icon + label for a state transition.

        Safe to call from any thread; marshals to the GTK main loop.
        """
        self._GLib.idle_add(self._apply_state, state)

    def set_partial(self, text: str) -> None:
        """Update the live partial transcript shown in the tray label.

        Called from the voice worker thread on every server partial.
        Safe to call from any thread.
        """
        self._GLib.idle_add(self._apply_partial, text or "")

    # -- internals (must run on GTK thread) ---------------------------

    def _apply_state(self, state: State) -> bool:
        self._state = state
        self._set_icon_for(state)
        # Going to IDLE: keep the last partial visible briefly so the
        # user sees the final recognition before the tray clears.
        # Any other transition cancels the linger and updates now.
        if self._linger_id:
            try:
                self._GLib.source_remove(self._linger_id)
            except Exception:
                pass
            self._linger_id = 0
        if state == State.IDLE and self._partial:
            self._linger_id = self._GLib.timeout_add(
                _POST_FINAL_LINGER_MS, self._clear_after_linger,
            )
        elif state != State.IDLE:
            # New active state — drop any stale partial from a prior
            # session so RECORDING starts visually clean.
            self._partial = ""
        self._refresh_label()
        self._refresh_menu_status()
        return False  # one-shot idle_add

    def _apply_partial(self, text: str) -> bool:
        self._partial = text
        # Any new partial supersedes a pending linger (the user is
        # talking again — the post-final fade should be cut short).
        if self._linger_id:
            try:
                self._GLib.source_remove(self._linger_id)
            except Exception:
                pass
            self._linger_id = 0
        self._refresh_label()
        return False

    def _clear_after_linger(self) -> bool:
        self._linger_id = 0
        self._partial = ""
        self._refresh_label()
        return False  # one-shot

    def _refresh_label(self) -> None:
        label = compose_label(self._state, self._partial)
        try:
            # The "guide" string is a width hint AppIndicator uses to
            # reserve space; pass the longest expected label we'll
            # actually render so the panel doesn't reflow on every
            # partial.
            self._ind.set_label(label, "🎙 ………………………………")
        except Exception as exc:
            log.debug("set_label failed: %s", exc)

    def _refresh_menu_status(self) -> None:
        labels = {
            State.IDLE:       "Spitch — idle",
            State.RECORDING:  "Spitch — recording…",
            State.FINALIZING: "Spitch — finalizing…",
            State.ERROR:      "Spitch — error",
        }
        try:
            self._item_status.set_label(labels.get(self._state, "Spitch"))
        except Exception:
            pass

    def _set_icon_for(self, state: State) -> None:
        path = str(_icon_dir() / _ICON_FOR_STATE.get(state, _ICON_FOR_STATE[State.IDLE]))
        try:
            self._ind.set_icon_full(path, f"Spitch ({state.value})")
        except Exception as exc:
            log.warning("set_icon_full failed: %s", exc)
