"""Tiny Unix-socket command channel between the Spitch daemon and the
console / cli.

Designed to be dependency-free (stdlib only) and bidirectional but
trivial: each request is one line of JSON, each response is one line
of JSON. The daemon serves the socket from a background thread; the
console / cli connects per-call (no long-lived sessions).

Socket path: ``$XDG_RUNTIME_DIR/spitch.sock`` (default
``/run/user/<uid>/spitch.sock``). Falls back to
``$XDG_STATE_HOME/spitch/cmd.sock`` if XDG_RUNTIME_DIR is missing
(e.g. some non-systemd hosts).

Commands the daemon handles:

  * ``{"op": "ping"}`` → ``{"ok": true, "version": "x.y.z"}``
  * ``{"op": "list"}`` → ``{"ok": true, "entries": [...]}`` — full
    history snapshot, oldest first.
  * ``{"op": "repaste", "index": -1}`` → re-inject a history entry by
    chronological index. Default is ``-1`` (latest).
  * ``{"op": "delete", "index": 0}`` → drop a history entry.
  * ``{"op": "clear"}`` → wipe history.
  * ``{"op": "reload_config"}`` → re-read ``config.json`` (rebuild the
    voice controller without restarting the daemon process).

Error responses use ``{"ok": false, "error": "<msg>"}``. The cli /
console treats anything else as a transient failure.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import socketserver
import stat
import threading
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("spitch.cmdsock")


def default_socket_path() -> Path:
    """Pick the socket location. Prefers XDG_RUNTIME_DIR (tmpfs,
    auto-cleaned at logout) over XDG_STATE_HOME."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "spitch.sock"
    state = os.environ.get("XDG_STATE_HOME")
    state_dir = Path(state) if state else Path.home() / ".local" / "state"
    return state_dir / "spitch" / "cmd.sock"


# ---------------------------------------------------------------------------
# Server side (lives inside the daemon)
# ---------------------------------------------------------------------------


class CmdServer:
    """Background Unix-socket listener.

    Construct with a dict of op-name → handler. Each handler receives
    the request payload (dict) and returns either a JSON-serializable
    dict (to be wrapped as the response) or raises an exception (which
    becomes ``{"ok": false, "error": "<repr>"}``).
    """

    def __init__(
        self,
        handlers: dict[str, Callable[[dict], dict]],
        path: Path | None = None,
    ):
        self._handlers = dict(handlers)
        self._path = path or default_socket_path()
        self._server: socketserver.UnixStreamServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def path(self) -> Path:
        return self._path

    def start(self) -> None:
        # Make sure the parent dir exists.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Remove a stale socket from a previous daemon crash. AF_UNIX
        # bind fails with EADDRINUSE otherwise.
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass
        handlers = self._handlers

        class _Handler(socketserver.StreamRequestHandler):
            def handle(self_inner) -> None:
                try:
                    raw = self_inner.rfile.readline()
                    if not raw:
                        return
                    try:
                        req = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, ValueError):
                        self_inner.wfile.write(
                            (json.dumps({"ok": False, "error": "invalid JSON"})
                             + "\n").encode("utf-8")
                        )
                        return
                    if not isinstance(req, dict):
                        self_inner.wfile.write(
                            (json.dumps({"ok": False, "error": "request must be a JSON object"})
                             + "\n").encode("utf-8")
                        )
                        return
                    op = req.get("op")
                    handler = handlers.get(op)
                    if handler is None:
                        self_inner.wfile.write(
                            (json.dumps({"ok": False, "error": f"unknown op: {op!r}"})
                             + "\n").encode("utf-8")
                        )
                        return
                    try:
                        result = handler(req) or {}
                        if not isinstance(result, dict):
                            result = {"value": result}
                        result.setdefault("ok", True)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("cmdsock handler %r raised", op)
                        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    self_inner.wfile.write(
                        (json.dumps(result, ensure_ascii=False) + "\n").encode("utf-8")
                    )
                except Exception:
                    log.exception("cmdsock dispatch failed")

        self._server = socketserver.UnixStreamServer(
            str(self._path), _Handler, bind_and_activate=False
        )
        # bind ourselves so we can chmod 600 BEFORE accepting (avoid
        # any window where another user could connect).
        self._server.server_bind()
        try:
            os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        self._server.server_activate()
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="spitch-cmdsock",
            daemon=True,
        )
        self._thread.start()
        log.info("cmd socket listening at %s", self._path)

    def stop(self) -> None:
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        try:
            if self._path.exists():
                self._path.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Client side (used by spitch-cli and the console)
# ---------------------------------------------------------------------------


def call(op: str, *, timeout: float = 5.0, path: Path | None = None,
         **payload: Any) -> dict:
    """Synchronous one-shot RPC. Connect, send one JSON line, read one
    JSON line, close. Returns the parsed response dict.

    Raises ``ConnectionError`` if the daemon is not running (no socket
    file or refused connection) — the cli surfaces that as a friendly
    "is the daemon running?" message instead of a Python traceback.
    """
    sock_path = path or default_socket_path()
    if not sock_path.exists():
        raise ConnectionError(f"daemon not running (no socket at {sock_path})")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(str(sock_path))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            raise ConnectionError(f"daemon not reachable: {exc}") from exc
        req = {"op": op, **payload}
        s.sendall((json.dumps(req, ensure_ascii=False) + "\n").encode("utf-8"))
        # Read one line of response.
        buf = bytearray()
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf:
                break
        line = bytes(buf).split(b"\n", 1)[0].decode("utf-8", errors="replace")
        if not line:
            raise ConnectionError("daemon closed connection without responding")
        try:
            return json.loads(line)
        except ValueError as exc:
            raise ConnectionError(f"daemon returned invalid JSON: {line!r}") from exc
    finally:
        try:
            s.close()
        except OSError:
            pass
