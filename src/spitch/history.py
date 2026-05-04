"""Recent-transcript ring buffer with on-disk persistence.

Spitch v0.5 added a "console" UI that lets the user re-paste a recent
transcript (the original injection failed, focused the wrong window,
or the user wants to send the same sentence twice). The daemon
maintains an in-memory deque of the last N final transcripts and
mirrors it to a JSONL file under ``$XDG_STATE_HOME/spitch/history.jsonl``
so the console can read it without an IPC channel.

Persistence layout: one JSON object per line, oldest first. Atomic
write via mkstemp + os.replace so a daemon crash mid-write doesn't
corrupt the file.

Audio bytes are *not* persisted — only the recognized text + a small
amount of metadata. That keeps the file small and avoids leaking voice
clips to disk in plain form.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class HistoryEntry:
    """One finalized voice-to-text session.

    ``timestamp`` is epoch seconds (UTC, fractional for ms precision).
    ``inject_ok`` tracks whether the auto-paste actually succeeded; a
    user looking at history will primarily want to repaste the ones
    where it didn't.
    """
    timestamp: float
    text: str
    duration_s: float = 0.0
    inject_ok: bool = True
    # Best-effort label for the focused application at inject time —
    # populated from `wmctrl` / `xdotool getactivewindow` if available,
    # otherwise empty. Pure metadata for the console UI; not used for
    # anything load-bearing.
    target_app: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HistoryEntry":
        # Tolerate missing/extra fields so an older history.jsonl can
        # still be loaded by a newer daemon version.
        return cls(
            timestamp=float(d.get("timestamp", 0.0)),
            text=str(d.get("text", "")),
            duration_s=float(d.get("duration_s", 0.0)),
            inject_ok=bool(d.get("inject_ok", True)),
            target_app=str(d.get("target_app", "")),
        )


class HistoryRing:
    """Bounded deque of :class:`HistoryEntry` with optional jsonl
    persistence. Thread-safe — callers from the hotkey thread, the
    inject thread, and the console-RPC thread all share one instance.
    """

    def __init__(self, capacity: int = 50, path: Path | str | None = None):
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._path = Path(path) if path is not None else None
        self._entries: deque[HistoryEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        if self._path is not None:
            self._load_from_disk()

    @property
    def capacity(self) -> int:
        return self._capacity

    def append(self, entry: HistoryEntry) -> None:
        with self._lock:
            self._entries.append(entry)
            self._persist_locked()

    def latest(self) -> HistoryEntry | None:
        with self._lock:
            return self._entries[-1] if self._entries else None

    def get(self, index: int) -> HistoryEntry | None:
        """Return entry by chronological index. Negative indices count
        from the end, just like list[]. Returns None if out of range
        (so the console doesn't have to wrap calls in try/except)."""
        with self._lock:
            try:
                return self._entries[index]
            except IndexError:
                return None

    def all(self) -> list[HistoryEntry]:
        """Return a snapshot, oldest first. Safe to mutate the list."""
        with self._lock:
            return list(self._entries)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._persist_locked()

    def remove(self, index: int) -> bool:
        """Remove the entry at ``index``. Returns True if removed."""
        with self._lock:
            try:
                del self._entries[index]
            except IndexError:
                return False
            self._persist_locked()
            return True

    # -- persistence ---------------------------------------------------

    def _persist_locked(self) -> None:
        """Write the deque to ``self._path`` atomically. Caller holds
        ``self._lock``. Failures are swallowed — losing history is a
        much smaller problem than crashing the daemon."""
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                prefix=self._path.name + ".",
                suffix=".tmp",
                dir=self._path.parent,
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for e in self._entries:
                        fh.write(json.dumps(e.to_dict(), ensure_ascii=False))
                        fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            try:
                # User-only — entries can contain dictation of private
                # text (passwords, drafts, etc.).
                os.chmod(self._path, 0o600)
            except OSError:
                pass
        except Exception:
            pass  # best-effort

    def _load_from_disk(self) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        if isinstance(d, dict):
                            self._entries.append(HistoryEntry.from_dict(d))
                    except (ValueError, TypeError):
                        continue
        except OSError:
            pass


def default_history_path() -> Path:
    """``$XDG_STATE_HOME/spitch/history.jsonl`` (default
    ``~/.local/state/spitch/history.jsonl``)."""
    base = os.environ.get("XDG_STATE_HOME")
    state_dir = Path(base) if base else Path.home() / ".local" / "state"
    return state_dir / "spitch" / "history.jsonl"
