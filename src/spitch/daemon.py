"""Spitch daemon — global hotkey + voice ASR + clipboard text injection.

Runs as a long-lived user process. Listens for the configured talk-key
combo (default ``Ctrl+Alt``) via /dev/input/event*, captures audio while
held, streams it to Doubao for realtime ASR, and on release injects the
final punctuated text into the focused application via the clipboard +
a synthetic Ctrl+V from /dev/uinput.

The whole path is IM-framework-independent — it works in any
GTK / Qt / Electron / native-Wayland application regardless of whether
the user has IBus, fcitx5, or no IM at all configured. That is the
release-friendly choice the project switched to in v0.2.
"""

from __future__ import annotations

import logging
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from .config import is_complete, is_verified, load_config
from .hotkey import HotkeyListener, parse_combo
from .inject import inject_text
from .tray import try_create as try_create_indicator
from .voice import (
    AudioCapture,
    AudioConfig,
    DoubaoClient,
    DoubaoCredentials,
    State,
    VoiceController,
)

log = logging.getLogger("spitch.daemon")


def _notify(summary: str, body: str = "") -> None:
    if not shutil.which("notify-send"):
        return
    try:
        subprocess.Popen(
            [
                "notify-send", "-a", "Spitch",
                "-i", "audio-input-microphone",
                "-t", "1500",
                summary, body,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


class SpitchDaemon:
    def __init__(self, cfg: dict):
        self._cfg = cfg
        # Per-press queue: created in _on_press, captured by _on_release
        # before the next press can replace it. Decouples session state
        # from shared-mutable globals so a fast re-press can't blank out
        # the previous session's final text before the inject thread reads it.
        self._pending_final: Optional["queue.Queue[str]"] = None
        self._listener: Optional[HotkeyListener] = None
        self._voice: Optional[VoiceController] = None
        self._indicator = None  # set in run() if the typelib is present
        self._finalize_timeout = float(
            (cfg.get("inject") or {}).get("final_wait_seconds", 5.0)
        )
        # Serialize the actual paste step. _finalize_and_inject runs on
        # a fresh thread per release, and a fast re-press scenario can
        # have N>1 inject threads alive at once (one waiting for the
        # server's final, another doing the quiescence wait). Without a
        # lock they'd race on the clipboard and on /dev/uinput, producing
        # interleaved keystrokes and stomped clipboard contents.
        self._inject_lock = threading.Lock()

    def _build_voice(self) -> VoiceController:
        d = self._cfg["doubao"]
        creds = DoubaoCredentials(
            app_key=d["app_key"],
            access_key=d["access_key"],
            resource_id=d.get("resource_id", "volc.bigasr.sauc.duration"),
            endpoint=d.get(
                "endpoint",
                "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
            ),
        )
        sample_rate = (self._cfg.get("audio") or {}).get("sample_rate", 16000)
        audio = AudioCapture(AudioConfig(sample_rate=sample_rate))
        return VoiceController(
            client_factory=lambda: DoubaoClient(creds, sample_rate=sample_rate),
            audio=audio,
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_error=self._on_error,
            on_state=self._on_state,
        )

    # -- voice callbacks ----------------------------------------------

    def _on_partial(self, text: str) -> None:
        if text:
            log.info("partial: …%s", text[-40:])

    def _on_final(self, text: str) -> None:
        log.info("final: %r", text)
        # on_final fires from inside the controller's session, which means
        # the corresponding _on_press has already run and self._pending_final
        # still references this session's queue (the next press only happens
        # after the session ends).
        q = self._pending_final
        if q is not None:
            try:
                q.put_nowait(text)
            except queue.Full:
                pass

    def _on_error(self, exc: BaseException) -> None:
        log.warning("voice error: %s", exc)
        _notify("Spitch — error", str(exc)[:120])

    def _on_state(self, s: State) -> None:
        if self._indicator is not None:
            self._indicator.set_state(s)
        if s == State.RECORDING:
            _notify("🎙 Spitch listening…")
        elif s == State.FINALIZING:
            _notify("✍ Spitch finalizing…")

    # -- hotkey callbacks ---------------------------------------------

    def _on_press(self) -> None:
        if self._voice is None:
            _notify("Spitch", "Not configured — run spitch-config")
            return
        # Only swap _pending_final after press() actually accepts —
        # otherwise a press during FINALIZING (rejected by the state
        # machine) would replace the previous session's queue and
        # the still-pending on_final would write to a queue nobody
        # is reading from.
        new_pending: "queue.Queue[str]" = queue.Queue(maxsize=1)
        if not self._voice.press():
            log.info("press: voice not idle (state=%s)", self._voice.state)
            return
        self._pending_final = new_pending

    def _on_release(self) -> None:
        if self._voice is None:
            return
        if self._voice.state != State.RECORDING:
            return
        # Capture the queue BEFORE release() — by the time the inject
        # thread runs, a fast next-press may have replaced
        # self._pending_final with a fresh queue.
        pending = self._pending_final
        self._voice.release()
        threading.Thread(
            target=self._finalize_and_inject,
            args=(pending,),
            name="spitch-inject",
            daemon=True,
        ).start()

    def _on_cancel(self) -> None:
        if self._voice is None:
            return
        self._voice.cancel()
        log.info("cancelled (third key during chord)")

    # -- finalize+inject ----------------------------------------------

    def _finalize_and_inject(self, pending: "queue.Queue[str]") -> None:
        try:
            text = pending.get(timeout=self._finalize_timeout)
        except queue.Empty:
            log.warning("no final transcript within %.1fs", self._finalize_timeout)
            return
        if not text:
            return
        # Wait for the user to physically release all hotkey modifiers
        # before we synthesize Ctrl+V — otherwise the still-held Alt
        # would turn our paste into Ctrl+Alt+V (a different shortcut).
        deadline = time.time() + 2.0
        while time.time() < deadline:
            if self._listener and self._listener.is_quiescent():
                break
            time.sleep(0.02)
        keystroke = (self._cfg.get("inject") or {}).get(
            "paste_keystroke", "Ctrl+Shift+V"
        )
        # Hold the lock across the whole clipboard write + keystroke +
        # restore so a second inject thread can't slip in between, copy
        # its text, and have us paste it for them.
        with self._inject_lock:
            ok, reason = inject_text(text, paste_keystroke=keystroke)
        if not ok:
            _notify("Spitch — inject failed", reason or "unknown error")

    # -- main loop ----------------------------------------------------

    def run(self) -> int:
        if not is_complete(self._cfg):
            print(
                "spitch: configure Doubao first — run spitch-config",
                file=sys.stderr,
            )
            return 2
        if not is_verified(self._cfg):
            print(
                "spitch: not verified — run spitch-config and click "
                "'Test connection' before launching the daemon",
                file=sys.stderr,
            )
            return 2
        self._voice = self._build_voice()
        combo = parse_combo(
            (self._cfg.get("hotkey") or {}).get("talk_key", "Ctrl+Alt")
        )
        if not combo:
            print(
                "spitch: invalid talk_key — set hotkey.talk_key to a "
                "modifier-pair like 'Ctrl+Alt'",
                file=sys.stderr,
            )
            return 2
        self._listener = HotkeyListener(
            combo,
            on_press=self._on_press,
            on_release=self._on_release,
            on_cancel=self._on_cancel,
        )
        try:
            self._listener.start()
        except RuntimeError as exc:
            print(f"spitch: {exc}", file=sys.stderr)
            return 3
        log.info("Spitch daemon ready — hold %s to talk", "+".join(combo))
        _notify(
            "Spitch ready",
            "Hold " + "+".join(c.title() for c in combo) + " to talk",
        )

        # Try to put up a tray indicator. If the AppIndicator typelib
        # is missing — or if it's present but Gtk import fails — we
        # fall back to a headless Event.wait() loop. We also fall back
        # to headless if try_create_indicator returns None (typelib
        # missing) so the user isn't stuck in a hidden Gtk loop with
        # no way to quit but SIGTERM.
        Gtk = None
        GLib = None
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk as _Gtk, GLib as _GLib
            Gtk, GLib = _Gtk, _GLib
            self._indicator = try_create_indicator(
                on_quit=lambda: GLib.idle_add(Gtk.main_quit),
            )
        except (ValueError, ImportError):
            Gtk = GLib = None

        if Gtk is not None and self._indicator is not None:
            def _quit(*_):
                Gtk.main_quit()
                return GLib.SOURCE_REMOVE
            try:
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, _quit)
                GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, _quit)
            except Exception:
                signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())
                signal.signal(signal.SIGTERM, lambda *_: Gtk.main_quit())
            try:
                Gtk.main()
            finally:
                if self._listener:
                    self._listener.stop()
            return 0

        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            stop.wait()
        finally:
            if self._listener:
                self._listener.stop()
        return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(name)s %(levelname)s] %(message)s",
    )
    cfg = load_config()
    return SpitchDaemon(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
