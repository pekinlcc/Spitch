"""Microphone capture for hold-to-talk.

The capture object has two layers:

1. **Mic lifecycle** (``open()`` / ``close()``). When opened, the mic
   runs continuously and every PCM chunk is appended to a small
   bounded ring buffer (the "pre-buffer"). The mic stays open across
   sessions so the user is not paying mic-startup latency on every
   press. ``close()`` shuts the backend down — typically only at
   daemon exit.

2. **Session lifecycle** (``start()`` / ``stop()``). ``start()``
   snapshots the current pre-buffer into the session output queue and
   routes subsequent live chunks there too. ``stop()`` ends the
   session but leaves the mic open. The controller reads via
   :meth:`chunks`.

The pre-buffer is what plugs the "first words got eaten" bug —
PortAudio / arecord both need 50–500 ms after their start() call
before PCM actually starts flowing, and during that window the user
is already talking. The pre-buffer means the first ``prebuffer_ms``
of audio comes from the ring (recorded *before* the press) so the
first phoneme is preserved.

Set ``prebuffer_ms`` to 0 to fall back to the legacy "open mic on
press, close on release" behavior — useful if continuous capture is
unacceptable for privacy reasons.

We avoid a hard dependency on ``sounddevice`` at import time because
unit tests on minimal CI hosts must still be able to import the rest
of the package. The real implementation tries ``sounddevice`` first
(PortAudio backend, works under PulseAudio/PipeWire) and falls back
to the ``arecord`` command-line tool — ``arecord`` ships in
``alsa-utils`` which is preinstalled on stock Ubuntu.
"""

from __future__ import annotations

import collections
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Iterator


class AudioCaptureError(RuntimeError):
    """Raised when no usable capture backend is available."""


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    channels: int = 1
    chunk_ms: int = 100  # ~100 ms per chunk → 3200 bytes at 16 kHz mono int16
    # Continuous-capture pre-buffer length. The mic is opened at daemon
    # start and audio fills a ring of this many milliseconds; on each
    # press we replay the ring into the session so the first words are
    # not lost to mic-startup latency. 500 ms covers PortAudio
    # warm-up + typical arecord device-open latency on stock Ubuntu.
    # Set to 0 to fall back to "open mic on press, close on release"
    # — privacy-preserving but loses the head of every utterance.
    prebuffer_ms: int = 500

    @property
    def chunk_frames(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000)

    @property
    def chunk_bytes(self) -> int:
        # int16 mono
        return self.chunk_frames * 2 * self.channels

    @property
    def prebuffer_chunks(self) -> int:
        return max(0, self.prebuffer_ms // self.chunk_ms)


class AudioCapture:
    """Push-to-talk microphone capture with optional continuous pre-buffer.

    Public lifecycle:

    * :meth:`open` — open the mic; chunks start filling the pre-buffer.
    * :meth:`close` — close the mic.
    * :meth:`start` — begin a session; replays pre-buffer + live chunks
      to the session queue. Auto-opens the mic if not already open.
    * :meth:`stop` — end the session. With ``prebuffer_ms > 0`` the mic
      stays open; with ``prebuffer_ms == 0`` the mic is also closed
      (matches the legacy behavior).
    * :meth:`chunks` — iterator over the session queue.

    The same object is reusable across multiple presses without ever
    closing the mic in continuous mode.
    """

    def __init__(self, config: AudioConfig | None = None, device: str | None = None):
        self.config = config or AudioConfig()
        self.device = device
        # Bounded ring buffer for continuous pre-recording. With
        # ``maxlen=0`` (prebuffer disabled) deque.append is a no-op,
        # which is exactly what we want for the legacy behavior — no
        # need for an explicit gate inside _on_audio.
        self._prebuffer: "collections.deque[bytes]" = collections.deque(
            maxlen=self.config.prebuffer_chunks,
        )
        # The session output. ``chunks()`` reads from here.
        self._session_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=128)
        self._session_active = False
        # Single short-held lock guarding _prebuffer + _session_active +
        # _session_queue mutations. The audio backend callback
        # / reader takes it for tens of microseconds per chunk; start()
        # / stop() take it for a few ms. Cheap enough that PortAudio
        # callback timing is not at risk.
        self._lock = threading.Lock()
        # Backend handles.
        self._stream = None  # sounddevice stream
        self._proc: subprocess.Popen | None = None  # arecord
        self._reader: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._mic_open = False
        self._backend: str | None = None

    # ------------------------------------------------------------------
    # internal: a single callback the backends route into

    def _on_audio(self, chunk: bytes) -> None:
        """Backend callback / reader pushes a fresh PCM chunk here.

        Always appends to the pre-buffer (bounded, oldest dropped). If
        a session is currently active, also forwards to the session
        queue. The session queue is bounded; on overflow we drop —
        better to drop a chunk than to OOM the daemon.
        """
        if not chunk:
            return
        with self._lock:
            self._prebuffer.append(chunk)
            if self._session_active:
                try:
                    self._session_queue.put_nowait(chunk)
                except queue.Full:
                    pass

    # ------------------------------------------------------------------
    # backend selection (called from open() / start())

    def _try_sounddevice(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore  # noqa: F401
        except Exception:
            return False

        def _cb(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                # underflow / overflow — keep going, the user cares about words.
                pass
            self._on_audio(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.config.sample_rate,
                blocksize=self.config.chunk_frames,
                device=self.device,
                channels=self.config.channels,
                dtype="int16",
                callback=_cb,
            )
            self._stream.start()
        except Exception:
            self._stream = None
            return False
        self._backend = "sounddevice"
        return True

    def _try_arecord(self) -> bool:
        if shutil.which("arecord") is None:
            return False
        cmd = [
            "arecord",
            "-q",
            "-t", "raw",
            "-f", "S16_LE",
            "-r", str(self.config.sample_rate),
            "-c", str(self.config.channels),
        ]
        if self.device:
            cmd += ["-D", self.device]
        try:
            # Capture stderr so we can surface the real reason if
            # arecord exits immediately (busy device, missing PCM,
            # ALSA misconfig). Without this, the daemon would silently
            # record nothing and time out 5 seconds later.
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            return False

        # Verify arecord didn't die immediately on device open.
        time.sleep(0.05)
        if self._proc.poll() is not None:
            err_bytes = b""
            try:
                if self._proc.stderr is not None:
                    err_bytes = self._proc.stderr.read() or b""
            except Exception:
                pass
            self._proc = None
            err_text = err_bytes.decode("utf-8", errors="replace").strip()
            raise AudioCaptureError(
                "arecord exited immediately: "
                + (err_text or "no stderr output")
            )

        # Drain stderr in a daemon thread so a long recording doesn't
        # get throttled by a full 64 KB pipe buffer.
        stderr_pipe = self._proc.stderr

        def _drain_stderr() -> None:
            try:
                while stderr_pipe.read(4096):
                    pass
            except Exception:
                pass

        threading.Thread(
            target=_drain_stderr,
            name="spitch-arecord-stderr",
            daemon=True,
        ).start()

        def _reader_loop() -> None:
            assert self._proc is not None and self._proc.stdout is not None
            chunk_bytes = self.config.chunk_bytes
            try:
                while not self._stop_event.is_set():
                    data = self._proc.stdout.read(chunk_bytes)
                    if not data:
                        break
                    self._on_audio(data)
            finally:
                # Sentinel into session queue so any blocked chunks()
                # caller returns when the backend dies.
                with self._lock:
                    if self._session_active:
                        try:
                            self._session_queue.put_nowait(None)
                        except queue.Full:
                            pass

        self._reader = threading.Thread(
            target=_reader_loop, name="spitch-arecord-reader", daemon=True
        )
        self._reader.start()
        self._backend = "arecord"
        return True

    def _open_backend(self) -> str:
        self._stop_event.clear()
        if self._try_sounddevice():
            self._mic_open = True
            return "sounddevice"
        if self._try_arecord():
            self._mic_open = True
            return "arecord"
        raise AudioCaptureError(
            "no audio backend available — install python-sounddevice "
            "or alsa-utils (arecord)"
        )

    # ------------------------------------------------------------------
    # public API: mic lifecycle

    def open(self) -> str:
        """Open the microphone and start filling the pre-buffer.

        Idempotent. With ``prebuffer_ms == 0`` this is a no-op — the
        mic is opened on demand by :meth:`start` instead, matching
        the legacy behavior.
        """
        if self._mic_open:
            return self._backend or ""
        if self.config.prebuffer_ms <= 0:
            return ""
        return self._open_backend()

    def close(self) -> None:
        """Close the microphone and clear the pre-buffer."""
        if not self._mic_open:
            return
        self._stop_event.set()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=1.0)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._reader is not None:
            self._reader.join(timeout=1.0)
            self._reader = None
        self._mic_open = False
        self._backend = None
        with self._lock:
            self._prebuffer.clear()
            self._session_active = False
            # Sentinel so any blocked reader returns.
            try:
                self._session_queue.put_nowait(None)
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # public API: session lifecycle

    def start(self) -> str:
        """Begin a new session.

        Snapshots the pre-buffer into the session output queue and
        marks the session active so subsequent live chunks are also
        forwarded there. If the mic isn't open yet, opens it on
        demand (legacy behavior; means the first session pays the
        backend-startup latency).

        Returns the backend name actually in use.
        """
        if not self._mic_open:
            self._open_backend()
        with self._lock:
            # Drain any leftover sentinels / late chunks from a
            # previous session.
            while True:
                try:
                    self._session_queue.get_nowait()
                except queue.Empty:
                    break
            # Replay pre-buffer first so the audio captured *before*
            # the press is at the head of the stream sent to Doubao.
            for chunk in self._prebuffer:
                try:
                    self._session_queue.put_nowait(chunk)
                except queue.Full:
                    pass
            self._session_active = True
        return self._backend or ""

    def stop(self) -> None:
        """End the current session.

        Drops a sentinel into the session queue so :meth:`chunks`
        returns. With continuous-capture mode (``prebuffer_ms > 0``)
        the mic stays open; with ``prebuffer_ms == 0`` we also close
        the backend so a stale capture stream doesn't leak between
        sessions.
        """
        with self._lock:
            self._session_active = False
            try:
                self._session_queue.put_nowait(None)
            except queue.Full:
                pass
        if self.config.prebuffer_ms <= 0:
            self.close()

    # ------------------------------------------------------------------
    # public API: read

    def read(self, timeout: float = 1.0) -> bytes | None:
        """Pop the next PCM chunk; return None when capture has ended."""
        try:
            return self._session_queue.get(timeout=timeout)
        except queue.Empty:
            return b""

    def chunks(self) -> Iterator[bytes]:
        """Yield chunks until :meth:`stop` is called or the backend dies.

        Empty bytes (``b""``) on read timeout are treated as keep-alives
        and skipped. ``None`` (sentinel) ends iteration.
        """
        while True:
            chunk = self.read()
            if chunk is None:
                return
            if chunk:
                yield chunk
