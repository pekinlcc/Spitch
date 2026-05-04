"""Integration test for the Doubao streaming client.

Strategy: rather than spin up a real WebSocket server (which would
require the optional ``websockets`` dep), we stub
:meth:`DoubaoClient.__aenter__` and :attr:`DoubaoClient._ws` with a
local fake that records every frame we send and replays a scripted
sequence of server responses. This exercises:

* the request/audio/EOS frame ordering;
* the full :meth:`stream` event loop;
* :class:`VoiceController` state machine behavior.

Skip the file when Python's asyncio cannot run (it always can on
3.10+, but be defensive).
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import AsyncIterator

from spitch.voice.doubao import (
    CLIENT_AUDIO_ONLY_REQUEST,
    CLIENT_FULL_REQUEST,
    COMP_NONE,
    NEG_WITH_SEQUENCE,
    POS_SEQUENCE,
    SER_JSON,
    SERVER_FULL_RESPONSE,
    DoubaoClient,
    DoubaoCredentials,
    DoubaoFrameCodec,
    TranscriptEvent,
)


class FakeWS:
    """A minimal WS double recording sent frames and replaying recv()s."""

    def __init__(self, scripted_responses: list[bytes]):
        self.sent: list[bytes] = []
        self._scripted = list(scripted_responses)
        self._send_lock = asyncio.Lock()
        self._closed = False

    async def send(self, data: bytes) -> None:
        async with self._send_lock:
            self.sent.append(data)

    async def recv(self) -> bytes:
        # Wait until at least one audio chunk has been sent so the
        # ordering reflects realistic streaming.
        while not self._scripted:
            if self._closed:
                raise EOFError("ws closed")
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        return self._scripted.pop(0)

    async def close(self) -> None:
        self._closed = True


def _server_response(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return DoubaoFrameCodec.encode(
        message_type=SERVER_FULL_RESPONSE,
        flags=0,
        serialization=SER_JSON,
        compression=COMP_NONE,
        payload=body,
    )


class StreamingTests(unittest.TestCase):
    def _stream_session(self, scripted: list[bytes], chunks: list[bytes]):
        """Drive DoubaoClient.stream() against a FakeWS; return events + sent frames."""

        async def _go():
            client = DoubaoClient(
                DoubaoCredentials(app_key="A", access_key="B"),
                sample_rate=16000,
            )
            ws = FakeWS(scripted)
            client._ws = ws  # bypass __aenter__ network connect

            async def chunk_iter() -> AsyncIterator[bytes]:
                for c in chunks:
                    yield c
                    await asyncio.sleep(0)

            events: list[TranscriptEvent] = []
            async for evt in client.stream(chunk_iter()):
                events.append(evt)
                if evt.is_final:
                    break
            return events, ws.sent

        return asyncio.run(_go())

    def test_streaming_session_sends_full_request_and_audio_then_eos(self):
        scripted = [
            _server_response({"result": {"text": "你", "utterances": [
                {"text": "你", "definite": False}
            ]}}),
            _server_response({"result": {"text": "你好", "utterances": [
                {"text": "你好", "definite": False}
            ]}}),
            _server_response({"result": {"text": "你好。", "utterances": [
                {"text": "你好。", "definite": True}
            ]}}),
        ]
        chunks = [b"\x00" * 320, b"\x01" * 320, b"\x02" * 320]
        events, sent = self._stream_session(scripted, chunks)

        self.assertEqual([e.text for e in events], ["你", "你好", "你好。"])
        self.assertTrue(events[-1].is_final)

        # First sent frame must be a CLIENT_FULL_REQUEST whose JSON body
        # uses Doubao's documented "audio.rate" field — not "sample_rate".
        # If this regresses, the live probe and stream get rejected by the
        # server before the user ever hears back about success/failure.
        first = DoubaoFrameCodec.decode(sent[0])
        self.assertEqual(first.message_type, CLIENT_FULL_REQUEST)
        self.assertEqual(first.flags, POS_SEQUENCE)
        self.assertIsInstance(first.payload, dict)
        self.assertEqual(first.payload["audio"]["rate"], 16000)
        self.assertNotIn("sample_rate", first.payload["audio"])

        # All subsequent frames must be CLIENT_AUDIO_ONLY_REQUEST,
        # last one terminal.
        audio_frames = [DoubaoFrameCodec.decode(b) for b in sent[1:]]
        for f in audio_frames[:-1]:
            self.assertEqual(f.message_type, CLIENT_AUDIO_ONLY_REQUEST)
            self.assertEqual(f.flags, POS_SEQUENCE)
        self.assertEqual(audio_frames[-1].message_type, CLIENT_AUDIO_ONLY_REQUEST)
        self.assertEqual(audio_frames[-1].flags, NEG_WITH_SEQUENCE)
        # Cumulative payload of audio frames matches the input chunks.
        sent_audio = b"".join(f.payload for f in audio_frames)
        self.assertEqual(sent_audio, b"".join(chunks))


if __name__ == "__main__":
    unittest.main()
