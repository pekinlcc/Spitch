"""VoiceController state-machine test against fully fake audio + WS client."""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import AsyncIterator

from spitch.voice.controller import State, VoiceController
from spitch.voice.doubao import TranscriptEvent


class FakeAudio:
    """Stand-in for AudioCapture: yields a scripted sequence of chunks."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self._stopped = threading.Event()
        self._cv = threading.Condition()
        self._idx = 0

    def start(self) -> str:
        self._stopped.clear()
        self._idx = 0
        return "fake"

    def stop(self) -> None:
        with self._cv:
            self._stopped.set()
            self._cv.notify_all()

    def chunks(self):
        # Mimic streaming: yield chunks with a tiny pause until stop()
        with self._cv:
            while self._idx < len(self._chunks):
                if self._stopped.is_set():
                    return
                chunk = self._chunks[self._idx]
                self._idx += 1
                yield chunk
                # tiny wait, releasing the cv so stop() can wake us
                self._cv.wait(timeout=0.005)
            # After scripted chunks exhausted, block until stop()
            while not self._stopped.is_set():
                self._cv.wait(timeout=0.01)


class FakeStreamingClient:
    """Async context manager + ``stream`` matching DoubaoClient's surface."""

    def __init__(self, scripted_events: list[TranscriptEvent]):
        self._scripted = scripted_events
        self.consumed_chunks: list[bytes] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def stream(self, audio_iter):
        async for chunk in audio_iter:
            self.consumed_chunks.append(chunk)
        for evt in self._scripted:
            await asyncio.sleep(0)
            yield evt


class VoiceControllerTests(unittest.TestCase):
    def _wait_state(self, ctrl: VoiceController, want: State, timeout: float = 2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if ctrl.state == want:
                return True
            time.sleep(0.01)
        return False

    def test_press_release_commits_final(self):
        events = [
            TranscriptEvent("你", False, {}),
            TranscriptEvent("你好", False, {}),
            TranscriptEvent("你好。", True, {}),
        ]
        client = FakeStreamingClient(events)
        audio = FakeAudio([b"\x01" * 320, b"\x02" * 320])

        partials: list[str] = []
        finals: list[str] = []
        errors: list[BaseException] = []

        ctrl = VoiceController(
            client_factory=lambda: client,
            audio=audio,
            on_partial=partials.append,
            on_final=finals.append,
            on_error=errors.append,
        )

        ok = ctrl.press()
        self.assertTrue(ok)
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))

        self.assertEqual(finals, ["你好。"])
        self.assertEqual(errors, [])
        self.assertIn("你", partials)

    def test_double_press_is_noop(self):
        events = [TranscriptEvent("ok", True, {})]
        ctrl = VoiceController(
            client_factory=lambda: FakeStreamingClient(events),
            audio=FakeAudio([b"x" * 32]),
        )
        self.assertTrue(ctrl.press())
        self.assertFalse(ctrl.press())
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))

    def test_cancel_does_not_commit(self):
        events = [TranscriptEvent("partial", False, {}), TranscriptEvent("final", True, {})]
        client = FakeStreamingClient(events)
        audio = FakeAudio([b"x" * 64, b"y" * 64])
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: client,
            audio=audio,
            on_final=finals.append,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.cancel()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0) or
                        self._wait_state(ctrl, State.ERROR, timeout=0.1))
        self.assertEqual(finals, [])

    def test_final_during_recording_commits_and_returns_to_idle(self):
        """Server sends definite=true while the user is still holding the keys.

        Regression: previously the controller went straight to IDLE
        without firing any event the daemon could distinguish from a
        cancel, and the daemon's _on_release dropped the transcript on
        the floor because it gated on state == RECORDING. We assert
        on_final is delivered exactly once and that the controller
        ends up IDLE without an explicit release().
        """

        class EagerClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                # Don't drain audio fully — emit a final immediately.
                # Simulates Doubao deciding the utterance is complete
                # while the user still has the key pressed.
                yield TranscriptEvent("早", False, {})
                yield TranscriptEvent("早安。", True, {})

        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: EagerClient(),
            audio=FakeAudio([b"\x00" * 320] * 10),
            on_final=finals.append,
        )
        self.assertTrue(ctrl.press())
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        self.assertEqual(finals, ["早安。"])

    def test_state_transition_runs_after_audio_stop_on_error(self):
        """ERROR is published only after the failed session's audio.stop()
        has run.

        Regression for the race where _set_state(ERROR) ran BEFORE the
        outer-finally cleanup, so a re-press observing ERROR could call
        audio.start() while the dying session was still in flight and
        about to call audio.stop() on the *new* stream.
        """

        state_at_stop: list[State] = []
        stop_done = threading.Event()

        class TracingFakeAudio:
            def start(self) -> str:
                return "fake"

            def stop(self) -> None:
                # Record the controller's published state at the moment
                # the dying session calls audio.stop() in its outer
                # finally. With the bug, state has already flipped to
                # ERROR (so a re-press observing ERROR could call
                # audio.start() and have us stomp on it). With the
                # fix, state is still RECORDING here — the transition
                # to ERROR happens strictly after we return.
                state_at_stop.append(ctrl.state)
                stop_done.set()

            def chunks(self):
                if False:
                    yield b""  # pragma: no cover

        class FailingOpenClient:
            async def __aenter__(self):
                raise RuntimeError("simulated connect failure")

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                if False:
                    yield  # pragma: no cover

        audio = TracingFakeAudio()
        errors: list[BaseException] = []
        ctrl = VoiceController(
            client_factory=lambda: FailingOpenClient(),
            audio=audio,
            on_error=errors.append,
        )
        self.assertTrue(ctrl.press())
        self.assertTrue(self._wait_state(ctrl, State.ERROR, timeout=3.0))
        self.assertTrue(stop_done.is_set())
        self.assertEqual(len(errors), 1)
        self.assertEqual(
            state_at_stop, [State.RECORDING],
            "audio.stop() should run while state is still RECORDING — "
            "ERROR must only be published after cleanup completes",
        )

    def test_finalize_timeout_commits_latest_partial(self):
        """Server sends partials then never returns a definite=true frame.

        After release(), the controller waits at most ``finalize_timeout``
        seconds before committing the most recent partial as a fallback —
        this is the PRD risk-mitigation for a slow / unresponsive server.
        """

        class HangingClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def stream(self, audio_iter):
                # consume the audio
                async for _ in audio_iter:
                    pass
                yield TranscriptEvent("你", False, {})
                yield TranscriptEvent("你好世界", False, {})
                # then never produce a final — block forever
                while True:
                    await asyncio.sleep(0.1)

        audio = FakeAudio([b"\x01" * 320, b"\x02" * 320])
        finals: list[str] = []
        ctrl = VoiceController(
            client_factory=lambda: HangingClient(),
            audio=audio,
            on_final=finals.append,
            finalize_timeout=0.3,
        )
        ctrl.press()
        time.sleep(0.05)
        ctrl.release()
        self.assertTrue(self._wait_state(ctrl, State.IDLE, timeout=3.0))
        self.assertEqual(finals, ["你好世界"])


if __name__ == "__main__":
    unittest.main()
