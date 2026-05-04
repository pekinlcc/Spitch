"""Tests for spitch.history.HistoryRing — the v0.5 console feature
needs reliable persistence across daemon restarts."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path

from spitch.history import HistoryEntry, HistoryRing, default_history_path


class HistoryEntryTests(unittest.TestCase):
    def test_round_trip(self):
        e = HistoryEntry(
            timestamp=1700000000.5,
            text="你好世界",
            duration_s=1.5,
            inject_ok=True,
            target_app="claude",
        )
        d = e.to_dict()
        e2 = HistoryEntry.from_dict(d)
        self.assertEqual(e, e2)

    def test_from_dict_tolerates_missing_fields(self):
        # Old-format history.jsonl without target_app / inject_ok
        e = HistoryEntry.from_dict({"timestamp": 1.0, "text": "hi"})
        self.assertEqual(e.text, "hi")
        self.assertEqual(e.target_app, "")
        self.assertTrue(e.inject_ok)

    def test_from_dict_tolerates_extra_fields(self):
        # Forward compat: future schema adds fields, today's daemon
        # should still load.
        e = HistoryEntry.from_dict({
            "timestamp": 1.0, "text": "x", "future_field": 42,
        })
        self.assertEqual(e.text, "x")


class HistoryRingTests(unittest.TestCase):
    def _make_entry(self, text: str, t: float = 0.0) -> HistoryEntry:
        return HistoryEntry(timestamp=t, text=text)

    def test_capacity_must_be_positive(self):
        with self.assertRaises(ValueError):
            HistoryRing(capacity=0)
        with self.assertRaises(ValueError):
            HistoryRing(capacity=-3)

    def test_ring_drops_oldest_when_full(self):
        r = HistoryRing(capacity=3)
        r.append(self._make_entry("a"))
        r.append(self._make_entry("b"))
        r.append(self._make_entry("c"))
        r.append(self._make_entry("d"))
        self.assertEqual([e.text for e in r.all()], ["b", "c", "d"])

    def test_latest(self):
        r = HistoryRing()
        self.assertIsNone(r.latest())
        r.append(self._make_entry("first"))
        r.append(self._make_entry("second"))
        self.assertEqual(r.latest().text, "second")

    def test_get_by_index(self):
        r = HistoryRing()
        r.append(self._make_entry("a"))
        r.append(self._make_entry("b"))
        r.append(self._make_entry("c"))
        self.assertEqual(r.get(0).text, "a")
        self.assertEqual(r.get(-1).text, "c")
        self.assertIsNone(r.get(5))
        self.assertIsNone(r.get(-99))

    def test_all_returns_a_snapshot(self):
        r = HistoryRing()
        r.append(self._make_entry("a"))
        snap = r.all()
        snap.clear()  # mutating snapshot must not touch the ring
        self.assertEqual(len(r.all()), 1)

    def test_remove(self):
        r = HistoryRing()
        r.append(self._make_entry("a"))
        r.append(self._make_entry("b"))
        r.append(self._make_entry("c"))
        self.assertTrue(r.remove(1))
        self.assertEqual([e.text for e in r.all()], ["a", "c"])
        self.assertFalse(r.remove(99))

    def test_clear(self):
        r = HistoryRing()
        r.append(self._make_entry("a"))
        r.clear()
        self.assertEqual(r.all(), [])

    def test_thread_safe_concurrent_appends(self):
        r = HistoryRing(capacity=200)

        def writer(start: int):
            for i in range(50):
                r.append(self._make_entry(f"t{start + i}"))

        threads = [threading.Thread(target=writer, args=(s,)) for s in (0, 100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # No assert about ordering — just no exceptions, and total count
        # equals 100 (capacity is 200, so nothing dropped).
        self.assertEqual(len(r.all()), 100)


class HistoryRingPersistenceTests(unittest.TestCase):
    def test_persist_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            r = HistoryRing(path=p)
            r.append(HistoryEntry(timestamp=1.0, text="第一条"))
            r.append(HistoryEntry(timestamp=2.0, text="第二条", target_app="claude"))
            self.assertTrue(p.exists())

            # Fresh ring loads from disk.
            r2 = HistoryRing(path=p)
            entries = r2.all()
            self.assertEqual(len(entries), 2)
            self.assertEqual(entries[0].text, "第一条")
            self.assertEqual(entries[1].target_app, "claude")

    def test_persist_chmods_600(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            r = HistoryRing(path=p)
            r.append(HistoryEntry(timestamp=1.0, text="敏感内容"))
            mode = stat.S_IMODE(p.stat().st_mode)
            self.assertEqual(mode, 0o600)

    def test_persist_atomic_no_temp_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            r = HistoryRing(path=p)
            r.append(HistoryEntry(timestamp=1.0, text="hi"))
            files = sorted(os.listdir(td))
            self.assertEqual(files, ["hist.jsonl"])

    def test_load_tolerates_corrupt_lines(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            p.write_text(
                json.dumps({"timestamp": 1.0, "text": "ok"}) + "\n"
                + "{not valid json\n"
                + "\n"  # blank line
                + json.dumps({"timestamp": 2.0, "text": "also ok"}) + "\n",
                encoding="utf-8",
            )
            r = HistoryRing(path=p)
            texts = [e.text for e in r.all()]
            self.assertEqual(texts, ["ok", "also ok"])

    def test_capacity_enforced_through_persistence(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            r = HistoryRing(capacity=2, path=p)
            for i in range(5):
                r.append(HistoryEntry(timestamp=float(i), text=f"t{i}"))
            r2 = HistoryRing(capacity=2, path=p)
            self.assertEqual([e.text for e in r2.all()], ["t3", "t4"])

    def test_remove_persists(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "hist.jsonl"
            r = HistoryRing(path=p)
            r.append(HistoryEntry(timestamp=1.0, text="a"))
            r.append(HistoryEntry(timestamp=2.0, text="b"))
            r.remove(0)
            r2 = HistoryRing(path=p)
            self.assertEqual([e.text for e in r2.all()], ["b"])


class DefaultPathTests(unittest.TestCase):
    def test_xdg_state_home_honored(self):
        prev = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = "/tmp/spitch-test-xdg"
        try:
            p = default_history_path()
            self.assertEqual(p, Path("/tmp/spitch-test-xdg/spitch/history.jsonl"))
        finally:
            if prev is None:
                del os.environ["XDG_STATE_HOME"]
            else:
                os.environ["XDG_STATE_HOME"] = prev

    def test_default_under_home(self):
        prev = os.environ.pop("XDG_STATE_HOME", None)
        try:
            p = default_history_path()
            self.assertEqual(p, Path.home() / ".local" / "state" / "spitch" / "history.jsonl")
        finally:
            if prev is not None:
                os.environ["XDG_STATE_HOME"] = prev


if __name__ == "__main__":
    unittest.main()
