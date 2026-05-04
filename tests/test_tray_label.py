"""Tests for the tray-label composer.

The full SpitchIndicator depends on PyGObject + GTK + AppIndicator at
runtime; we keep those out of CI by extracting the label-building
logic as a pure function in :mod:`spitch.tray.indicator`. This file
exercises that function directly.
"""

from __future__ import annotations

import unittest

from spitch.tray.indicator import _tail, compose_label
from spitch.voice import State


class TailTests(unittest.TestCase):
    def test_short_passes_through(self):
        self.assertEqual(_tail("你好", 10), "你好")

    def test_exactly_n_passes_through(self):
        self.assertEqual(_tail("a" * 10, 10), "a" * 10)

    def test_long_truncates_with_leading_ellipsis(self):
        out = _tail("0123456789ABCDE", 5)
        # 4 trailing chars + 1 leading "…"
        self.assertEqual(len(out), 5)
        self.assertTrue(out.startswith("…"))
        self.assertEqual(out, "…BCDE")

    def test_keeps_tail_not_head(self):
        # Recent words matter more than ancient words for live ASR
        # feedback; verify we drop from the front.
        out = _tail("早上好今天天气真不错", 5)
        self.assertEqual(out, "…气真不错")


class ComposeLabelTests(unittest.TestCase):
    def test_idle_with_no_partial_is_empty(self):
        self.assertEqual(compose_label(State.IDLE, ""), "")

    def test_idle_with_partial_shows_checkmark(self):
        # The post-final linger uses IDLE + leftover partial to show
        # the recognition result briefly before the tray clears.
        self.assertEqual(compose_label(State.IDLE, "你好世界"), "✓ 你好世界")

    def test_recording_with_no_partial_shows_placeholder(self):
        self.assertEqual(compose_label(State.RECORDING, ""), "🎙 听写中…")

    def test_recording_with_partial_streams_text(self):
        self.assertEqual(
            compose_label(State.RECORDING, "你好世界"),
            "🎙 你好世界",
        )

    def test_recording_long_partial_shows_tail(self):
        long = "今天天气真不错我们去公园散步吧顺便看看樱花"
        out = compose_label(State.RECORDING, long, max_chars=10)
        self.assertTrue(out.startswith("🎙 …"))
        # 10 chars budget → "…" + 9 trailing chars
        self.assertEqual(len(out) - len("🎙 "), 10)

    def test_finalizing_with_partial(self):
        self.assertEqual(
            compose_label(State.FINALIZING, "你好世界"),
            "✍ 你好世界",
        )

    def test_finalizing_with_no_partial_shows_placeholder(self):
        self.assertEqual(compose_label(State.FINALIZING, ""), "✍ 转写中…")

    def test_error_state(self):
        self.assertEqual(compose_label(State.ERROR, ""), "⚠ 出错")

    def test_error_state_ignores_partial(self):
        # Whatever was being said is irrelevant when an error fires.
        self.assertEqual(compose_label(State.ERROR, "anything"), "⚠ 出错")


if __name__ == "__main__":
    unittest.main()
