"""Persistent configuration for Spitch.

The config file lives at ``$XDG_CONFIG_HOME/spitch/config.json`` (default
``~/.config/spitch/config.json``). It stores Doubao credentials and a small
amount of audio / hotkey state. Writes are atomic — we mkstemp a sibling in
the same directory, fsync, then ``os.replace`` it onto the final path — and
the file is chmod 600 so that the access keys are not world-readable.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


class ConfigError(Exception):
    """Raised when the on-disk config is malformed."""


DEFAULT_CONFIG: dict[str, Any] = {
    "provider": "doubao",
    "doubao": {
        "app_key": "",
        "access_key": "",
        "resource_id": "volc.bigasr.sauc.duration",
        "endpoint": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel",
    },
    "audio": {
        "device": None,
        "sample_rate": 16000,
        # Continuous-capture pre-buffer length in ms. The mic is opened
        # at daemon start and audio fills a ring of this many ms; on
        # press we replay the ring into the session so the first words
        # are not lost to mic-startup latency (PortAudio + ALSA combine
        # for a 50–500 ms gap before PCM actually flows). 500 ms is
        # safe across desktops. Set to 0 to fall back to the legacy
        # "open mic on press, close on release" behavior — gives back
        # the always-on-mic but loses the head of every utterance.
        "prebuffer_ms": 500,
    },
    "hotkey": {
        "talk_key": "Ctrl+Alt",
    },
    "inject": {
        # Synthetic keystroke used to paste the final transcript into the
        # focused app. ``Ctrl+Shift+V`` works in terminals (where Ctrl+V
        # is the literal-quote escape, not paste), browsers, address
        # bars, Slack, Feishu, Word, Google Docs. Set to ``Ctrl+V`` if
        # you primarily voice-type into VSCode-on-markdown (where
        # Ctrl+Shift+V is bound to "Open Preview").
        "paste_keystroke": "Ctrl+Shift+V",
        # How long to wait after sending the paste keystroke before
        # restoring the user's prior clipboard contents. Chromium /
        # Electron apps (Claude Code, VS Code, Slack, Feishu, Discord)
        # read the clipboard via an *async Promise* that can take
        # 300–700 ms to resolve on long CJK strings; if we restore too
        # early they read the OLD clipboard and paste only part (or
        # none) of the recognized text. 800 ms is safe for the slowest
        # Electron app we've tested. Lower it to 200–300 if you only
        # ever paste into traditional GTK/Qt apps that read the
        # clipboard synchronously.
        "restore_clipboard_delay_ms": 800,
        # How long to wait for the server's final transcript after
        # the user releases the talk key. With a flaky network this
        # may be longer than feels intuitive: ws connect retries
        # alone can eat 10 s of backoff, then the audio still has
        # to go out and the server has to respond. 30 s is generous
        # enough that "released the key, waited a bit, text appeared"
        # works even on bad wifi without forcing the user to redo
        # the dictation. Set lower if you want failures to surface
        # faster instead of waiting it out.
        "final_wait_seconds": 30.0,
    },
    "verified_at": None,
    "verified_signature": None,
}


def config_dir() -> Path:
    """Return the spitch config directory, honoring ``$XDG_CONFIG_HOME``."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "spitch"


def config_path() -> Path:
    """Return the full path to ``config.json``."""
    return config_dir() / "config.json"


def default_config() -> dict[str, Any]:
    """Return a fresh deep copy of :data:`DEFAULT_CONFIG`."""
    return copy.deepcopy(DEFAULT_CONFIG)


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {k: copy.deepcopy(v) for k, v in base.items()}
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, Mapping):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load config, merging with :data:`DEFAULT_CONFIG` for any missing keys.

    Returns a fresh deep copy of :data:`DEFAULT_CONFIG` when the file is
    missing. Raises :class:`ConfigError` on malformed JSON or non-object
    contents.
    """
    p = Path(path) if path is not None else config_path()
    if not p.exists():
        return default_config()
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config at {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config at {p} must be a JSON object, got {type(data).__name__}")
    return _deep_merge(DEFAULT_CONFIG, data)


def save_config(cfg: Mapping[str, Any], path: Path | str | None = None) -> Path:
    """Atomically persist ``cfg`` and chmod the result to 600.

    Strategy: ``mkstemp`` a sibling of the target in the same directory, write
    + fsync, then ``os.replace`` onto the final path. The temp file is
    cleaned up on error so we never leave ``config.json.*.tmp`` behind.
    """
    p = Path(path) if path is not None else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmpname = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=p.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmpname, p)
    except Exception:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
    # os.replace preserves the source mode but be defensive in case the target
    # already existed with looser perms on some filesystems.
    os.chmod(p, 0o600)
    return p


def _signature_fingerprint(sig: tuple) -> str:
    """Stable short hash of a credentials signature, for storing alongside verified_at."""
    import hashlib
    raw = "|".join("" if v is None else str(v) for v in sig).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def is_complete(cfg: Mapping[str, Any]) -> bool:
    """True iff the user has supplied the credentials needed to talk to Doubao.

    A complete config has ``provider == "doubao"`` and non-empty
    ``app_key``, ``access_key``, and ``endpoint``. This is a *necessary*
    but not sufficient condition for voice usage — see :func:`is_verified`.
    """
    if cfg.get("provider") != "doubao":
        return False
    d = cfg.get("doubao") or {}
    if not isinstance(d, Mapping):
        return False
    return bool(d.get("app_key")) and bool(d.get("access_key")) and bool(d.get("endpoint"))


def is_verified(cfg: Mapping[str, Any]) -> bool:
    """True iff the config is complete *and* a successful probe stamped *these*
    credentials.

    GOAL.md requires the IME tell the user whether configuration succeeded
    *before* allowing voice usage ("成功后就可以使用了"). The probe writes
    ``verified_at`` only after a live Doubao handshake succeeded, and the
    write also fingerprints the credentials it verified into
    ``verified_signature``. If the user later edits the config (via the
    dialog or by hand) to change keys, the fingerprint stops matching and
    the gate closes again — preventing an old "good" stamp from
    laundering new untested credentials.

    Configs written by older Spitch builds that lack ``verified_signature``
    are treated as verified iff ``verified_at`` is set; this keeps the
    upgrade path painless without ever weakening the new gate.
    """
    if not is_complete(cfg):
        return False
    stamp = cfg.get("verified_at")
    if not (isinstance(stamp, str) and stamp.strip()):
        return False
    sig_fp = cfg.get("verified_signature")
    if isinstance(sig_fp, str) and sig_fp.strip():
        return sig_fp == _signature_fingerprint(credentials_signature(cfg))
    return True


def credentials_signature(cfg: Mapping[str, Any]) -> tuple:
    """Return a tuple identifying which credentials are stamped by ``verified_at``.

    Two configs whose signatures differ describe different Doubao endpoints
    or keys; in that case any old ``verified_at`` no longer applies.
    """
    d = cfg.get("doubao") or {}
    if not isinstance(d, Mapping):
        d = {}
    return (
        cfg.get("provider"),
        d.get("app_key"),
        d.get("access_key"),
        d.get("resource_id"),
        d.get("endpoint"),
    )


def mark_verified(cfg: Mapping[str, Any], when: datetime | None = None) -> dict[str, Any]:
    """Return a deep copy of ``cfg`` with ``verified_at`` and
    ``verified_signature`` stamped.

    A naive ``datetime`` is interpreted as UTC. The signature fingerprint
    binds the stamp to the current credentials so a later credential edit
    invalidates the verification automatically.
    """
    out = copy.deepcopy(dict(cfg))
    moment = when or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    out["verified_at"] = moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["verified_signature"] = _signature_fingerprint(credentials_signature(out))
    return out


def clear_verified(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``cfg`` with verification fields cleared."""
    out = copy.deepcopy(dict(cfg))
    out["verified_at"] = None
    out["verified_signature"] = None
    return out
