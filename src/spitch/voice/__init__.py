"""Voice subsystem: Doubao client, audio capture, push-to-talk controller."""

from .audio import AudioCapture, AudioCaptureError, AudioConfig
from .controller import State, TranscriptUpdate, VoiceController
from .doubao import (
    DoubaoClient,
    DoubaoCredentials,
    DoubaoFrameCodec,
    DoubaoProtocolError,
    Frame,
    TranscriptEvent,
    auth_headers,
    build_request_meta,
    encode_audio,
    encode_full_request,
    extract_full_text,
    extract_text,
)

__all__ = [
    "AudioCapture",
    "AudioCaptureError",
    "AudioConfig",
    "DoubaoClient",
    "DoubaoCredentials",
    "DoubaoFrameCodec",
    "DoubaoProtocolError",
    "Frame",
    "State",
    "TranscriptEvent",
    "TranscriptUpdate",
    "VoiceController",
    "auth_headers",
    "build_request_meta",
    "encode_audio",
    "encode_full_request",
    "extract_full_text",
    "extract_text",
]
