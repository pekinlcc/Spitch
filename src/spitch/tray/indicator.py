"""System-tray status indicator backed by libayatana-appindicator.

The indicator surfaces the current daemon state in the GNOME / KDE
top-right status area:

* idle      — yellow microphone   (waiting for the hotkey)
* recording — red microphone + pulsing dot (audio is streaming)
* finalizing— blue microphone + pulsing ring (waiting for ASR final)

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
    """Thin wrapper around AppIndicator + a tiny right-click menu."""

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

    def _on_quit_clicked(self, _widget):
        if callable(self._on_quit):
            self._on_quit()
        else:
            self._Gtk.main_quit()

    # -- public API (thread-safe) -------------------------------------

    def set_state(self, state: State) -> None:
        """Update icon + tooltip. Safe to call from any thread."""
        self._GLib.idle_add(self._apply_state, state)

    # -- internals (must run on GTK thread) ---------------------------

    def _apply_state(self, state: State) -> bool:
        self._state = state
        self._set_icon_for(state)
        labels = {
            State.IDLE:       "Spitch — idle",
            State.RECORDING:  "Spitch — recording…",
            State.FINALIZING: "Spitch — finalizing…",
            State.ERROR:      "Spitch — error",
        }
        try:
            self._item_status.set_label(labels.get(state, "Spitch"))
        except Exception:
            pass
        return False  # one-shot idle_add

    def _set_icon_for(self, state: State) -> None:
        path = str(_icon_dir() / _ICON_FOR_STATE.get(state, _ICON_FOR_STATE[State.IDLE]))
        try:
            self._ind.set_icon_full(path, f"Spitch ({state.value})")
        except Exception as exc:
            log.warning("set_icon_full failed: %s", exc)
