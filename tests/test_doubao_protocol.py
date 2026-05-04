"""Tests for the Doubao binary frame codec.

These exercise just the bytes — no network, no websockets dep — so
they run on a stock Ubuntu box with python3 only.
"""

from __future__ import annotations

import gzip
import json
import struct
import unittest

from spitch.voice.doubao import (
    CLIENT_AUDIO_ONLY_REQUEST,
    CLIENT_FULL_REQUEST,
    COMP_GZIP,
    COMP_NONE,
    NEG_WITH_SEQUENCE,
    POS_SEQUENCE,
    SER_JSON,
    SER_NONE,
    SERVER_ERROR_RESPONSE,
    SERVER_FULL_RESPONSE,
    DoubaoFrameCodec,
    DoubaoProtocolError,
    auth_headers,
    build_request_meta,
    encode_audio,
    encode_full_request,
    extract_text,
)


def _server_full_response(payload: dict, *, sequence: int | None = None) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    flags = POS_SEQUENCE if sequence is not None else 0
    return DoubaoFrameCodec.encode(
        message_type=SERVER_FULL_RESPONSE,
        flags=flags,
        serialization=SER_JSON,
        compression=COMP_NONE,
        payload=body,
        sequence=sequence,
    )


class FrameRoundTripTests(unittest.TestCase):
    def test_full_request_roundtrip_with_gzip(self):
        meta = {"hello": "world", "中文": "测试"}
        wire = encode_full_request(meta, sequence=1)
        frame = DoubaoFrameCodec.decode(wire)
        self.assertEqual(frame.message_type, CLIENT_FULL_REQUEST)
        self.assertEqual(frame.flags, POS_SEQUENCE)
        self.assertEqual(frame.serialization, SER_JSON)
        self.assertEqual(frame.compression, COMP_GZIP)
        self.assertEqual(frame.sequence, 1)
        self.assertEqual(frame.payload, meta)

    def test_full_request_no_gzip(self):
        meta = {"k": "v"}
        wire = encode_full_request(meta, gzip_payload=False, sequence=42)
        frame = DoubaoFrameCodec.decode(wire)
        self.assertEqual(frame.compression, COMP_NONE)
        self.assertEqual(frame.payload, meta)
        self.assertEqual(frame.sequence, 42)

    def test_audio_chunk_roundtrip(self):
        pcm = bytes(range(256)) * 4  # 1 KB of synthetic audio
        wire = encode_audio(pcm, last=False, sequence=2)
        frame = DoubaoFrameCodec.decode(wire)
        self.assertEqual(frame.message_type, CLIENT_AUDIO_ONLY_REQUEST)
        self.assertEqual(frame.flags, POS_SEQUENCE)
        self.assertEqual(frame.sequence, 2)
        self.assertEqual(frame.serialization, SER_NONE)
        self.assertEqual(frame.compression, COMP_NONE)
        self.assertEqual(frame.payload, pcm)

    def test_audio_chunk_terminal_uses_negative_sequence(self):
        wire = encode_audio(b"final", last=True, sequence=7)
        frame = DoubaoFrameCodec.decode(wire)
        self.assertEqual(frame.message_type, CLIENT_AUDIO_ONLY_REQUEST)
        self.assertEqual(frame.flags, NEG_WITH_SEQUENCE)
        self.assertEqual(frame.sequence, -7)
        self.assertEqual(frame.payload, b"final")

    def test_decode_server_response_json(self):
        payload = {"result": {"text": "你好", "utterances": [
            {"text": "你好", "definite": True}
        ]}}
        wire = _server_full_response(payload)
        frame = DoubaoFrameCodec.decode(wire)
        self.assertEqual(frame.message_type, SERVER_FULL_RESPONSE)
        self.assertEqual(frame.payload, payload)


class FrameErrorTests(unittest.TestCase):
    def test_short_buffer_raises(self):
        with self.assertRaises(DoubaoProtocolError):
            DoubaoFrameCodec.decode(b"\x00\x00")

    def test_truncated_payload_raises(self):
        # claim payload size 100 but only ship 1 byte
        header = bytes([0b00010001, 0b00010000, 0b00010000, 0])
        size = struct.pack(">I", 100)
        wire = header + size + b"x"
        with self.assertRaises(DoubaoProtocolError):
            DoubaoFrameCodec.decode(wire)

    def test_invalid_json_raises(self):
        bad = bytes([0b00010001, 0b00010000, 0b00010000, 0]) + struct.pack(">I", 5) + b"\xff\xff\xff\xff\xff"
        with self.assertRaises(DoubaoProtocolError):
            DoubaoFrameCodec.decode(bad)


class ExtractTextTests(unittest.TestCase):
    def test_partial_utterance(self):
        payload = {"result": {"text": "你", "utterances": [
            {"text": "你", "definite": False}
        ]}}
        text, is_final = extract_text(payload)
        self.assertEqual(text, "你")
        self.assertFalse(is_final)

    def test_definite_utterance(self):
        payload = {"result": {"text": "你好。", "utterances": [
            {"text": "你好。", "definite": True}
        ]}}
        text, is_final = extract_text(payload)
        self.assertEqual(text, "你好。")
        self.assertTrue(is_final)

    def test_mixed_definite_partial_not_final(self):
        payload = {"result": {"text": "a", "utterances": [
            {"text": "a", "definite": True},
            {"text": "b", "definite": False},
        ]}}
        _, is_final = extract_text(payload)
        self.assertFalse(is_final)

    def test_no_result_returns_empty(self):
        text, is_final = extract_text({})
        self.assertEqual(text, "")
        self.assertFalse(is_final)


class AuthHeadersTests(unittest.TestCase):
    def test_required_keys_present(self):
        h = auth_headers(app_key="A", access_key="B", resource_id="R")
        for k in ("X-Api-App-Key", "X-Api-Access-Key", "X-Api-Resource-Id",
                  "X-Api-Connect-Id", "X-Api-Request-Id"):
            self.assertIn(k, h)
        self.assertEqual(h["X-Api-App-Key"], "A")
        self.assertEqual(h["X-Api-Access-Key"], "B")
        self.assertEqual(h["X-Api-Resource-Id"], "R")

    def test_explicit_ids_propagate(self):
        h = auth_headers(
            app_key="A", access_key="B", resource_id="R",
            connect_id="conn-1", request_id="req-1",
        )
        self.assertEqual(h["X-Api-Connect-Id"], "conn-1")
        self.assertEqual(h["X-Api-Request-Id"], "req-1")


class RequestMetaTests(unittest.TestCase):
    def test_default_meta_shape(self):
        meta = build_request_meta()
        # The Doubao bigmodel realtime ASR audio object uses "rate"
        # (not "sample_rate"); regression-protect that field name here.
        self.assertEqual(meta["audio"]["rate"], 16000)
        self.assertNotIn("sample_rate", meta["audio"])
        self.assertEqual(meta["audio"]["format"], "pcm")
        self.assertEqual(meta["audio"]["bits"], 16)
        self.assertEqual(meta["audio"]["channel"], 1)
        self.assertTrue(meta["request"]["enable_punc"])
        self.assertTrue(meta["request"]["enable_itn"])
        self.assertEqual(meta["request"]["model_name"], "bigmodel")

    def test_custom_sample_rate_propagates(self):
        meta = build_request_meta(sample_rate=24000)
        self.assertEqual(meta["audio"]["rate"], 24000)

    def test_uid_is_provided_or_generated(self):
        m1 = build_request_meta(uid="u-1")
        self.assertEqual(m1["user"]["uid"], "u-1")
        m2 = build_request_meta()
        self.assertTrue(m2["user"]["uid"])
        self.assertNotEqual(m1["user"]["uid"], m2["user"]["uid"])


if __name__ == "__main__":
    unittest.main()
