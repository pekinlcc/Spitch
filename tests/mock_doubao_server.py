"""Stdlib mock Doubao bigmodel realtime ASR server (round-9 only).

Speaks the Doubao binary frame protocol that
:mod:`spitch.voice.doubao` ships, against the stdlib WebSocket
implementation in :mod:`tests._ws_stdlib`. Runs on ``ws://127.0.0.1:PORT``
so the round-9 pipeline harness can exercise the *full* hot path —
audio capture → WS protocol encode → server response → WS protocol
decode → final commit — without holding a real Doubao SaaS grant.

Round 8 (`tests/probe_real_endpoint.sh`) already proved the production
endpoint accepts Spitch's exact bytes; the only thing this round adds
is an end-to-end pipeline that proves the *engine subprocess*
correctly drives that protocol. Replacing the mock with the production
endpoint requires only valid keys.

Behavior:
  * Accept the WS upgrade (any path; we ignore the auth headers).
  * On each incoming binary frame, decode it as a Doubao frame.
  * On the first CLIENT_FULL_REQUEST, validate the JSON shape — must
    use ``audio.rate`` (round-5 fix). Reply with one initial partial.
  * For each CLIENT_AUDIO_ONLY_REQUEST audio frame, advance through
    a scripted sequence of partials (yields one partial per chunk
    until the script is exhausted, then echoes the latest partial).
  * On the terminal NEG_WITH_SEQUENCE audio frame, emit the scripted
    final response with ``definite=true``.

The scripted transcript is the canonical "你好世界" demo.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Allow running as a standalone script (python3 tests/mock_doubao_server.py)
# or as part of the test imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from spitch.voice.doubao import (  # noqa: E402
    CLIENT_AUDIO_ONLY_REQUEST,
    CLIENT_FULL_REQUEST,
    COMP_NONE,
    NEG_WITH_SEQUENCE,
    POS_SEQUENCE,
    SER_JSON,
    SERVER_FULL_RESPONSE,
    DoubaoFrameCodec,
)

import _ws_stdlib  # noqa: E402

DEFAULT_PARTIALS = ["你", "你好", "你好世", "你好世界"]
DEFAULT_FINAL = "你好世界。"


def _server_response(payload: dict, sequence: int) -> bytes:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return DoubaoFrameCodec.encode(
        message_type=SERVER_FULL_RESPONSE,
        flags=POS_SEQUENCE,
        serialization=SER_JSON,
        compression=COMP_NONE,
        payload=body,
        sequence=sequence,
    )


def _partial(text: str, sequence: int) -> bytes:
    return _server_response(
        {
            "result": {
                "text": text,
                "utterances": [{"text": text, "definite": False}],
            }
        },
        sequence,
    )


def _final(text: str, sequence: int) -> bytes:
    return _server_response(
        {
            "result": {
                "text": text,
                "utterances": [{"text": text, "definite": True}],
            }
        },
        sequence,
    )


async def session(ws: "_ws_stdlib.WSConnection",
                  *, partials: list[str] = DEFAULT_PARTIALS,
                  final_text: str = DEFAULT_FINAL) -> None:
    """Run one Doubao-spoken session against ``ws``.

    The mock is intentionally tolerant: we don't validate sequence
    numbers (the real server does, but the round-8 endpoint probe
    already covers schema-level correctness).
    """
    seq = 0
    audio_count = 0
    saw_full = False
    saw_terminal = False

    try:
        while True:
            try:
                raw = await ws.recv()
            except EOFError:
                return
            try:
                frame = DoubaoFrameCodec.decode(raw)
            except Exception as exc:  # noqa: BLE001
                # malformed → close
                print(f"mock: decode error {exc!r}", file=sys.stderr)
                return

            if frame.message_type == CLIENT_FULL_REQUEST:
                saw_full = True
                # Validate the wire shape — round-5 fix
                if isinstance(frame.payload, dict):
                    audio = frame.payload.get("audio") or {}
                    if "sample_rate" in audio or "rate" not in audio:
                        # Don't crash; reply with an error-shaped partial
                        # so the client sees the regression loudly.
                        seq += 1
                        await ws.send(_partial(
                            f"!! mock-doubao schema-mismatch: {audio!r}",
                            seq,
                        ))
                        continue
                # First partial — the harness uses this as a "server is
                # alive" beacon.
                seq += 1
                await ws.send(_partial(partials[0] if partials else "", seq))
                continue

            if frame.message_type == CLIENT_AUDIO_ONLY_REQUEST:
                audio_count += 1
                last = frame.flags == NEG_WITH_SEQUENCE
                if last:
                    saw_terminal = True
                    seq += 1
                    await ws.send(_final(final_text, seq))
                    return
                # advance through scripted partials
                idx = min(audio_count, len(partials) - 1)
                if idx >= 0 and partials:
                    seq += 1
                    await ws.send(_partial(partials[idx], seq))
                continue

            # any other frame type — ignore
    finally:
        # Diagnostic
        sys.stderr.write(
            f"mock: ended saw_full={saw_full} audio_frames={audio_count} "
            f"saw_terminal={saw_terminal}\n"
        )


async def run(host: str = "127.0.0.1", port: int = 0) -> tuple[asyncio.AbstractServer, int]:
    """Start the mock server. Returns ``(server, bound_port)``."""
    server = await _ws_stdlib.serve(host, port, session)
    bound_port = server.sockets[0].getsockname()[1]
    return server, bound_port


def main() -> int:
    """CLI for running the mock standalone (`python3 tests/mock_doubao_server.py`)."""
    import argparse

    p = argparse.ArgumentParser(description="stdlib mock Doubao server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=18080)
    args = p.parse_args()

    async def _serve():
        server, port = await run(args.host, args.port)
        print(f"mock-doubao listening on ws://{args.host}:{port}", flush=True)
        async with server:
            await server.serve_forever()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
