"""Microphone capture for hold-to-talk.

We avoid a hard dependency on ``sounddevice`` at import time because
unit tests on minimal CI hosts must still be able to import the rest
of the package. The real implementation tries ``sounddevice`` first
(PortAudio backend, works under PulseAudio/PipeWire) and falls back
to the ``arecord`` command-line tool — ``arecord`` ships in
``alsa-utils`` which is preinstalled on stock Ubuntu.
"""

from __future__ import annotations

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

    @property
    def chunk_frames(self) -> int:
        return int(self.sample_rate * self.chunk_ms / 1000)

    @property
    def chunk_bytes(self) -> int:
        # int16 mono
        return self.chunk_frames * 2 * self.channels


class AudioCapture:
    """Push-to-talk microphone capture.

    ``start()`` begins streaming PCM bytes into an internal queue;
    ``read()`` pops one chunk; ``stop()`` ends capture. The same object
    is reusable across multiple talk presses — :meth:`start` resets
    state.
    """

    def __init__(self, config: AudioConfig | None = None, device: str | None = None):
        self.config = config or AudioConfig()
        self.device = device
        self._queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=128)
        self._stop_event = threading.Event()
        self._stream = None  # sounddevice stream handle, when used
        self._proc: subprocess.Popen | None = None  # arecord handle, when used
        self._reader: threading.Thread | None = None
        self._backend: str | None = None

    # ------------------------------------------------------------------
    # backend selection

    def _try_sounddevice(self) -> bool:
        try:
            import sounddevice as sd  # type: ignore
            import numpy as np  # type: ignore
        except Exception:
            return False

        def _cb(indata, frames, time_info, status):  # noqa: ARG001
            if status:
                # underflow / overflow — keep going, the user cares about words.
                pass
            try:
                self._queue.put_nowait(bytes(indata))
            except queue.Full:
                pass

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
            # Capture stderr (not DEVNULL) so we can surface the real
            # reason if arecord exits immediately — e.g. "device or
            # resource busy", "audio open error: No such file or
            # directory". Without this, the daemon would silently
            # record nothing for ``final_wait_seconds`` then warn
            # "no final transcript" with no diagnostic.
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            return False

        # Give arecord a moment to actually open the capture device.
        # If it bails out (busy device, missing PCM, ALSA misconfig),
        # raise immediately with the stderr text so the user gets a
        # real error notification instead of a 5-second silent timeout.
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

        # arecord is alive. Spawn a small daemon thread to drain stderr
        # so a long recording doesn't get throttled by a full 64 KB
        # pipe buffer if arecord ever writes a warning. With ``-q``
        # this is rare but the cost is one trivial thread per session
        # and the failure mode without it (stdout starves silently)
        # would be diabolical to debug.
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
            chunk = self.config.chunk_bytes
            try:
                while not self._stop_event.is_set():
                    data = self._proc.stdout.read(chunk)
                    if not data:
                        break
                    try:
                        self._queue.put_nowait(data)
                    except queue.Full:
                        pass
            finally:
                self._queue.put(None)

        self._reader = threading.Thread(target=_reader_loop, daemon=True)
        self._reader.start()
        self._backend = "arecord"
        return True

    # ------------------------------------------------------------------
    # public API

    def start(self) -> str:
        """Open the microphone. Returns the backend name actually used."""
        self._stop_event.clear()
        # drain any leftovers from a prior session
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        if self._try_sounddevice():
            return "sounddevice"
        if self._try_arecord():
            return "arecord"
        raise AudioCaptureError(
            "no audio backend available — install python-sounddevice "
            "or alsa-utils (arecord)"
        )

    def read(self, timeout: float = 1.0) -> bytes | None:
        """Pop the next PCM chunk; return None when capture has ended."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return b""

    def stop(self) -> None:
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
        # sentinel so any blocked reader returns
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._backend = None

    def chunks(self) -> Iterator[bytes]:
        """Yield chunks until the backend stops or :meth:`stop` is called.

        Empty bytes (``b""``) on read timeout are treated as keep-alives
        and skipped. ``None`` (sentinel) ends iteration.
        """
        while True:
            chunk = self.read()
            if chunk is None:
                return
            if chunk:
                yield chunk
