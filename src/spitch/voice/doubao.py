"""Doubao (Volcano Engine) realtime ASR client.

Implements the binary frame protocol used by the BigModel realtime ASR
endpoint at ``wss://openspeech.bytedance.com/api/v3/sauc/bigmodel``.

The protocol layout (matching Volcano's reference Python sample):

    byte 0 : (protocol_version << 4) | header_size_in_32bit_units
    byte 1 : (message_type    << 4) | message_type_specific_flags
    byte 2 : (serialization   << 4) | compression
    byte 3 : reserved (0)

After the 4-byte header come:
  * a 4-byte big-endian sequence number IFF the flags carry a sequence
    bit (POS_SEQUENCE / NEG_WITH_SEQUENCE);
  * a 4-byte big-endian payload size;
  * the payload itself (JSON for control frames, raw PCM bytes for
    audio frames; both may be gzipped).

Spitch only needs the slice of the spec required for hold-to-talk:

    1. send a full-client-request describing audio + request meta;
    2. stream PCM audio chunks; mark the final chunk with NEG_SEQUENCE
       to signal end-of-stream;
    3. read SERVER_ACK / SERVER_FULL_RESPONSE frames containing
       partial / final transcripts; raise on SERVER_ERROR_RESPONSE.

The :class:`DoubaoFrameCodec` class deals only with bytes — it has no
network dependency, which keeps it unit-testable. :class:`DoubaoClient`
wraps the codec with a ``websockets`` connection and exposes
``stream(audio_iter)`` -> async iterator of result dicts; that part is
exercised by ``tests/test_doubao_client_mock.py`` against a local
mock WS server.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
import uuid
from dataclasses import dataclass
from typing import AsyncIterator, Iterable

PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001  # in 32-bit units, so a 4-byte header

# Message types (high nibble of byte 1)
CLIENT_FULL_REQUEST = 0b0001
CLIENT_AUDIO_ONLY_REQUEST = 0b0010
SERVER_FULL_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR_RESPONSE = 0b1111

# Message-type specific flags (low nibble of byte 1)
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001  # carries a positive sequence number
NEG_SEQUENCE = 0b0010  # last frame, no sequence number in payload
NEG_WITH_SEQUENCE = 0b0011  # last frame, carries a (negative) sequence number

# Serialization (high nibble of byte 2)
SER_NONE = 0b0000
SER_JSON = 0b0001

# Compression (low nibble of byte 2)
COMP_NONE = 0b0000
COMP_GZIP = 0b0001


class DoubaoProtocolError(Exception):
    """Raised when an incoming frame is malformed or the server reports an error."""


@dataclass
class Frame:
    """A decoded WebSocket frame from the Doubao ASR endpoint.

    ``payload`` is bytes for raw frames and a ``dict`` if the frame was
    JSON-serialized; ``sequence`` is ``None`` when the frame carried no
    sequence number.
    """

    message_type: int
    flags: int
    serialization: int
    compression: int
    sequence: int | None
    payload: bytes | dict


class DoubaoFrameCodec:
    """Encode / decode Doubao binary frames, network-independent."""

    @staticmethod
    def encode(
        message_type: int,
        flags: int,
        serialization: int,
        compression: int,
        payload: bytes,
        sequence: int | None = None,
    ) -> bytes:
        """Build the on-wire bytes for one frame.

        ``payload`` is the raw bytes that go on the wire — JSON encoding
        and gzip happen in the caller (see :func:`encode_full_request`,
        :func:`encode_audio`). ``sequence`` is included iff ``flags`` has
        ``POS_SEQUENCE`` or ``NEG_WITH_SEQUENCE`` set.
        """
        if not (0 <= message_type <= 0xF):
            raise ValueError("message_type out of range")
        if not (0 <= flags <= 0xF):
            raise ValueError("flags out of range")
        header = bytes(
            [
                (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
                (message_type << 4) | flags,
                (serialization << 4) | compression,
                0,
            ]
        )
        out = bytearray(header)
        if flags in (POS_SEQUENCE, NEG_WITH_SEQUENCE):
            if sequence is None:
                raise ValueError("sequence required for POS/NEG_WITH_SEQUENCE flags")
            out += struct.pack(">i", sequence)
        out += struct.pack(">I", len(payload))
        out += payload
        return bytes(out)

    @staticmethod
    def decode(data: bytes) -> Frame:
        """Parse a single Doubao frame from ``data``.

        Decompresses if ``compression == COMP_GZIP`` and JSON-decodes the
        payload if ``serialization == SER_JSON``.
        """
        if len(data) < 4:
            raise DoubaoProtocolError(f"frame too short: {len(data)} bytes")
        b0, b1, b2, _b3 = data[0], data[1], data[2], data[3]
        header_size = b0 & 0x0F
        message_type = (b1 >> 4) & 0x0F
        flags = b1 & 0x0F
        serialization = (b2 >> 4) & 0x0F
        compression = b2 & 0x0F
        offset = header_size * 4
        if len(data) < offset:
            raise DoubaoProtocolError("frame shorter than declared header size")
        sequence: int | None = None
        if flags in (POS_SEQUENCE, NEG_WITH_SEQUENCE):
            if len(data) < offset + 4:
                raise DoubaoProtocolError("frame truncated before sequence number")
            (sequence,) = struct.unpack(">i", data[offset : offset + 4])
            offset += 4
        if len(data) < offset + 4:
            raise DoubaoProtocolError("frame truncated before payload size")
        (size,) = struct.unpack(">I", data[offset : offset + 4])
        offset += 4
        if len(data) < offset + size:
            raise DoubaoProtocolError(
                f"frame truncated: declared {size} bytes, got {len(data) - offset}"
            )
        raw = data[offset : offset + size]
        if compression == COMP_GZIP and raw:
            raw = gzip.decompress(raw)
        payload: bytes | dict = raw
        if serialization == SER_JSON and raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DoubaoProtocolError(f"invalid JSON payload: {exc}") from exc
        return Frame(
            message_type=message_type,
            flags=flags,
            serialization=serialization,
            compression=compression,
            sequence=sequence,
            payload=payload,
        )


def _gzip_bytes(b: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(b)
    return buf.getvalue()


def encode_full_request(meta: dict, *, gzip_payload: bool = True, sequence: int = 1) -> bytes:
    """Build a CLIENT_FULL_REQUEST frame carrying ``meta`` as JSON."""
    body = json.dumps(meta, ensure_ascii=False).encode("utf-8")
    comp = COMP_NONE
    if gzip_payload:
        body = _gzip_bytes(body)
        comp = COMP_GZIP
    return DoubaoFrameCodec.encode(
        message_type=CLIENT_FULL_REQUEST,
        flags=POS_SEQUENCE,
        serialization=SER_JSON,
        compression=comp,
        payload=body,
        sequence=sequence,
    )


def encode_audio(pcm: bytes, *, last: bool, sequence: int, gzip_payload: bool = False) -> bytes:
    """Build a CLIENT_AUDIO_ONLY_REQUEST frame for one PCM chunk.

    The terminal chunk uses ``NEG_WITH_SEQUENCE`` and a negated sequence
    number — that's how the server learns this is the final chunk and
    should produce a definite result.
    """
    if last:
        flags = NEG_WITH_SEQUENCE
        seq = -abs(sequence)
    else:
        flags = POS_SEQUENCE
        seq = sequence
    body = pcm
    comp = COMP_NONE
    if gzip_payload and body:
        body = _gzip_bytes(body)
        comp = COMP_GZIP
    return DoubaoFrameCodec.encode(
        message_type=CLIENT_AUDIO_ONLY_REQUEST,
        flags=flags,
        serialization=SER_NONE,
        compression=comp,
        payload=body,
        sequence=seq,
    )


def build_request_meta(
    *,
    sample_rate: int = 16000,
    uid: str | None = None,
    enable_punc: bool = True,
    enable_itn: bool = True,
    show_utterances: bool = True,
    model_name: str = "bigmodel",
) -> dict:
    """Default request metadata for hold-to-talk Mandarin transcription.

    ``enable_punc=True`` and ``enable_itn=True`` together give us the
    "final rewrite" the goal asks for: punctuated, ITN-normalized text.
    ``show_utterances=True`` means the server emits incremental
    utterance-level partials we can render in preedit.
    """
    return {
        "user": {"uid": uid or str(uuid.uuid4())},
        "audio": {
            # Doubao bigmodel realtime ASR uses "rate" (not "sample_rate")
            # per the published JSON schema; sending the wrong key gets
            # the request rejected before any transcription happens.
            "format": "pcm",
            "rate": sample_rate,
            "bits": 16,
            "channel": 1,
            "codec": "raw",
        },
        "request": {
            "model_name": model_name,
            "show_utterances": show_utterances,
            "enable_punc": enable_punc,
            "enable_itn": enable_itn,
            "result_type": "single",
        },
    }


def auth_headers(
    *,
    app_key: str,
    access_key: str,
    resource_id: str,
    connect_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, str]:
    """Return the HTTP upgrade headers for the Doubao bigmodel WS endpoint."""
    return {
        "X-Api-App-Key": app_key,
        "X-Api-Access-Key": access_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": connect_id or str(uuid.uuid4()),
        "X-Api-Request-Id": request_id or str(uuid.uuid4()),
    }


def extract_text(payload: dict) -> tuple[str, bool]:
    """Pull ``(text, is_final)`` out of a SERVER_FULL_RESPONSE / SERVER_ACK payload.

    .. warning:: This function returns ``result.text`` verbatim, which on
       multi-utterance streams contains **only the current in-progress
       utterance**. Once an utterance is marked ``definite=true``, it
       disappears from ``result.text`` and only the next utterance is
       reported there. For an accumulated full transcript across
       utterance boundaries use :func:`extract_full_text`. ``extract_text``
       is kept for the single-utterance fast path and for tests.

    Treats the response as final when every utterance is ``definite=true``.
    """
    if not isinstance(payload, dict):
        return "", False
    result = payload.get("result")
    if not isinstance(result, dict):
        return "", False
    text = result.get("text") or ""
    if not isinstance(text, str):
        text = ""
    utterances = result.get("utterances") or []
    is_final = False
    if isinstance(utterances, list) and utterances:
        # final only when *every* utterance reported is definite
        is_final = all(
            isinstance(u, dict) and u.get("definite") is True for u in utterances
        )
    return text, is_final


def extract_full_text(payload: dict) -> tuple[str, bool]:
    """Pull ``(full_text, is_final)`` reconstructing the **whole transcript**.

    Doubao bigmodel responses look like::

        {
          "result": {
            "text": "<current in-progress utterance>",
            "utterances": [
              {"text": "first sentence.", "definite": true, ...},
              {"text": "second sentence still being recognized",
               "definite": false, ...}
            ]
          }
        }

    Once an utterance is marked ``definite=true``, the server **drops it
    from `result.text`**; only the in-progress utterance is reported
    there. To rebuild the full transcript across utterance boundaries we
    concatenate every ``utterances[].text`` (definite ones first in
    chronological order, plus the current in-progress one).

    Falls back to ``result.text`` when the ``utterances`` array is
    missing or empty (single-utterance fast path / probe responses).

    ``is_final`` is True iff every utterance is definite — that's the
    server's "everything has been finalized" signal, which only fires
    after the client sent the EOS audio frame (user released the talk
    key).
    """
    if not isinstance(payload, dict):
        return "", False
    result = payload.get("result")
    if not isinstance(result, dict):
        return "", False
    utterances = result.get("utterances") or []
    if not isinstance(utterances, list) or not utterances:
        # No utterance breakdown — fall back to result.text. is_final
        # stays False because there's no definite=true marker to flip it.
        text = result.get("text") or ""
        return text if isinstance(text, str) else "", False
    parts: list[str] = []
    for u in utterances:
        if not isinstance(u, dict):
            continue
        t = u.get("text", "")
        if isinstance(t, str) and t:
            parts.append(t)
    full = "".join(parts)
    is_final = all(
        isinstance(u, dict) and u.get("definite") is True for u in utterances
    )
    return full, is_final


# ---------------------------------------------------------------------------
# Live client. Optional — only used when ``websockets`` is installed.
# ---------------------------------------------------------------------------


@dataclass
class DoubaoCredentials:
    app_key: str
    access_key: str
    resource_id: str = "volc.bigasr.sauc.duration"
    endpoint: str = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"


@dataclass
class TranscriptEvent:
    """One transcription update from the server.

    ``text`` is the server's accumulated text so far. ``is_final`` is
    True when this is the last result of the utterance (Doubao's
    ``definite=true`` family).
    """

    text: str
    is_final: bool
    raw: dict


class DoubaoClient:
    """Async streaming client for Doubao bigmodel realtime ASR.

    Usage::

        async with DoubaoClient(creds) as client:
            async for event in client.stream(audio_iter):
                ...

    The class lazily imports ``websockets`` so unit tests that only
    exercise the codec do not need it installed.
    """

    def __init__(self, creds: DoubaoCredentials, *, sample_rate: int = 16000):
        self._creds = creds
        self._sample_rate = sample_rate
        self._ws = None  # populated in __aenter__

    async def __aenter__(self) -> "DoubaoClient":
        try:
            import websockets  # local import — optional dep
        except ImportError as exc:
            raise RuntimeError(
                "websockets package required for live Doubao calls; "
                "install it via pip install websockets"
            ) from exc
        self._ws = await websockets.connect(
            self._creds.endpoint,
            additional_headers=list(
                auth_headers(
                    app_key=self._creds.app_key,
                    access_key=self._creds.access_key,
                    resource_id=self._creds.resource_id,
                ).items()
            ),
            max_size=None,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def probe(self, timeout: float = 5.0) -> bool:
        """Auth + connectivity probe.

        Sends one full-client-request describing a ~zero-length audio
        stream, then a terminal empty audio frame, and waits for the
        server to acknowledge with anything other than an error frame.
        Returns True on success; raises :class:`DoubaoProtocolError` (or
        an underlying network error) on failure.
        """
        import asyncio  # local import keeps top of module light

        if self._ws is None:
            raise RuntimeError("DoubaoClient.probe called outside context manager")
        meta = build_request_meta(sample_rate=self._sample_rate)
        await self._ws.send(encode_full_request(meta, sequence=1))
        await self._ws.send(encode_audio(b"", last=True, sequence=2))
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise DoubaoProtocolError("probe timed out waiting for server reply") from exc
        frame = DoubaoFrameCodec.decode(raw)
        if frame.message_type == SERVER_ERROR_RESPONSE:
            raise DoubaoProtocolError(f"server error: {frame.payload!r}")
        return True

    async def stream(
        self, audio_iter: AsyncIterator[bytes] | Iterable[bytes]
    ) -> AsyncIterator[TranscriptEvent]:
        """Stream PCM chunks from ``audio_iter`` and yield TranscriptEvents.

        ``audio_iter`` may be sync or async. The terminal chunk is sent
        with NEG_WITH_SEQUENCE to flush the server side. Yields one
        TranscriptEvent per server frame; the caller decides when to
        stop iterating (e.g. on ``event.is_final``).
        """
        import asyncio
        import inspect

        if self._ws is None:
            raise RuntimeError("DoubaoClient.stream called outside context manager")
        meta = build_request_meta(sample_rate=self._sample_rate)
        await self._ws.send(encode_full_request(meta, sequence=1))

        # Two coroutines: one feeds audio, one drains responses. We
        # interleave by running the sender as a background task and
        # awaiting recv() in the foreground.
        async def _drain_chunks() -> AsyncIterator[bytes]:
            if hasattr(audio_iter, "__aiter__"):
                async for chunk in audio_iter:  # type: ignore[union-attr]
                    yield chunk
            else:
                for chunk in audio_iter:  # type: ignore[union-attr]
                    yield chunk
                    await asyncio.sleep(0)

        async def _send_audio() -> None:
            seq = 2
            last_chunk: bytes | None = None
            async for chunk in _drain_chunks():
                if last_chunk is not None:
                    await self._ws.send(encode_audio(last_chunk, last=False, sequence=seq))
                    seq += 1
                last_chunk = chunk
            tail = last_chunk if last_chunk is not None else b""
            await self._ws.send(encode_audio(tail, last=True, sequence=seq))

        sender = asyncio.create_task(_send_audio())
        try:
            while True:
                try:
                    raw = await self._ws.recv()
                except Exception:
                    if sender.done() and sender.exception() is None:
                        return
                    raise
                frame = DoubaoFrameCodec.decode(raw)
                if frame.message_type == SERVER_ERROR_RESPONSE:
                    raise DoubaoProtocolError(f"server error: {frame.payload!r}")
                if isinstance(frame.payload, dict):
                    # extract_full_text concatenates every utterance —
                    # critical for multi-utterance streams where
                    # result.text alone drops already-finalized segments.
                    text, is_final = extract_full_text(frame.payload)
                    yield TranscriptEvent(text=text, is_final=is_final, raw=frame.payload)
                    # Do NOT return on is_final. Doubao splits long
                    # utterances into multiple definite segments and
                    # keeps streaming as the user keeps talking; if
                    # we exit on the first one, everything spoken
                    # after it is silently lost. The session ends
                    # naturally when our audio sender finishes (user
                    # released the talk key, EOS frame went out, ws
                    # close response comes back).
        finally:
            if not sender.done():
                sender.cancel()
                try:
                    await sender
                except (asyncio.CancelledError, Exception):
                    pass
