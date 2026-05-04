"""Inject CJK-friendly text into the focused application via the
clipboard + a synthetic Ctrl+V keystroke from /dev/uinput.

This bypasses the IM framework entirely — it works in any GTK / Qt /
Electron / native-Wayland app regardless of whether IBus or fcitx5 is
the active input method, as long as:

1. A clipboard helper is on the PATH:
     * Wayland sessions:  ``wl-copy`` / ``wl-paste`` (apt: ``wl-clipboard``)
     * X11 sessions:      ``xclip`` (apt: ``xclip``) or ``xsel`` (apt: ``xsel``)
2. /dev/uinput is writable by the current user (logind sets a per-session
   ACL on Ubuntu 24.04 by default).

Typing CJK characters via uinput key-by-key would require routing
through the active IM (which we deliberately skip), so we go through
the clipboard instead and synthesize the universal "paste" shortcut.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from typing import Optional, Tuple

log = logging.getLogger("spitch.inject")


# ---------------------------------------------------------------------------
# Clipboard backend selection (Wayland / X11)
# ---------------------------------------------------------------------------


def _detect_backend() -> Optional[str]:
    """Pick a clipboard backend appropriate for the current session.

    Honors the session type strictly: an X11 session with wl-clipboard
    installed but no xclip/xsel is NOT a reason to call wl-copy, which
    would hang trying to talk to a non-existent Wayland socket. Only
    if neither display var is set do we fall back to "any helper that
    exists" — that path covers truly headless test environments.
    """
    on_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    on_x11 = bool(os.environ.get("DISPLAY"))
    if on_wayland and shutil.which("wl-copy"):
        return "wayland"
    if on_x11 and shutil.which("xclip"):
        return "xclip"
    if on_x11 and shutil.which("xsel"):
        return "xsel"
    # Wayland session that also exports DISPLAY (XWayland) — fall back
    # to X11 helpers if wl-copy isn't around.
    if on_wayland and shutil.which("xclip"):
        return "xclip"
    if on_wayland and shutil.which("xsel"):
        return "xsel"
    # No display var set at all — honor whatever helper is present.
    if not on_wayland and not on_x11:
        if shutil.which("wl-copy"):
            return "wayland"
        if shutil.which("xclip"):
            return "xclip"
        if shutil.which("xsel"):
            return "xsel"
    return None


def _copy(data: bytes) -> Tuple[bool, str]:
    backend = _detect_backend()
    if backend is None:
        msg = "no clipboard helper found — install wl-clipboard (Wayland) or xclip/xsel (X11)"
        log.error(msg)
        return False, msg
    if backend == "wayland":
        cmd = ["wl-copy"]
    elif backend == "xclip":
        cmd = ["xclip", "-selection", "clipboard"]
    else:  # xsel
        cmd = ["xsel", "--clipboard", "--input"]
    try:
        subprocess.run(cmd, input=data, check=True, timeout=2)
        return True, ""
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        msg = f"{cmd[0]} failed: {e}"
        log.error(msg)
        return False, msg


def _paste() -> Optional[bytes]:
    """Read the current clipboard. Returns ``None`` if no helper is
    available or the call fails. Preserves trailing newlines so a
    save+restore round trip is byte-identical.
    """
    backend = _detect_backend()
    if backend == "wayland":
        if not shutil.which("wl-paste"):
            return None
        cmd = ["wl-paste"]
    elif backend == "xclip":
        cmd = ["xclip", "-selection", "clipboard", "-o"]
    elif backend == "xsel":
        cmd = ["xsel", "--clipboard", "--output"]
    else:
        return None
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=2)
        if r.returncode == 0:
            return r.stdout
    except subprocess.TimeoutExpired:
        return None
    return None


# ---------------------------------------------------------------------------
# Keystroke synthesis via /dev/uinput
# ---------------------------------------------------------------------------


_MOD_TO_CODE: dict[str, str] = {
    "ctrl":    "KEY_LEFTCTRL",
    "control": "KEY_LEFTCTRL",
    "shift":   "KEY_LEFTSHIFT",
    "alt":     "KEY_LEFTALT",
    "meta":    "KEY_LEFTALT",
    "super":   "KEY_LEFTMETA",
    "win":     "KEY_LEFTMETA",
}


def _parse_keystroke(spec: str) -> tuple[list[int], int]:
    """Parse ``"Ctrl+Shift+V"`` → ``([KEY_LEFTCTRL, KEY_LEFTSHIFT], KEY_V)``.

    Returns ``(modifier_codes, main_key_code)``. Raises ValueError if the
    spec is malformed or names a key evdev does not know.
    """
    from evdev import ecodes as ec
    mods: list[int] = []
    main: int | None = None
    for raw in spec.replace("-", "+").split("+"):
        p = raw.strip()
        if not p:
            continue
        low = p.lower()
        if low in _MOD_TO_CODE:
            mods.append(getattr(ec, _MOD_TO_CODE[low]))
            continue
        # Last token is the main key. evdev's KEY_* names are uppercase.
        name = p.upper() if len(p) == 1 else p
        attr = "KEY_" + name.upper()
        if not hasattr(ec, attr):
            raise ValueError(f"unknown key '{p}' in keystroke spec '{spec}'")
        if main is not None:
            raise ValueError(
                f"keystroke spec '{spec}' has more than one non-modifier key"
            )
        main = getattr(ec, attr)
    if main is None:
        raise ValueError(f"keystroke spec '{spec}' must include a main key (e.g. V)")
    return mods, main


def _send_paste_keystroke(spec: str = "Ctrl+Shift+V") -> Tuple[bool, str]:
    """Synthesize the configured paste keystroke via /dev/uinput.

    The kernel routes the events to the focused window the same way a
    real keyboard would. Default ``Ctrl+Shift+V`` works in terminals,
    browsers, address bars, Slack/Feishu, Word, Google Docs.
    """
    try:
        from evdev import UInput, ecodes as ec
    except ImportError:
        msg = "python-evdev not importable"
        log.error(msg)
        return False, msg
    try:
        mods, main = _parse_keystroke(spec)
    except ValueError as exc:
        msg = f"invalid paste_keystroke {spec!r}: {exc}"
        log.error(msg)
        return False, msg
    try:
        ui = UInput(name="spitch-injector")
    except (OSError, PermissionError) as e:
        msg = f"cannot open /dev/uinput ({e}) — check ACL"
        log.error(msg)
        return False, msg
    # Wait for udev → libinput → compositor to enumerate the new
    # virtual keyboard into the seat. Without this delay the first few
    # EV_KEY events can be dropped on fast machines and the paste
    # silently does nothing — only a retry recovers. 30 ms is enough
    # in practice on GNOME/KDE Wayland and X11.
    time.sleep(0.03)
    try:
        # Press modifiers, then main key, with small inter-event waits
        # so the compositor sees a clean chord rather than simultaneous
        # events that some focused apps drop.
        for m in mods:
            ui.write(ec.EV_KEY, m, 1); ui.syn()
            time.sleep(0.005)
        ui.write(ec.EV_KEY, main, 1); ui.syn()
        time.sleep(0.02)
        ui.write(ec.EV_KEY, main, 0); ui.syn()
        time.sleep(0.005)
        for m in reversed(mods):
            ui.write(ec.EV_KEY, m, 0); ui.syn()
        return True, ""
    except OSError as e:
        msg = f"uinput write failed: {e}"
        log.error(msg)
        return False, msg
    finally:
        time.sleep(0.05)
        try:
            ui.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def inject_text(
    text: str,
    *,
    paste_keystroke: str = "Ctrl+Shift+V",
    restore_clipboard: bool = True,
    restore_delay_ms: int = 300,
) -> Tuple[bool, str]:
    """Paste ``text`` into the focused app.

    Steps:
      1. Save current clipboard (if ``restore_clipboard``).
      2. Write ``text`` to clipboard.
      3. Send the configured paste keystroke via uinput.
      4. After ``restore_delay_ms`` milliseconds, restore the saved
         clipboard so we don't surprise the user with stale content
         next time they paste manually. The restore runs in
         ``finally`` so it also fires on failure paths after the
         clipboard was overwritten.

    ``paste_keystroke`` defaults to ``Ctrl+Shift+V`` — works in terminals
    (where Ctrl+V is literal-quote), browsers, Slack, Feishu, etc.
    ``restore_delay_ms`` defaults to 300 ms; bump it to 600–800 ms if
    your target Electron app is slow to consume the paste.

    Returns ``(ok, reason)``. ``reason`` is empty on success and a
    short human-readable string on failure (so the daemon can surface
    a useful notification rather than a generic "uinput permissions"
    blurb when the real cause is a missing clipboard helper).
    """
    if not text:
        return True, ""
    saved = _paste() if restore_clipboard else None
    clipboard_was_overwritten = False
    try:
        ok, reason = _copy(text.encode("utf-8"))
        if not ok:
            return False, reason
        clipboard_was_overwritten = True
        ok, reason = _send_paste_keystroke(paste_keystroke)
        if not ok:
            return False, reason
        return True, ""
    finally:
        if saved is not None and clipboard_was_overwritten:
            # Settle: let the focused app actually consume the paste
            # before we overwrite the clipboard with the saved bytes.
            # Runs even on the failure path so the user gets their
            # original clipboard back if our paste failed mid-way.
            time.sleep(max(0.0, restore_delay_ms / 1000.0))
            try:
                _copy(saved)
            except Exception:
                pass
