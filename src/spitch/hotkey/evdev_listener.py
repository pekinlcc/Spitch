"""Global keyboard hotkey listener via /dev/input/event* (evdev).

Watches every keyboard device for a configured modifier-pair (e.g.
``Ctrl+Alt``) held simultaneously, with no third non-modifier key
pressed during the chord. Press / release / cancel events fire on the
caller-provided callbacks. The listener runs in its own daemon thread
and is IM-framework-independent — it works on Wayland and X11 alike.

Reading from /dev/input/event* requires the user to be in the ``input``
group (or have an equivalent ACL). The ``start()`` method raises a
descriptive RuntimeError if no readable keyboard is found.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

log = logging.getLogger("spitch.hotkey")


_MOD_KEYS: dict[str, set[int]] = {}


def _init_codes() -> None:
    global _MOD_KEYS
    if _MOD_KEYS:
        return
    from evdev import ecodes as ec
    _MOD_KEYS = {
        "ctrl":  {ec.KEY_LEFTCTRL, ec.KEY_RIGHTCTRL},
        "alt":   {ec.KEY_LEFTALT, ec.KEY_RIGHTALT},
        "shift": {ec.KEY_LEFTSHIFT, ec.KEY_RIGHTSHIFT},
        "super": {ec.KEY_LEFTMETA, ec.KEY_RIGHTMETA},
    }


def parse_combo(spec: str) -> list[str]:
    """Parse ``"Ctrl+Alt"`` → ``['ctrl', 'alt']``. Order-insensitive,
    duplicates removed. Unknown tokens are dropped.
    """
    out: list[str] = []
    for raw in spec.replace("-", "+").split("+"):
        p = raw.strip().lower()
        if p in ("ctrl", "control"):
            p = "ctrl"
        elif p in ("alt", "meta"):
            p = "alt"
        elif p in ("super", "win"):
            p = "super"
        if p in ("ctrl", "alt", "shift", "super") and p not in out:
            out.append(p)
    return out


def list_keyboards():
    """All input devices that look like keyboards (have KEY_A + KEY_LEFTCTRL)."""
    from evdev import InputDevice, list_devices, ecodes as ec
    devs = []
    for path in list_devices():
        try:
            d = InputDevice(path)
        except (OSError, PermissionError) as e:
            log.debug("cannot open %s: %s", path, e)
            continue
        caps = d.capabilities().get(ec.EV_KEY, [])
        if ec.KEY_A in caps and ec.KEY_LEFTCTRL in caps:
            devs.append(d)
        else:
            d.close()
    return devs


class HotkeyListener:
    """Detect a hold-to-talk modifier-pair hotkey on the global keyboard.

    Fires ``on_press`` the moment all configured modifiers (e.g. Ctrl
    AND Alt) are held simultaneously. Fires ``on_release`` as soon as
    any one of them is released. If a non-modifier key is pressed
    during the chord, fires ``on_cancel`` and the next combo arrival
    is required to re-fire ``on_press`` — this lets system shortcuts
    like Ctrl+Alt+T pass through cleanly.
    """

    def __init__(
        self,
        combo: Iterable[str],
        *,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
        on_cancel: Callable[[], None] | None = None,
        allow_single_mod: bool = False,
    ):
        _init_codes()
        self._wanted = list(combo)
        if len(self._wanted) < 2 and not allow_single_mod:
            # Single-modifier push-to-talk is normally unusable: Ctrl
            # / Alt / Shift / Super get pressed dozens of times per
            # minute for system shortcuts and would each kick off a
            # bogus recording. ``allow_single_mod=True`` opts into it
            # for the salmon-mode subscriber, which routes the
            # transcript to a dedicated app (the overlay) instead of
            # pasting into whichever window happens to be focused —
            # the conflict the gate guards against doesn't apply.
            raise ValueError(
                "combo must contain at least two distinct modifier keys "
                "(got %r) — pass allow_single_mod=True to opt into a "
                "single-modifier hold" % self._wanted
            )
        self._wanted_codes: set[int] = set().union(
            *(_MOD_KEYS[m] for m in self._wanted)
        )
        self._all_mod_codes: set[int] = set().union(*_MOD_KEYS.values())
        self._on_press = on_press
        self._on_release = on_release
        self._on_cancel = on_cancel or (lambda: None)
        self._held: dict[str, bool] = {m: False for m in self._wanted}
        self._talk_active = False
        self._stop = threading.Event()
        # Set whenever none of the wanted modifiers is currently held.
        # Lets the inject thread block on Event.wait() instead of
        # busy-polling is_quiescent().
        self._quiescent_event = threading.Event()
        self._quiescent_event.set()
        self._thread: threading.Thread | None = None
        self._devices: list = []

    def start(self) -> None:
        self._devices = list_keyboards()
        if not self._devices:
            raise RuntimeError(
                "no readable keyboard devices found — add the user to "
                "the 'input' group: 'sudo usermod -aG input $USER' "
                "and log out / back in"
            )
        log.info("listening on %d keyboard device(s)", len(self._devices))
        self._thread = threading.Thread(
            target=self._run, name="spitch-hotkey", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        for d in self._devices:
            try:
                d.close()
            except Exception:
                pass
        self._devices = []

    def is_quiescent(self) -> bool:
        """True when none of the wanted modifiers is currently held."""
        return not any(self._held.values())

    def wait_quiescent(self, timeout: float | None = None) -> bool:
        """Block until all wanted modifiers are released.

        Returns ``True`` if quiescence was observed within ``timeout``,
        ``False`` if the timeout fired first. ``None`` waits forever.
        Used by the inject thread instead of a busy-poll over
        ``is_quiescent()`` so the daemon idle-burns 0% CPU between
        releases.
        """
        return self._quiescent_event.wait(timeout=timeout)

    def _run(self) -> None:
        from evdev import ecodes as ec
        from select import select
        fds = {d.fd: d for d in self._devices}
        while not self._stop.is_set():
            try:
                r, _, _ = select(list(fds.keys()), [], [], 0.5)
            except (OSError, ValueError):
                return
            for fd in r:
                d = fds.get(fd)
                if d is None:
                    continue
                try:
                    for ev in d.read():
                        if ev.type == ec.EV_KEY:
                            self._on_key(ev.code, ev.value)
                except OSError:
                    fds.pop(fd, None)

    def _on_key(self, code: int, value: int) -> None:
        # value: 0=release, 1=press, 2=autorepeat
        is_press = value == 1
        is_release = value == 0
        wanted_mod: str | None = None
        for name in self._wanted:
            if code in _MOD_KEYS[name]:
                wanted_mod = name
                break
        if wanted_mod is not None:
            if is_press:
                self._held[wanted_mod] = True
            elif is_release:
                self._held[wanted_mod] = False
            # Maintain the quiescent event in lockstep with _held so a
            # blocked wait_quiescent() returns the moment the user
            # finishes releasing the chord.
            if any(self._held.values()):
                self._quiescent_event.clear()
            else:
                self._quiescent_event.set()
            all_held = all(self._held[m] for m in self._wanted)
            if all_held and not self._talk_active:
                self._talk_active = True
                self._safe(self._on_press)
            elif not all_held and self._talk_active:
                self._talk_active = False
                self._safe(self._on_release)
            return
        # Non-wanted key event. If during a chord and it's a real key
        # (not a different modifier like Shift), the user meant a
        # shortcut — cancel the talk session.
        if (
            is_press
            and self._talk_active
            and code not in self._all_mod_codes
        ):
            self._talk_active = False
            self._safe(self._on_cancel)

    @staticmethod
    def _safe(fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception:
            log.exception("hotkey callback raised")
