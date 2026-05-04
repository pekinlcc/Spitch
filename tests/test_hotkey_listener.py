"""Tests for HotkeyListener helpers that don't need real evdev devices.

evdev's InputDevice / list_devices live behind /dev/input/event* so we
can't open them in unit tests, but the parse_combo helper, the
constructor's validation, and the wait_quiescent / is_quiescent state
machine are all exercisable without the kernel side.
"""

from __future__ import annotations

import threading
import time
import unittest

from spitch.hotkey.evdev_listener import HotkeyListener, parse_combo

try:
    import evdev  # noqa: F401
    _HAS_EVDEV = True
except ImportError:
    _HAS_EVDEV = False


class ParseComboTests(unittest.TestCase):
    def test_pair_order_insensitive(self):
        self.assertEqual(parse_combo("Ctrl+Alt"), ["ctrl", "alt"])
        self.assertEqual(parse_combo("alt+ctrl"), ["alt", "ctrl"])

    def test_dedupes(self):
        self.assertEqual(parse_combo("Ctrl+Ctrl+Alt"), ["ctrl", "alt"])

    def test_unknown_dropped(self):
        self.assertEqual(parse_combo("Ctrl+Foo+Alt"), ["ctrl", "alt"])

    def test_all_unknown_returns_empty(self):
        self.assertEqual(parse_combo("Foo+Bar"), [])

    def test_meta_is_alt(self):
        self.assertEqual(parse_combo("Meta+Ctrl"), ["alt", "ctrl"])

    def test_super_synonym(self):
        self.assertEqual(parse_combo("Win+Ctrl"), ["super", "ctrl"])


@unittest.skipUnless(_HAS_EVDEV, "evdev module unavailable")
class HotkeyListenerInitTests(unittest.TestCase):
    def test_rejects_empty_combo(self):
        with self.assertRaises(ValueError):
            HotkeyListener(
                [],
                on_press=lambda: None,
                on_release=lambda: None,
            )

    def test_rejects_single_modifier(self):
        # Single-modifier hold would fire on every system Ctrl-anything
        # press — refuse it at construction time so the daemon's CLI
        # gate is not the only safety net.
        with self.assertRaisesRegex(ValueError, "two distinct modifier"):
            HotkeyListener(
                ["ctrl"],
                on_press=lambda: None,
                on_release=lambda: None,
            )

    def test_accepts_pair(self):
        # Should not raise.
        HotkeyListener(
            ["ctrl", "alt"],
            on_press=lambda: None,
            on_release=lambda: None,
        )


@unittest.skipUnless(_HAS_EVDEV, "evdev module unavailable")
class WaitQuiescentTests(unittest.TestCase):
    def _make(self) -> HotkeyListener:
        return HotkeyListener(
            ["ctrl", "alt"],
            on_press=lambda: None,
            on_release=lambda: None,
        )

    def test_starts_quiescent(self):
        listener = self._make()
        self.assertTrue(listener.is_quiescent())
        # No timeout needed — already set.
        self.assertTrue(listener.wait_quiescent(timeout=0.0))

    def test_blocks_while_modifier_held_then_returns_on_release(self):
        from evdev import ecodes as ec
        listener = self._make()
        # Simulate the user pressing Ctrl. Use the internal _on_key so
        # we don't need a real keyboard device.
        listener._on_key(ec.KEY_LEFTCTRL, 1)
        self.assertFalse(listener.is_quiescent())
        self.assertFalse(listener.wait_quiescent(timeout=0.05))

        # In a side thread, release Ctrl 30 ms from now and expect
        # wait_quiescent on the main thread to wake up promptly.
        def _release_later():
            time.sleep(0.03)
            listener._on_key(ec.KEY_LEFTCTRL, 0)

        threading.Thread(target=_release_later, daemon=True).start()
        t0 = time.time()
        self.assertTrue(listener.wait_quiescent(timeout=1.0))
        elapsed = time.time() - t0
        # Should be well under the 1-second timeout — typical wakeup
        # latency on Linux is sub-10ms. Allow a generous 200ms ceiling
        # for slow CI hosts.
        self.assertLess(elapsed, 0.2)
        self.assertTrue(listener.is_quiescent())

    def test_re_clears_when_pressed_again(self):
        from evdev import ecodes as ec
        listener = self._make()
        listener._on_key(ec.KEY_LEFTCTRL, 1)
        listener._on_key(ec.KEY_LEFTCTRL, 0)
        self.assertTrue(listener.wait_quiescent(timeout=0.0))
        # Press again — event clears.
        listener._on_key(ec.KEY_LEFTALT, 1)
        self.assertFalse(listener.wait_quiescent(timeout=0.0))


if __name__ == "__main__":
    unittest.main()
