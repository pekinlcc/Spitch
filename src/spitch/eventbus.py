"""In-process fanout event bus used by the salmon-mode subscribe channel.

Background
----------

Spitch's classic paste flow ends at ``inject_text``: the daemon dictates,
finalizes, and pastes into whichever app holds focus. v0.6 adds a second
output mode — ``salmon`` — where instead of injecting, the daemon emits
the transcript as structured events that an external subscriber (the
Salmon Overlay window) consumes over the existing cmdsock.

The bus is intentionally tiny:

* No persistence — the salmon overlay is meant to react live; if it
  wasn't connected when an event fired the event is lost. That's the
  right semantics for a UI overlay (you don't want a stale recording
  popping up an overlay an hour later).
* No filtering or topics — every subscriber gets every event.
  Subscribers are expected to gate on ``event["source"]`` themselves.
* No backpressure — slow subscribers eventually fail to publish() and
  are dropped.

Thread model: ``publish()`` is called from whichever thread emitted
the event (audio callback, hotkey thread, cmdsock thread, etc.).
``subscribe()`` / ``unsubscribe()`` are called from the cmdsock thread.
A single lock guards the subscriber list so registration is safe under
concurrent publishes.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

log = logging.getLogger("spitch.eventbus")


# An event sink is just a callable that takes a JSON-serializable dict.
# The cmdsock streaming handler wraps a socket file object in one of
# these. Returning ``None`` means "delivered"; raising means "drop me".
EventSink = Callable[[dict[str, Any]], None]


class EventBus:
    """Fanout JSON event bus with no buffering and no topics."""

    def __init__(self) -> None:
        self._sinks: list[EventSink] = []
        self._lock = threading.Lock()

    def subscribe(self, sink: EventSink) -> None:
        with self._lock:
            self._sinks.append(sink)

    def unsubscribe(self, sink: EventSink) -> None:
        with self._lock:
            try:
                self._sinks.remove(sink)
            except ValueError:
                pass

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._sinks)

    def publish(self, event: dict[str, Any]) -> None:
        """Deliver ``event`` to every subscriber. A subscriber that
        raises is dropped — typically that means the socket on the
        other end has closed."""
        with self._lock:
            sinks = list(self._sinks)
        dead: list[EventSink] = []
        for sink in sinks:
            try:
                sink(event)
            except Exception as exc:  # noqa: BLE001
                log.debug("subscriber raised, dropping: %r", exc)
                dead.append(sink)
        if dead:
            with self._lock:
                for s in dead:
                    try:
                        self._sinks.remove(s)
                    except ValueError:
                        pass
