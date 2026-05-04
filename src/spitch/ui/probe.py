"""Synchronous Doubao auth probe used by ``spitch-config``.

Runs the async :meth:`spitch.voice.doubao.DoubaoClient.probe` from a
thread + event loop so the GTK dialog stays responsive. The result is
``(ok, message)`` where ``ok`` is False on connection / auth / protocol
errors and ``message`` is human-readable.
"""

from __future__ import annotations

import asyncio
from typing import Tuple

from ..voice.doubao import DoubaoClient, DoubaoCredentials, DoubaoProtocolError


def probe_credentials(creds: DoubaoCredentials, *, timeout: float = 8.0) -> Tuple[bool, str]:
    """Open the Doubao WS, send a zero-length stream, expect a non-error reply.

    Errors are wrapped into a friendly message. The probe deliberately
    does NOT require any audio device — it just round-trips the
    handshake + control frames so we can tell the user "config is OK".
    """

    async def _go() -> Tuple[bool, str]:
        try:
            async with DoubaoClient(creds) as client:
                await client.probe(timeout=timeout)
            return True, "Doubao connection succeeded — credentials accepted."
        except DoubaoProtocolError as exc:
            return False, f"Server rejected the credentials: {exc}"
        except asyncio.TimeoutError:
            return False, "Timed out waiting for the Doubao server."
        except Exception as exc:  # network, DNS, TLS, etc.
            return False, f"Cannot reach Doubao endpoint: {exc!r}"

    try:
        return asyncio.run(_go())
    except RuntimeError as exc:
        # Already-running loop (very unlikely from GTK main thread but
        # be defensive): make a fresh loop in this thread.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_go())
        finally:
            loop.close()
