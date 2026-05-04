"""Tests for the continuous-capture pre-buffer.

The fix for "first words got eaten" is a ring buffer of the last
``prebuffer_ms`` of audio. We exercise it without a real mic by
driving :meth:`AudioCapture._on_audio` directly — that's the same
entry point the sounddevice callback and the arecord reader call.
"""

from __future__ import annotations

import threading
import unittest

from spitch.voice.audio import AudioCapture, AudioConfig


class _Harness(AudioCapture):
    """An AudioCapture that doesn't need a real backend.

    open() / close() are stubbed so the tests can drive _on_audio
    directly. This mirrors what the real backends do — both call
    _on_audio with raw PCM bytes.
    """

    def __init__(self, prebuffer_ms: int):
        super().__init__(
            AudioConfig(
                sample_rate=16000,
                chunk_ms=100,
                prebuffer_ms=prebuffer_ms,
            )
        )

    def _open_backend(self) -> str:  # noqa: D401 - test stub
        self._mic_open = True
        self._backend = "test"
        return "test"


def _consume(audio: AudioCapture, n: int, timeout: float = 0.5) -> list[bytes]:
    """Pull ``n`` chunks from chunks() with a timeout per chunk."""
    out: list[bytes] = []
    iterator = audio.chunks()
    for _ in range(n):
        try:
            out.append(next(iterator))
        except StopIteration:
            break
    return out


class PrebufferReplayTests(unittest.TestCase):
    def test_session_replays_prebuffer_chunks(self):
        """Chunks captured BEFORE start() must appear in the session.

        That's the fix for the "first half got eaten" bug — the user
        already started talking before audio.start() returned, so the
        first words sit in the ring buffer and need to be flushed
        into the session queue at the head.
        """
        audio = _Harness(prebuffer_ms=500)
        audio.open()
        # Three chunks captured before press — these are the user's
        # first phonemes that the legacy code would have lost.
        for chunk in (b"AAA", b"BBB", b"CCC"):
            audio._on_audio(chunk)
        audio.start()
        # Two more chunks after press.
        audio._on_audio(b"DDD")
        audio._on_audio(b"EEE")
        audio.stop()
        # chunks() ends at the stop sentinel; collect everything yielded.
        out = list(audio.chunks())
        # Order matters: prebuffer first (in capture order), then live.
        self.assertEqual(out, [b"AAA", b"BBB", b"CCC", b"DDD", b"EEE"])
        audio.close()

    def test_prebuffer_is_bounded_by_config(self):
        """Capturing more than prebuffer_ms drops the oldest chunks."""
        # 200 ms of pre-buffer at 100 ms chunks → keeps the last 2 chunks.
        audio = _Harness(prebuffer_ms=200)
        audio.open()
        for chunk in (b"old1", b"old2", b"keep1", b"keep2"):
            audio._on_audio(chunk)
        audio.start()
        audio.stop()
        out = list(audio.chunks())
        self.assertEqual(out, [b"keep1", b"keep2"])
        audio.close()

    def test_prebuffer_disabled_replays_nothing(self):
        """With prebuffer_ms == 0 we keep the legacy on-press semantics."""
        audio = _Harness(prebuffer_ms=0)
        # Without prebuffer, open() is a no-op; the mic only opens on
        # start(). _on_audio called before start() goes nowhere.
        self.assertEqual(audio.open(), "")
        # But for the test we want to verify that pre-start audio
        # would not appear, so push a chunk anyway.
        audio._on_audio(b"pre-start (should be invisible)")
        audio.start()
        audio._on_audio(b"live1")
        audio.stop()
        out = list(audio.chunks())
        self.assertEqual(out, [b"live1"])
        audio.close()

    def test_two_consecutive_sessions_replay_independently(self):
        """The pre-buffer continues filling between sessions, so the
        second press has fresh head-room."""
        audio = _Harness(prebuffer_ms=500)
        audio.open()

        # First session.
        audio._on_audio(b"S1pre")
        audio.start()
        audio._on_audio(b"S1live")
        audio.stop()
        out1 = list(audio.chunks())
        self.assertEqual(out1, [b"S1pre", b"S1live"])

        # Between sessions, audio keeps flowing into the prebuffer.
        audio._on_audio(b"between1")
        audio._on_audio(b"between2")

        # Second session — replays the latest prebuffer (which now
        # contains S1pre, S1live, between1, between2 — capped at 5
        # chunks for 500ms).
        audio.start()
        audio._on_audio(b"S2live")
        audio.stop()
        out2 = list(audio.chunks())
        # The prebuffer carried over; S1pre/S1live/between1/between2
        # are all still inside (5-chunk capacity).
        self.assertEqual(
            out2,
            [b"S1pre", b"S1live", b"between1", b"between2", b"S2live"],
        )
        audio.close()

    def test_close_drops_prebuffer(self):
        audio = _Harness(prebuffer_ms=500)
        audio.open()
        audio._on_audio(b"x")
        audio._on_audio(b"y")
        audio.close()
        # Re-open: prebuffer should be empty.
        audio.open()
        audio.start()
        audio.stop()
        out = list(audio.chunks())
        self.assertEqual(out, [])
        audio.close()


class ConcurrencyTests(unittest.TestCase):
    def test_callback_during_start_does_not_lose_chunks(self):
        """A backend callback firing while start() is mid-snapshot
        should still get its chunk into the session queue.

        Implementation detail: _on_audio holds the same lock that
        start() holds, so the callback briefly blocks. As long as the
        lock-handoff is correct, neither path drops the chunk.
        """
        audio = _Harness(prebuffer_ms=500)
        audio.open()
        # Pre-fill some prebuffer.
        audio._on_audio(b"pre1")
        audio._on_audio(b"pre2")

        ready = threading.Event()
        proceed = threading.Event()

        # Hold start() open by patching: take the lock from a side
        # thread, signal ready, and have the test thread call start
        # which will block on the lock.  Then fire a callback from
        # *another* thread, also blocked, then release everything.
        # If start() loses the chunk, the assertion at the end fails.

        def _holder():
            with audio._lock:
                ready.set()
                proceed.wait(timeout=1.0)

        t = threading.Thread(target=_holder, daemon=True)
        t.start()
        ready.wait(timeout=1.0)

        # Fire a chunk via the callback path while the lock is held.
        cb_done = threading.Event()

        def _fire():
            audio._on_audio(b"raced")
            cb_done.set()

        cb = threading.Thread(target=_fire, daemon=True)
        cb.start()

        # Now also start a session. Both _on_audio and start() are
        # now blocked on the lock.
        start_done = threading.Event()

        def _start():
            audio.start()
            start_done.set()

        s = threading.Thread(target=_start, daemon=True)
        s.start()

        # Release the holder — both pending paths run in some order.
        proceed.set()
        t.join(timeout=1.0)
        cb_done.wait(timeout=1.0)
        start_done.wait(timeout=1.0)

        audio._on_audio(b"after")
        audio.stop()
        out = list(audio.chunks())
        # The prebuffer at start() captured pre1/pre2 plus possibly raced.
        # "raced" must end up in the session output regardless of
        # ordering: either as part of the prebuffer snapshot, or as a
        # live chunk pushed after session_active flipped to True.
        self.assertIn(b"raced", out)
        self.assertEqual(out[-1], b"after")
        audio.close()


if __name__ == "__main__":
    unittest.main()
