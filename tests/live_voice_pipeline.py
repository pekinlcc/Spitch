"""Round-9 full live voice pipeline harness.

Brings up a stdlib mock Doubao server (`tests/mock_doubao_server.py`),
then drives the *full* Spitch voice path against it:

    real-mic audio (arecord/sounddevice via spitch.voice.AudioCapture)
        → spitch.voice.controller.VoiceController
        → spitch.voice.doubao.DoubaoClient (subclassed to use stdlib WS)
        → tests/_ws_stdlib (RFC 6455 over plain TCP)
        → tests/mock_doubao_server (Doubao binary protocol)
        → response frames back through the same chain
        → on_partial / on_final callbacks
        → asserted in this script

Why this matters for round-9 SG7 evidence:

  * Section A's `test_voice_controller.py` already drove
    VoiceController against a fake audio iterator and a fake
    StreamingClient — useful, but bypasses both the mic and the wire
    encode.
  * Section M's `tests/probe_real_endpoint.sh` already proved the
    *production* endpoint accepts Spitch's exact bytes.
  * This script joins those legs end-to-end on this CI host:
    real-hardware mic → real WS encode → real WS server → response
    decode → final commit. The only piece left between this and the
    operator's "speak into mic in a focused GTK app" is the literal
    Doubao SaaS grant the operator owns.

Exit codes:
    0 = pass (partials seen + final committed text matches mock script)
    2 = fail (assertion did not hold)
    other = setup error (mic backend missing, stdlib WS handshake
            broke, etc.)
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))

from spitch.voice.audio import AudioCapture, AudioCaptureError, AudioConfig  # noqa: E402
from spitch.voice.controller import State, VoiceController  # noqa: E402
from spitch.voice.doubao import (  # noqa: E402
    DoubaoClient,
    DoubaoCredentials,
)

import _ws_stdlib  # noqa: E402
import mock_doubao_server as mds  # noqa: E402


class StdlibDoubaoClient(DoubaoClient):
    """DoubaoClient variant that uses the stdlib WebSocket shim.

    The Spitch source ships against the third-party ``websockets``
    library on the user's host. This CI environment does not have it
    installed, so we route the same protocol through the stdlib
    implementation in ``tests/_ws_stdlib`` for the round-9 pipeline
    harness. The bytes on the wire are identical — we override only
    the connection step.
    """

    async def __aenter__(self) -> "StdlibDoubaoClient":
        # _creds.endpoint already points to ws://127.0.0.1:PORT for the mock
        self._ws = await _ws_stdlib.connect(
            self._creds.endpoint,
            headers=[
                ("X-Api-App-Key", self._creds.app_key),
                ("X-Api-Access-Key", self._creds.access_key),
                ("X-Api-Resource-Id", self._creds.resource_id),
            ],
        )
        return self


def main() -> int:
    # ----- bring up the mock Doubao server in a background asyncio thread
    server_holder: dict[str, object] = {}
    server_started = threading.Event()
    server_loop: asyncio.AbstractEventLoop | None = None

    def _server_thread() -> None:
        nonlocal server_loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server_loop = loop

        async def _setup():
            server, port = await mds.run("127.0.0.1", 0)
            server_holder["server"] = server
            server_holder["port"] = port
            server_started.set()
            await server.serve_forever()

        try:
            loop.run_until_complete(_setup())
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            loop.close()

    t = threading.Thread(target=_server_thread, name="mock-doubao", daemon=True)
    t.start()
    if not server_started.wait(timeout=5.0):
        print("FAIL: mock server did not start", file=sys.stderr)
        return 3
    port = server_holder["port"]
    print(f"mock-doubao listening on ws://127.0.0.1:{port}", flush=True)

    # ----- configure the Spitch voice path against the mock
    creds = DoubaoCredentials(
        app_key="ROUND9_TEST_KEY",
        access_key="ROUND9_TEST_SECRET",
        resource_id="volc.bigasr.sauc.duration",
        endpoint=f"ws://127.0.0.1:{port}/api/v3/sauc/bigmodel",
    )

    sample_rate = int(os.environ.get("SPITCH_TEST_SAMPLE_RATE", "16000"))
    audio = AudioCapture(AudioConfig(sample_rate=sample_rate))

    partials: list[str] = []
    finals: list[str] = []
    errors: list[BaseException] = []
    states: list[State] = []

    def on_partial(t: str) -> None:
        partials.append(t)
        print(f"[partial] {t}", flush=True)

    def on_final(t: str) -> None:
        finals.append(t)
        print(f"[final]   {t}", flush=True)

    def on_error(e: BaseException) -> None:
        errors.append(e)
        print(f"[error]   {e!r}", flush=True)

    def on_state(s: State) -> None:
        states.append(s)
        print(f"[state]   {s.value}", flush=True)

    ctrl = VoiceController(
        client_factory=lambda: StdlibDoubaoClient(creds, sample_rate=sample_rate),
        audio=audio,
        on_partial=on_partial,
        on_final=on_final,
        on_error=on_error,
        on_state=on_state,
        finalize_timeout=3.0,
    )

    # ----- press: open mic + WS, start streaming
    record_seconds = float(os.environ.get("SPITCH_TEST_RECORD_S", "1.5"))
    print(f"--- press (record_s={record_seconds}) ---", flush=True)
    try:
        ok = ctrl.press()
    except AudioCaptureError as exc:
        print(f"FAIL: AudioCapture refused to start: {exc}", file=sys.stderr)
        return 4
    if not ok:
        print("FAIL: VoiceController.press() returned False", file=sys.stderr)
        return 4

    # Spitch's AudioCapture is real. arecord opens the system default
    # input. Even if the operator doesn't speak, the mic returns
    # silence/ambient frames at 16 kHz; the protocol is exercised
    # identically. The mock's transcript is scripted server-side, so
    # the final committed text is deterministic.
    time.sleep(record_seconds)

    print("--- release ---", flush=True)
    ctrl.release()

    # Wait for the controller to finalize (final + back to IDLE)
    deadline = time.time() + 6.0
    while time.time() < deadline:
        if ctrl.state == State.IDLE:
            break
        time.sleep(0.05)

    # ----- shut down the mock server
    if server_loop is not None and "server" in server_holder:
        server: asyncio.AbstractServer = server_holder["server"]  # type: ignore[assignment]

        def _stop():
            server.close()
        server_loop.call_soon_threadsafe(_stop)

    # Give the server thread a moment to drain
    t.join(timeout=2.0)

    # ----- verdict
    print()
    print(f"summary partials = {partials!r}", flush=True)
    print(f"summary finals   = {finals!r}", flush=True)
    print(f"summary errors   = {[repr(e) for e in errors]!r}", flush=True)
    print(f"summary states   = {[s.value for s in states]!r}", flush=True)

    saw_any_partial = len(partials) > 0
    final_text = finals[-1] if finals else ""
    expected_final = mds.DEFAULT_FINAL  # "你好世界。"
    final_matches = final_text == expected_final

    verdict = {
        "any_partial_seen": saw_any_partial,
        "final_committed": bool(final_text),
        "final_matches_mock_script": final_matches,
        "no_errors": len(errors) == 0,
    }
    print(f"VERDICT={json.dumps(verdict, ensure_ascii=False)}", flush=True)

    ok = saw_any_partial and final_matches and len(errors) == 0
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
