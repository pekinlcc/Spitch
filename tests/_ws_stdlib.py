"""Minimal stdlib RFC 6455 WebSocket server + client (round-9 only).

Spitch's :class:`DoubaoClient` lazily imports the third-party
``websockets`` library, which is not available on this CI host. To
prove the *full* voice pipeline (audio capture → WS protocol → final
commit) on this host without taking the live SaaS grant the operator
owns, round 9 needs both ends of a WebSocket-spoken Doubao server.

This module implements just enough of the WS protocol on top of
``asyncio.StreamReader``/``StreamWriter`` for that scope:

  * server-side upgrade handshake (Sec-WebSocket-Accept);
  * client-side upgrade handshake;
  * binary message frames in both directions (RFC 6455 opcode 0x2);
  * close frames (opcode 0x8) on graceful shutdown;
  * payload length encoding for ≤125 / 126+2-byte / 127+8-byte sizes.

We do NOT implement: ping/pong frames, fragmentation, extensions,
permessage-deflate. The mock Doubao server we run against doesn't
need them, and a pipeline test that finishes in <2 s is well below
keepalive timers.

This file is test-only — it lives under ``tests/`` and is never
loaded by the engine. The Spitch source tree ships against the real
``websockets`` library on the user's host (declared in pyproject.toml).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import struct
from typing import AsyncIterator, Awaitable, Callable, Iterable
from urllib.parse import urlparse

WS_MAGIC = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_MAGIC).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


# ---------------------------------------------------------------------------
# Frame encode / decode
# ---------------------------------------------------------------------------


def _encode_frame(payload: bytes, *, opcode: int = 0x2, mask: bool = False) -> bytes:
    """Encode one final, single-frame WebSocket message.

    ``mask=True`` is used by clients (RFC says client→server is always
    masked); servers send unmasked frames.
    """
    fin = 0x80
    head = bytes([fin | (opcode & 0x0F)])
    length = len(payload)
    mask_bit = 0x80 if mask else 0x00
    if length < 126:
        out = head + bytes([mask_bit | length])
    elif length < (1 << 16):
        out = head + bytes([mask_bit | 126]) + struct.pack(">H", length)
    else:
        out = head + bytes([mask_bit | 127]) + struct.pack(">Q", length)
    if mask:
        mask_key = os.urandom(4)
        out += mask_key
        out += bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    else:
        out += payload
    return out


async def _read_exactly(reader: asyncio.StreamReader, n: int) -> bytes:
    if n == 0:
        return b""
    data = await reader.readexactly(n)
    return data


async def _read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one frame from ``reader``; return ``(opcode, payload)``.

    Raises :class:`EOFError` on clean close / connection drop. Continuation
    frames and ping/pong are out of scope for the mock — we assume the
    caller sent FIN=1 single frames, which is what our own client and
    Doubao both do for binary control/audio messages.
    """
    head = await _read_exactly(reader, 2)
    fin = head[0] & 0x80
    opcode = head[0] & 0x0F
    masked = head[1] & 0x80
    length = head[1] & 0x7F
    if length == 126:
        ext = await _read_exactly(reader, 2)
        (length,) = struct.unpack(">H", ext)
    elif length == 127:
        ext = await _read_exactly(reader, 8)
        (length,) = struct.unpack(">Q", ext)
    mask_key = b""
    if masked:
        mask_key = await _read_exactly(reader, 4)
    payload = await _read_exactly(reader, length)
    if masked and mask_key:
        payload = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
    if not fin:
        # Round-9 mock doesn't fragment; promote to error rather than
        # silently mis-parsing the next frame.
        raise EOFError("fragmented frames are not supported by the mock")
    return opcode, payload


# ---------------------------------------------------------------------------
# Server side — accepts upgrade, then exposes a Connection object
# ---------------------------------------------------------------------------


class WSConnection:
    """Server-side connection: read masked client frames, write unmasked frames."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 headers: dict[str, str], path: str):
        self._reader = reader
        self._writer = writer
        self.headers = headers
        self.path = path
        self._closed = False

    async def recv(self) -> bytes:
        while True:
            try:
                opcode, payload = await _read_frame(self._reader)
            except (EOFError, asyncio.IncompleteReadError, ConnectionResetError):
                self._closed = True
                raise EOFError("ws closed")
            if opcode == 0x8:
                self._closed = True
                raise EOFError("ws closed by peer")
            if opcode in (0x9, 0xA):
                # ping/pong — ignore for the mock
                continue
            if opcode in (0x1, 0x2):
                return payload

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise EOFError("ws closed")
        self._writer.write(_encode_frame(data, opcode=0x2, mask=False))
        await self._writer.drain()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.write(_encode_frame(b"", opcode=0x8, mask=False))
            await self._writer.drain()
        except Exception:
            pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def _server_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter
                            ) -> WSConnection | None:
    """Read the HTTP upgrade request, send 101 if valid; return WSConnection."""
    request_lines: list[str] = []
    while True:
        line = await reader.readline()
        if not line:
            return None
        s = line.decode("iso-8859-1").rstrip("\r\n")
        if s == "":
            break
        request_lines.append(s)
    if not request_lines:
        return None
    request_line = request_lines[0]
    parts = request_line.split(" ")
    if len(parts) < 2:
        return None
    path = parts[1]
    headers: dict[str, str] = {}
    for hl in request_lines[1:]:
        if ":" not in hl:
            continue
        k, _, v = hl.partition(":")
        headers[k.strip().lower()] = v.strip()

    key = headers.get("sec-websocket-key")
    if not key or headers.get("upgrade", "").lower() != "websocket":
        # Send a basic 400 so a misconfigured client gets a clear failure.
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return None

    accept = _accept_key(key)
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    )
    writer.write(response.encode("ascii"))
    await writer.drain()
    return WSConnection(reader, writer, headers, path)


async def serve(host: str, port: int,
                handler: Callable[[WSConnection], Awaitable[None]]
                ) -> asyncio.AbstractServer:
    """Start a WS server on ``host:port`` and dispatch each connection to ``handler``.

    Returns the asyncio server; caller is responsible for closing it.
    """
    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            ws = await _server_handshake(reader, writer)
            if ws is None:
                return
            try:
                await handler(ws)
            finally:
                await ws.close()
        except Exception:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    return await asyncio.start_server(_on_connect, host=host, port=port)


# ---------------------------------------------------------------------------
# Client side — implements the slice DoubaoClient needs (send/recv/close)
# ---------------------------------------------------------------------------


class WSClient:
    """Client connection compatible with ``websockets.connect()`` for our use.

    Exposes ``await send(bytes)``, ``await recv() -> bytes``, ``await close()``
    — the only surface :class:`DoubaoClient` consumes from the underlying
    library. Frames are masked (RFC 6455 mandates client-side masking).
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send(self, data: bytes) -> None:
        if self._closed:
            raise EOFError("ws closed")
        self._writer.write(_encode_frame(data, opcode=0x2, mask=True))
        await self._writer.drain()

    async def recv(self) -> bytes:
        while True:
            try:
                opcode, payload = await _read_frame(self._reader)
            except (EOFError, asyncio.IncompleteReadError, ConnectionResetError):
                self._closed = True
                raise EOFError("ws closed")
            if opcode == 0x8:
                self._closed = True
                raise EOFError("ws closed by peer")
            if opcode in (0x9, 0xA):
                continue
            if opcode in (0x1, 0x2):
                return payload

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._writer.write(_encode_frame(b"", opcode=0x8, mask=True))
            await self._writer.drain()
        except Exception:
            pass
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass


async def connect(uri: str, *, headers: Iterable[tuple[str, str]] | None = None,
                  ) -> WSClient:
    """Connect to ``ws://host:port/path`` and return a :class:`WSClient`.

    No TLS. The round-9 mock runs on localhost; the round-8 probe
    already proved the TLS path against the production endpoint.
    """
    parsed = urlparse(uri)
    if parsed.scheme not in ("ws", "wsx"):  # wsx = test alias, never wss
        raise ValueError(f"only ws:// is supported by the stdlib shim; got {uri!r}")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    reader, writer = await asyncio.open_connection(host, port)

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if headers:
        for k, v in headers:
            request.append(f"{k}: {v}")
    request_str = "\r\n".join(request) + "\r\n\r\n"
    writer.write(request_str.encode("ascii"))
    await writer.drain()

    status_line = await reader.readline()
    if not status_line.startswith(b"HTTP/1.1 101"):
        raise ConnectionError(f"unexpected status: {status_line!r}")
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b""):
            break
    return WSClient(reader, writer)
