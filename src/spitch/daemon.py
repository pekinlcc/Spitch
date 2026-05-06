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

from .cmdsock import CmdServer, default_socket_path
from .config import is_complete, is_verified, load_config
from .history import HistoryEntry, HistoryRing, default_history_path
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


class _WebsocketsAttributeErrorFilter(logging.Filter):
    """Suppress a known noisy traceback from the websockets library.

    On a server-side connection reset during a session, websockets'
    ``Connection.connection_lost`` callback can run before its
    ``recv_messages`` attribute has been initialized, producing:

        AttributeError: 'ClientConnection' object has no attribute 'recv_messages'

    The exception is harmless — the underlying ``ConnectionResetError``
    is already propagated to our session loop and surfaces as a normal
    ``voice error: ConnectionResetError`` warning. The traceback just
    pollutes daemon.log with five lines of irrelevant stack. Filter it
    out so the log stays useful for actual debugging.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        msg = record.getMessage()
        if "Connection.connection_lost" in msg and "recv_messages" in (
            record.exc_text or msg
        ):
            return False
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            if (
                isinstance(exc, AttributeError)
                and "recv_messages" in str(exc)
            ):
                return False
        return True


def _active_window_label() -> str:
    """Best-effort label for the currently-focused window. Used as a
    metadata tag in history entries — the user looking at history
    might want to know which app they were dictating into.

    Tries a couple of common Linux window-info tools and gives up
    silently if none are available. Empty string means "unknown".
    """
    # xdotool works on X11 + XWayland.
    if shutil.which("xdotool"):
        try:
            r = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, timeout=0.3, text=True,
            )
            if r.returncode == 0:
                name = r.stdout.strip()
                if name:
                    return name[:80]
        except (subprocess.TimeoutExpired, OSError):
            pass
    # Wayland (GNOME / KDE) doesn't expose a portable focused-window
    # API to unprivileged clients, so we just return empty.
    return ""


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
        # Audio capture lives across sessions in continuous-capture
        # mode; daemon owns its lifecycle (open at run() start, close
        # at shutdown). Stored here so run() can call open()/close()
        # on the same instance the controller is using.
        self._audio: Optional[AudioCapture] = None
        # Per-press queue: created in _on_press, captured by _on_release
        # before the next press can replace it. Decouples session state
        # from shared-mutable globals so a fast re-press can't blank out
        # the previous session's final text before the inject thread reads it.
        self._pending_final: Optional["queue.Queue[str]"] = None
        # Set when a press() was accepted by the voice controller. Used
        # by _on_release to decide whether to start an inject thread,
        # *without* re-checking voice.state — the controller can already
        # have transitioned back to IDLE if Doubao sent a definite=true
        # frame before the user physically released the modifiers.
        self._press_accepted = False
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
        # v0.5: recent-transcript history + cmd socket. The console UI
        # and the spitch-cli tool both talk to the daemon via this
        # socket to list / re-paste / clear history without restarting.
        history_capacity = 50
        try:
            history_capacity = int((cfg.get("history") or {}).get("capacity", 50))
        except (TypeError, ValueError):
            history_capacity = 50
        self._history = HistoryRing(
            capacity=history_capacity,
            path=default_history_path(),
        )
        self._cmdserver: Optional[CmdServer] = None
        # When set, _finalize_and_inject stamps the time the press
        # was accepted so we can record the recording duration in
        # the history entry.
        self._press_started_at: float = 0.0

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
        audio_cfg = self._cfg.get("audio") or {}
        sample_rate = audio_cfg.get("sample_rate", 16000)
        try:
            prebuffer_ms = int(audio_cfg.get("prebuffer_ms", 500))
        except (TypeError, ValueError):
            prebuffer_ms = 500
        self._audio = AudioCapture(
            AudioConfig(sample_rate=sample_rate, prebuffer_ms=prebuffer_ms)
        )
        return VoiceController(
            client_factory=lambda: DoubaoClient(creds, sample_rate=sample_rate),
            audio=self._audio,
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_error=self._on_error,
            on_state=self._on_state,
        )

    # -- voice callbacks ----------------------------------------------

    def _on_partial(self, text: str) -> None:
        if text:
            log.info("partial: …%s", text[-40:])
        # Stream partials into the tray label so the user sees what
        # the server is recognizing in real time. Cheap — the
        # indicator coalesces via GLib.idle_add and only the latest
        # value is rendered on the panel.
        if self._indicator is not None:
            self._indicator.set_partial(text)

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
        # Push the final into the tray too so the user briefly sees
        # the recognized text under a checkmark after the session
        # ends. The indicator's IDLE-linger timer keeps it visible
        # for a short window before the label clears.
        if self._indicator is not None:
            self._indicator.set_partial(text)

    def _on_error(self, exc: BaseException) -> None:
        log.warning("voice error: %s", exc)
        _notify("Spitch — error", str(exc)[:120])

    def _on_state(self, s: State) -> None:
        if self._indicator is not None:
            # Tray icon + label provide all the state feedback the
            # user needs; suppress the desktop notification popups
            # that used to fire here so we don't double up with a
            # less-elegant top-of-screen toast for every press.
            self._indicator.set_state(s)
            return
        # Headless fallback (no AppIndicator typelib): keep the
        # legacy notify-send path so the user still gets *some*
        # feedback that the daemon registered the press.
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
        self._press_accepted = True
        self._press_started_at = time.time()
        log.info("press: session started (state=%s)", self._voice.state)

    def _on_release(self) -> None:
        if self._voice is None:
            return
        # Don't gate on voice.state — Doubao may have already sent a
        # definite=true frame while the user was still holding the keys,
        # which transitions the controller back to IDLE. We still need
        # to inject the text in that case. The _press_accepted flag is
        # the source of truth for "this release pairs with an accepted
        # press of OUR session".
        if not self._press_accepted:
            log.info("release: ignored (no accepted press)")
            return
        self._press_accepted = False
        log.info("release: voice.state=%s, scheduling inject", self._voice.state)
        # Capture the queue locally so a later, fast next-press that
        # replaces self._pending_final with Q2 cannot redirect *our*
        # inject thread to the wrong queue. Do NOT clear
        # self._pending_final here — the worker may still be in
        # FINALIZING and on_final fires by reading self._pending_final;
        # if we'd nulled it the slow-final path would silently drop
        # the transcript. The next accepted press is the only thing
        # that legitimately replaces it.
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
        # Drop the queue and the accepted-press flag so the eventual
        # _on_release (the user is still holding the modifiers when
        # cancel fires) does not start an inject thread that would
        # block on an empty queue and surface a misleading
        # "no final transcript" warning 5 seconds later.
        self._press_accepted = False
        self._pending_final = None
        log.info("cancelled (third key during chord)")

    # -- finalize+inject ----------------------------------------------

    def _finalize_and_inject(self, pending: "queue.Queue[str]") -> None:
        press_started = self._press_started_at or time.time()
        try:
            text = pending.get(timeout=self._finalize_timeout)
        except queue.Empty:
            log.warning("no final transcript within %.1fs", self._finalize_timeout)
            return
        if not text:
            log.warning("inject: empty text from queue, aborting")
            return
        log.info(
            "inject: prep text len=%d preview=%r",
            len(text), text[:60] + ("…" if len(text) > 60 else ""),
        )
        # Wait for the user to physically release all hotkey modifiers
        # before we synthesize Ctrl+V — otherwise the still-held Alt
        # would turn our paste into Ctrl+Alt+V (a different shortcut).
        # The listener exposes an Event that flips on the release of
        # the last modifier; blocking on it idle-burns 0% CPU between
        # presses (the previous busy-poll spent 50 wakeups/s here).
        if self._listener is not None:
            quiescent = self._listener.wait_quiescent(timeout=2.0)
            if not quiescent:
                log.warning(
                    "inject: hotkey modifiers still held after 2s — "
                    "synthesized paste will fight the held modifiers"
                )
        ok, reason = self._inject_text_locked(text)
        log.info("inject: result ok=%s reason=%r", ok, reason)
        if not ok:
            _notify("Spitch — inject failed", reason or "unknown error")
        # Record this session in history regardless of inject success —
        # the user may want to repaste a session whose first inject was
        # eaten by a slow Electron app.
        try:
            self._history.append(HistoryEntry(
                timestamp=time.time(),
                text=text,
                duration_s=max(0.0, time.time() - press_started),
                inject_ok=bool(ok),
                target_app=_active_window_label(),
            ))
        except Exception:
            log.exception("history append failed (non-fatal)")

    def _inject_text_locked(self, text: str) -> tuple[bool, str]:
        """Run inject_text with the daemon's serialization lock applied.

        Used both by _finalize_and_inject (live press) and by
        cmdsock repaste handlers (console / cli).
        """
        inject_cfg = self._cfg.get("inject") or {}
        keystroke = inject_cfg.get("paste_keystroke", "Ctrl+Shift+V")
        try:
            restore_delay_ms = int(inject_cfg.get("restore_clipboard_delay_ms", 800))
        except (TypeError, ValueError):
            restore_delay_ms = 800
        with self._inject_lock:
            return inject_text(
                text,
                paste_keystroke=keystroke,
                restore_delay_ms=restore_delay_ms,
            )

    # -- cmd socket handlers (called from the cmdsock thread) ----------

    def _cmd_ping(self, _req: dict) -> dict:
        from . import __version__
        return {"version": __version__}

    def _cmd_list_history(self, _req: dict) -> dict:
        return {"entries": [e.to_dict() for e in self._history.all()]}

    def _cmd_repaste(self, req: dict) -> dict:
        try:
            index = int(req.get("index", -1))
        except (TypeError, ValueError):
            return {"ok": False, "error": "index must be an integer"}
        entry = self._history.get(index)
        if entry is None:
            return {"ok": False, "error": f"no history entry at index {index}"}
        # Spawn a worker thread so the cmdsock response returns
        # immediately — paste involves uinput keystrokes + 800ms
        # restore-delay sleep.
        def _do():
            ok, reason = self._inject_text_locked(entry.text)
            log.info("repaste: ok=%s reason=%r", ok, reason)
            if not ok:
                _notify("Spitch — repaste failed", reason or "unknown error")
        threading.Thread(target=_do, name="spitch-repaste", daemon=True).start()
        return {"ok": True, "scheduled": True, "text_preview": entry.text[:60]}

    def _cmd_delete_history(self, req: dict) -> dict:
        try:
            index = int(req.get("index"))
        except (TypeError, ValueError):
            return {"ok": False, "error": "index must be an integer"}
        if not self._history.remove(index):
            return {"ok": False, "error": f"no history entry at index {index}"}
        return {"ok": True}

    def _cmd_clear_history(self, _req: dict) -> dict:
        self._history.clear()
        return {"ok": True}

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
        if len(combo) < 2:
            # Single-modifier hold is unusable: Ctrl/Alt/Shift/Super get
            # pressed dozens of times per minute for system shortcuts
            # and would each trigger a recording. Reject with a
            # specific, fixable message rather than letting the daemon
            # come up and behave erratically.
            print(
                f"spitch: hotkey.talk_key must combine two modifiers "
                f"(got just '{combo[0]}'). Try 'Ctrl+Alt' or "
                "'Ctrl+Shift'.",
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
        # Pre-open the mic so the very first press doesn't pay the
        # 50–500 ms backend warm-up latency that otherwise eats the
        # head of the user's first utterance. With prebuffer_ms == 0
        # this is a no-op and we fall back to open-on-press.
        if self._audio is not None:
            try:
                backend = self._audio.open()
                if backend:
                    log.info("audio backend warmed up: %s", backend)
            except Exception as exc:
                # If continuous capture failed (busy device, missing
                # backend), don't kill the daemon — fall back to
                # open-on-press by leaving the mic closed. The first
                # press's audio.start() will retry and surface a real
                # error to the user via the controller.
                log.warning(
                    "could not pre-open mic (%s) — will open on press", exc
                )
        # Warm up the WebSocket path to Doubao so the first press
        # doesn't pay the cold DNS + TCP + TLS + WS-upgrade latency
        # (we've measured 5+ seconds on the first connect after a
        # fresh boot — long enough that a short utterance can finish
        # before the connection is even established, leaving the daemon
        # with nothing to inject). Periodic re-warm in a background
        # thread keeps the network path hot during idle stretches.
        threading.Thread(
            target=self._network_warmup_loop,
            name="spitch-warmup",
            daemon=True,
        ).start()
        # Start the command socket so the console UI / spitch-cli can
        # list history, repaste an old transcript, etc. Failure is
        # non-fatal — voice input still works without it.
        try:
            self._cmdserver = CmdServer(
                handlers={
                    "ping":           self._cmd_ping,
                    "list":           self._cmd_list_history,
                    "list_history":   self._cmd_list_history,  # alias
                    "repaste":        self._cmd_repaste,
                    "delete":         self._cmd_delete_history,
                    "delete_history": self._cmd_delete_history,  # alias
                    "clear":          self._cmd_clear_history,
                    "clear_history":  self._cmd_clear_history,  # alias
                },
                path=default_socket_path(),
            )
            self._cmdserver.start()
        except Exception as exc:
            log.warning("could not start cmd socket (%s) — console / "
                        "spitch-cli won't be able to talk to daemon", exc)
            self._cmdserver = None
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
                self._shutdown()
            return 0

        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        try:
            stop.wait()
        finally:
            self._shutdown()
        return 0

    def _network_warmup_loop(self) -> None:
        """Pre-establish (then close) a WebSocket to Doubao on a timer.

        First connect after a cold boot can take 5+ seconds — DNS
        resolution + TCP handshake + TLS handshake + WS upgrade, none
        of which are cached. If the user's press happens during that
        cold period, the audio capture sits in the session queue
        waiting for the connection while the user already finishes
        speaking and releases. The session ends with no transcript.

        This loop opens a probe connection on daemon start and then
        every 4 minutes — short enough that the OS keeps DNS in
        cache and the TLS resumption ticket stays warm, long enough
        that we're not hammering Doubao's auth endpoint.
        """
        import asyncio
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

        async def _one_probe() -> float:
            t0 = time.time()
            client = DoubaoClient(creds)
            try:
                await client.__aenter__()
            finally:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:
                    pass
            return time.time() - t0

        while True:
            try:
                loop = asyncio.new_event_loop()
                try:
                    elapsed = loop.run_until_complete(_one_probe())
                finally:
                    loop.close()
                log.info("network warmup: %.2fs", elapsed)
            except Exception as exc:
                log.warning("network warmup failed: %s", exc)
            time.sleep(240.0)  # 4 min

    def _shutdown(self) -> None:
        """Clean shutdown: stop hotkey listener and close the mic.

        Called from both the GTK and headless main loops on exit. The
        mic close releases the ALSA / PortAudio handle so a re-launch
        of the daemon doesn't hit "device busy" on the same hardware.
        Also tear down the cmd socket so a stale path doesn't fool
        ``spitch-cli`` next time the daemon starts.
        """
        if self._cmdserver is not None:
            try:
                self._cmdserver.stop()
            except Exception:
                pass
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
        if self._audio is not None:
            try:
                self._audio.close()
            except Exception:
                pass


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s %(name)s %(levelname)s] %(message)s",
    )
    # Quiet a known-noisy traceback from the websockets library that
    # fires on server-side connection resets. The underlying error is
    # already surfaced through our own voice-error path.
    _ws_filter = _WebsocketsAttributeErrorFilter()
    logging.getLogger("asyncio").addFilter(_ws_filter)
    logging.getLogger("websockets").addFilter(_ws_filter)
    cfg = load_config()
    return SpitchDaemon(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
