"""Push-to-talk voice controller — the bridge between hotkey events,
the audio capture layer, and the Doubao streaming client.

The controller's lifecycle:

    IDLE  --press_talk-->  RECORDING  --release_talk-->  FINALIZING  -->  IDLE

Streaming partial transcripts are pushed to a caller-provided
``on_partial(text)`` callback. The final text is pushed to
``on_final(text)``. The controller is hotkey-source-agnostic — the
daemon wires it up to evdev events; tests wire it up to a fake client.

Concurrency: capture runs in a daemon thread, the asyncio event loop
runs in another daemon thread, so the caller's main thread stays
responsive while a recording is in flight. The controller exposes
``press()`` / ``release()`` / ``cancel()`` from the main thread and
is otherwise fully internal.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import AsyncIterator, Callable, Iterable, Iterator, Protocol

from .audio import AudioCapture, AudioConfig, AudioCaptureError


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"
    ERROR = "error"


@dataclass
class TranscriptUpdate:
    text: str
    is_final: bool


class StreamingClient(Protocol):
    """The slice of :class:`spitch.voice.doubao.DoubaoClient` we depend on."""

    async def __aenter__(self) -> "StreamingClient": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...
    def stream(self, audio_iter) -> AsyncIterator: ...  # yields .text/.is_final


class VoiceController:
    """State machine for hold-to-talk Doubao transcription.

    ``client_factory`` returns a fresh streaming client per press —
    typically ``lambda: DoubaoClient(creds, sample_rate=...)``. Tests
    can pass a fake client.

    ``audio`` is an :class:`AudioCapture` (or duck-typed equivalent —
    must implement ``start()``, ``stop()``, ``chunks()``).

    All callbacks fire on the controller's own thread — they should be
    cheap and not raise.
    """

    def __init__(
        self,
        client_factory: Callable[[], StreamingClient],
        audio: AudioCapture | None = None,
        *,
        on_partial: Callable[[str], None] | None = None,
        on_final: Callable[[str], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        on_state: Callable[[State], None] | None = None,
        finalize_timeout: float = 2.0,
        audio_config: AudioConfig | None = None,
    ):
        self._client_factory = client_factory
        self._audio = audio or AudioCapture(audio_config)
        self._on_partial = on_partial or (lambda _t: None)
        self._on_final = on_final or (lambda _t: None)
        self._on_error = on_error or (lambda _e: None)
        self._on_state = on_state or (lambda _s: None)
        self._finalize_timeout = finalize_timeout

        self._state = State.IDLE
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._latest_text = ""
        self._worker: threading.Thread | None = None

    # -- introspection -------------------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def latest_text(self) -> str:
        return self._latest_text

    # -- main API ------------------------------------------------------

    def press(self) -> bool:
        """Start a recording session. Returns False if already recording.

        ERROR is treated as a soft latch — the next press resets and
        starts fresh. This keeps the daemon usable after transient
        Doubao / WebSocket / network failures without forcing a
        process restart.
        """
        with self._lock:
            if self._state not in (State.IDLE, State.ERROR):
                return False
            self._cancel.clear()
            self._latest_text = ""
            self._set_state(State.RECORDING)
        try:
            self._audio.start()
        except Exception as exc:
            # Catch broadly: AudioCaptureError is the documented case,
            # but the underlying backend can throw OSError (audio device
            # gone), RuntimeError (thread/proc spawn refused), etc. If
            # any of those leak, state is stuck at RECORDING with no
            # active session and the next press() refuses forever.
            self._on_error(exc)
            self._set_state(State.ERROR)
            return False
        try:
            self._worker = threading.Thread(
                target=self._run_session, name="spitch-voice-worker", daemon=True
            )
            self._worker.start()
        except Exception as exc:
            # Worker spawn failed — undo the audio start so we don't
            # leak the open mic into the next press.
            try:
                self._audio.stop()
            except Exception:
                pass
            self._on_error(exc)
            self._set_state(State.ERROR)
            return False
        return True

    def release(self) -> None:
        """Signal end-of-stream; the worker thread finishes finalizing."""
        with self._lock:
            if self._state != State.RECORDING:
                return
            self._set_state(State.FINALIZING)
        # stop capture so the audio iterator drains and the WS sender
        # writes its terminal frame.
        self._audio.stop()

    def cancel(self) -> None:
        """Abort: stop capture, signal cancellation, no commit."""
        with self._lock:
            if self._state == State.IDLE:
                return
        self._cancel.set()
        self._audio.stop()

    # -- internals -----------------------------------------------------

    def _set_state(self, s: State) -> None:
        self._state = s
        try:
            self._on_state(s)
        except Exception:
            pass

    def _audio_iter(self) -> Iterator[bytes]:
        """PCM iterator that yields until capture stops or cancel fires."""
        for chunk in self._audio.chunks():
            if self._cancel.is_set():
                return
            yield chunk

    def _run_session(self) -> None:
        loop = asyncio.new_event_loop()
        errored = False
        try:
            try:
                loop.run_until_complete(self._session_coro())
            except Exception as exc:
                errored = True
                # Re-raise the original — _on_error wraps in a richer
                # message (type + repr) so callers can tell ECONNRESET
                # apart from a websockets-library bug.
                wrapped = type(exc).__name__ + ": " + (str(exc) or repr(exc))
                self._on_error(RuntimeError(wrapped))
            finally:
                # Drain async-generator finalizers (the Doubao stream
                # and the audio _async_chunks generator) before tearing
                # the loop down — otherwise we'd leak "Task was
                # destroyed but it is pending!" warnings into the test
                # output and mask future real leaks.
                try:
                    loop.run_until_complete(loop.shutdown_asyncgens())
                except Exception:
                    pass
        finally:
            loop.close()
            # Belt-and-suspenders: stop the mic regardless of how we
            # exited. A clean exit (server sent definite=true while
            # still RECORDING, never reached release()) would otherwise
            # leak the capture stream until the next press.
            try:
                self._audio.stop()
            except Exception:
                pass
            # Publish the new state AFTER audio.stop. If we set ERROR /
            # IDLE first, a press() observing the new state could call
            # self._audio.start() and open a fresh stream — then our
            # stop() above would tear down the *new* session's mic.
            self._set_state(State.ERROR if errored else State.IDLE)

    async def _session_coro(self) -> None:
        import logging
        log = logging.getLogger("spitch.voice")
        log.info("session: starting client_factory")
        client = self._client_factory()
        # Convert the sync chunk iterator into an async one without
        # blocking the loop: hand off reads to a thread.
        chunks = self._audio_iter()

        async def _async_chunks():
            loop = asyncio.get_running_loop()
            while True:
                try:
                    chunk = await loop.run_in_executor(None, next, chunks, b"__END__")
                except StopIteration:
                    return
                if chunk == b"__END__" or chunk is None:
                    return
                if self._cancel.is_set():
                    return
                yield chunk

        log.info("session: connecting to ASR endpoint")
        async with client as live:
            log.info("session: connected, starting stream")
            chunks_gen = _async_chunks()
            stream = live.stream(chunks_gen).__aiter__()

            async def _consume() -> bool:
                """Drain events until cancel or end-of-stream. Return True iff a final fired.

                Doubao marks each utterance segment ``definite=true`` as
                soon as it's stable, even while the user is still
                speaking the rest of the sentence. We must NOT exit on
                the first such marker — instead we cache it as
                "best final so far" and keep consuming. The session
                ends naturally when the WebSocket stream closes (user
                released the talk key, EOS frame went out, server
                acknowledged), at which point we commit the most
                recent definite text.
                """
                last_final_text = ""
                saw_any_final = False
                try:
                    while True:
                        try:
                            evt = await stream.__anext__()
                        except StopAsyncIteration:
                            break
                        if self._cancel.is_set():
                            return False
                        if evt.text:
                            self._latest_text = evt.text
                            if evt.is_final:
                                # Remember it but stay in the loop —
                                # more partials / finals may follow as
                                # the user keeps talking.
                                saw_any_final = True
                                last_final_text = evt.text
                                # Surface it as a partial too so the
                                # tray label keeps updating across
                                # the segment boundary; the actual
                                # on_final fires once at EOS below.
                                self._on_partial(evt.text)
                            else:
                                self._on_partial(evt.text)
                    # Stream ended normally — commit the last definite
                    # text we got. If we never saw a definite frame,
                    # leave it to the FINALIZING-timeout fallback in
                    # _session_coro to commit latest_text.
                    if saw_any_final and last_final_text and not self._cancel.is_set():
                        self._on_final(last_final_text)
                        return True
                    return False
                except Exception:
                    if not self._cancel.is_set():
                        # Prefer the last definite text over the bare
                        # latest partial — definite frames have been
                        # ITN-normalized + punctuated by the server.
                        commit = last_final_text or self._latest_text
                        if commit:
                            self._on_final(commit)
                    raise

            consume_task: asyncio.Task | None = None
            try:
                # Race the stream against the finalize-wall: if the user
                # has released the talk key (state FINALIZING) and the
                # server still hasn't sent definite=true after
                # finalize_timeout seconds, we commit the latest partial
                # rather than block the daemon indefinitely. PRD risk row
                # "Latency between key release and Doubao final result".
                consume_task = asyncio.create_task(_consume())
                while not consume_task.done():
                    if self._state == State.FINALIZING:
                        try:
                            committed = await asyncio.wait_for(
                                asyncio.shield(consume_task),
                                timeout=self._finalize_timeout,
                            )
                            if not committed and not self._cancel.is_set() and self._latest_text:
                                self._on_final(self._latest_text)
                            return
                        except asyncio.TimeoutError:
                            consume_task.cancel()
                            try:
                                await consume_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            if not self._cancel.is_set() and self._latest_text:
                                self._on_final(self._latest_text)
                            return
                    else:
                        # still RECORDING — short tick so we re-check state.
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(consume_task), timeout=0.1
                            )
                        except asyncio.TimeoutError:
                            continue
                committed = consume_task.result() if not consume_task.cancelled() else False
                if not committed and not self._cancel.is_set() and self._latest_text:
                    self._on_final(self._latest_text)
            except Exception:
                if not self._cancel.is_set() and self._latest_text:
                    self._on_final(self._latest_text)
                raise
            finally:
                # Drive the async generators through their cleanup path
                # before the loop tears down — otherwise their pending
                # athrow tasks leak as "Task was destroyed but it is
                # pending!" warnings, masking real future leaks.
                if consume_task is not None and not consume_task.done():
                    consume_task.cancel()
                    try:
                        await consume_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for ag in (stream, chunks_gen):
                    aclose = getattr(ag, "aclose", None)
                    if aclose is None:
                        continue
                    try:
                        await aclose()
                    except (asyncio.CancelledError, Exception):
                        pass
