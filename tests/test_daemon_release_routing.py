"""Unit tests for SpitchDaemon's hotkey-event routing.

Specifically for the queue lifecycle around _on_press / _on_release /
_on_cancel, which is where two fast-acting bugs lived in v0.2.1:

* "Doubao sent definite=true while the user was still holding the
  keys, the controller went straight to IDLE, and _on_release was
  short-circuited because state != RECORDING — transcript dropped."
* "User releases first, server sends definite=true a few hundred
  milliseconds later (the FINALIZING window). on_final must still
  reach the inject thread's queue."

We bypass the real HotkeyListener / VoiceController here and drive
the daemon's callbacks directly with stubs.
"""

from __future__ import annotations

import queue
import threading
import time
import unittest
from typing import Optional
from unittest.mock import patch

from spitch.daemon import SpitchDaemon
from spitch.voice import State


class _FakeVoice:
    """Just enough of VoiceController for the daemon's hotkey callbacks."""

    def __init__(self, accept_press: bool = True):
        self.accept_press = accept_press
        self.state = State.IDLE
        self.press_calls = 0
        self.release_calls = 0
        self.cancel_calls = 0

    def press(self) -> bool:
        self.press_calls += 1
        if not self.accept_press:
            return False
        self.state = State.RECORDING
        return True

    def release(self) -> None:
        self.release_calls += 1
        if self.state == State.RECORDING:
            self.state = State.FINALIZING

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.state = State.IDLE


class _FakeQuiescentListener:
    """Stand-in for HotkeyListener — the inject thread polls is_quiescent."""

    def is_quiescent(self) -> bool:
        return True


def _build_daemon() -> SpitchDaemon:
    cfg = {
        "doubao": {"app_key": "x", "access_key": "y"},
        "inject": {
            "paste_keystroke": "Ctrl+V",
            "final_wait_seconds": 0.5,
            "restore_clipboard_delay_ms": 0,
        },
    }
    d = SpitchDaemon(cfg)
    d._listener = _FakeQuiescentListener()
    return d


def _wait_until(predicate, timeout: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class ReleaseRoutingTests(unittest.TestCase):
    def test_on_final_during_finalizing_window_reaches_inject(self):
        """Slow-final case: user releases first, server replies later.

        Regression for the v0.2.2 follow-up where _on_release nulled
        self._pending_final, so on_final's later put_nowait went to
        a None queue and the transcript was silently dropped.
        """
        daemon = _build_daemon()
        daemon._voice = _FakeVoice(accept_press=True)

        injected: list[str] = []

        def fake_inject(text, *, paste_keystroke, restore_delay_ms):
            injected.append(text)
            return True, ""

        with patch("spitch.daemon.inject_text", side_effect=fake_inject):
            daemon._on_press()
            self.assertTrue(daemon._press_accepted)
            self.assertIsNotNone(daemon._pending_final)

            # User releases BEFORE the server's final arrives. _on_release
            # spawns the inject thread which now blocks on the queue.
            daemon._on_release()
            self.assertEqual(daemon._voice.release_calls, 1)

            # Now simulate the worker's late on_final firing from the
            # voice thread — it must still find self._pending_final and
            # put the text where the inject thread is reading.
            daemon._on_final("你好世界。")

            self.assertTrue(_wait_until(lambda: injected == ["你好世界。"]))

    def test_on_final_before_release_still_injects(self):
        """Fast-final case: server replies before the user releases.

        Regression for v0.2.1 where _on_release gated on
        voice.state == RECORDING. Once the controller saw a
        definite=true frame mid-press it transitioned to IDLE and the
        guard short-circuited the release, dropping the transcript.
        """
        daemon = _build_daemon()
        daemon._voice = _FakeVoice(accept_press=True)
        injected: list[str] = []

        def fake_inject(text, *, paste_keystroke, restore_delay_ms):
            injected.append(text)
            return True, ""

        with patch("spitch.daemon.inject_text", side_effect=fake_inject):
            daemon._on_press()
            # Server's on_final fires while user is still holding the chord.
            daemon._on_final("早上好。")
            # Controller went straight to IDLE without an explicit release.
            daemon._voice.state = State.IDLE
            # User releases. _press_accepted must carry the inject through
            # even though state is no longer RECORDING.
            daemon._on_release()
            self.assertTrue(_wait_until(lambda: injected == ["早上好。"]))

    def test_cancel_drops_pending_and_release_is_noop(self):
        daemon = _build_daemon()
        daemon._voice = _FakeVoice(accept_press=True)

        with patch("spitch.daemon.inject_text") as inject_mock:
            daemon._on_press()
            daemon._on_cancel()
            self.assertFalse(daemon._press_accepted)
            self.assertIsNone(daemon._pending_final)
            # Late on_final from a still-cleaning-up worker must not
            # land anywhere observable.
            daemon._on_final("不应被注入")
            daemon._on_release()
            time.sleep(0.05)
            inject_mock.assert_not_called()

    def test_rejected_press_does_not_arm_release(self):
        daemon = _build_daemon()
        daemon._voice = _FakeVoice(accept_press=False)

        with patch("spitch.daemon.inject_text") as inject_mock:
            daemon._on_press()
            self.assertFalse(daemon._press_accepted)
            daemon._on_release()
            self.assertEqual(daemon._voice.release_calls, 0)
            inject_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
