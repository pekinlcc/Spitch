"""Tests for :mod:`spitch.config`."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from spitch.config import (
    DEFAULT_CONFIG,
    ConfigError,
    clear_verified,
    config_dir,
    config_path,
    credentials_signature,
    default_config,
    is_complete,
    is_verified,
    load_config,
    mark_verified,
    save_config,
)


class ConfigPathTests(unittest.TestCase):
    def test_xdg_config_home_overrides(self):
        prev = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = "/tmp/spitch-test-xdg"
        try:
            self.assertEqual(config_dir(), Path("/tmp/spitch-test-xdg/spitch"))
            self.assertEqual(
                config_path(), Path("/tmp/spitch-test-xdg/spitch/config.json")
            )
        finally:
            if prev is None:
                del os.environ["XDG_CONFIG_HOME"]
            else:
                os.environ["XDG_CONFIG_HOME"] = prev

    def test_default_path_under_home(self):
        prev = os.environ.pop("XDG_CONFIG_HOME", None)
        try:
            self.assertEqual(config_dir(), Path.home() / ".config" / "spitch")
        finally:
            if prev is not None:
                os.environ["XDG_CONFIG_HOME"] = prev


class LoadConfigTests(unittest.TestCase):
    def test_missing_returns_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            cfg = load_config(p)
            self.assertEqual(cfg, DEFAULT_CONFIG)

    def test_returned_config_is_a_copy(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nope.json"
            cfg = load_config(p)
            cfg["doubao"]["app_key"] = "MUTATED"
            self.assertEqual(DEFAULT_CONFIG["doubao"]["app_key"], "")

    def test_partial_merges_with_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            p.write_text(
                json.dumps({"doubao": {"app_key": "AK"}}), encoding="utf-8"
            )
            cfg = load_config(p)
            self.assertEqual(cfg["doubao"]["app_key"], "AK")
            # untouched defaults preserved
            self.assertEqual(cfg["doubao"]["access_key"], "")
            self.assertEqual(cfg["doubao"]["endpoint"], DEFAULT_CONFIG["doubao"]["endpoint"])
            self.assertEqual(cfg["audio"]["sample_rate"], 16000)
            self.assertEqual(cfg["hotkey"]["talk_key"], "Ctrl+Alt")
            self.assertEqual(cfg["provider"], "doubao")

    def test_invalid_json_raises_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not valid", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)

    def test_non_object_raises_config_error(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "list.json"
            p.write_text("[1,2,3]", encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(p)


class SaveConfigTests(unittest.TestCase):
    def test_round_trip_and_perms(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sub" / "config.json"
            cfg = default_config()
            cfg["doubao"]["app_key"] = "AK"
            cfg["doubao"]["access_key"] = "SK"
            saved = save_config(cfg, p)
            self.assertTrue(saved.exists())
            mode = stat.S_IMODE(saved.stat().st_mode)
            self.assertEqual(mode, 0o600)
            reloaded = load_config(p)
            self.assertEqual(reloaded["doubao"]["app_key"], "AK")
            self.assertEqual(reloaded["doubao"]["access_key"], "SK")

    def test_no_temp_files_left_behind(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            save_config(default_config(), p)
            leftover = sorted(os.listdir(td))
            self.assertEqual(leftover, ["config.json"])

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "config.json"
            save_config({"provider": "doubao", "doubao": {"app_key": "A"}}, p)
            save_config({"provider": "doubao", "doubao": {"app_key": "B"}}, p)
            self.assertEqual(load_config(p)["doubao"]["app_key"], "B")

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nested" / "deeper" / "config.json"
            save_config(default_config(), p)
            self.assertTrue(p.exists())


class IsCompleteTests(unittest.TestCase):
    def test_default_is_incomplete(self):
        self.assertFalse(is_complete(default_config()))

    def test_with_creds_is_complete(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertTrue(is_complete(cfg))

    def test_missing_endpoint_incomplete(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["doubao"]["endpoint"] = ""
        self.assertFalse(is_complete(cfg))

    def test_wrong_provider_incomplete(self):
        cfg = default_config()
        cfg["provider"] = "other"
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertFalse(is_complete(cfg))


class MarkVerifiedTests(unittest.TestCase):
    def test_sets_iso_z_for_aware_utc(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0, tzinfo=timezone.utc)
        out = mark_verified(cfg, moment)
        self.assertEqual(out["verified_at"], "2026-05-03T14:00:00Z")

    def test_does_not_mutate_input(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0, tzinfo=timezone.utc)
        mark_verified(cfg, moment)
        self.assertIsNone(cfg["verified_at"])

    def test_naive_datetime_treated_as_utc(self):
        cfg = default_config()
        moment = datetime(2026, 5, 3, 14, 0, 0)
        out = mark_verified(cfg, moment)
        self.assertEqual(out["verified_at"], "2026-05-03T14:00:00Z")


class IsVerifiedTests(unittest.TestCase):
    def _stamped(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        return mark_verified(cfg)

    def test_default_not_verified(self):
        self.assertFalse(is_verified(default_config()))

    def test_complete_but_unstamped_not_verified(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        self.assertTrue(is_complete(cfg))
        self.assertFalse(is_verified(cfg))

    def test_stamped_complete_is_verified(self):
        self.assertTrue(is_verified(self._stamped()))

    def test_stamped_but_incomplete_not_verified(self):
        cfg = mark_verified(default_config())
        # creds still empty
        self.assertFalse(is_complete(cfg))
        self.assertFalse(is_verified(cfg))

    def test_clear_drops_verified(self):
        cfg = self._stamped()
        cleared = clear_verified(cfg)
        self.assertIsNone(cleared["verified_at"])
        self.assertFalse(is_verified(cleared))
        # original untouched
        self.assertTrue(is_verified(cfg))

    def test_empty_string_stamp_is_not_verified(self):
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["verified_at"] = "   "
        self.assertFalse(is_verified(cfg))

    def test_signature_change_invalidates_stamp(self):
        # Stamp is good for these creds…
        cfg = self._stamped()
        self.assertTrue(is_verified(cfg))
        # …but if someone hand-edits the access key, the gate closes.
        cfg["doubao"]["access_key"] = "rotated"
        self.assertFalse(is_verified(cfg))

    def test_legacy_stamp_without_signature_still_verified(self):
        # Older Spitch builds wrote verified_at without verified_signature.
        cfg = default_config()
        cfg["doubao"]["app_key"] = "x"
        cfg["doubao"]["access_key"] = "y"
        cfg["verified_at"] = "2026-05-03T14:00:00Z"
        # No verified_signature key at all — treat as verified.
        self.assertTrue(is_verified(cfg))


class CredentialsSignatureTests(unittest.TestCase):
    def test_signature_changes_with_credentials(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        b = default_config()
        b["doubao"]["app_key"] = "different"
        self.assertNotEqual(credentials_signature(a), credentials_signature(b))

    def test_signature_stable_across_unrelated_changes(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        a["audio"]["sample_rate"] = 16000
        b = default_config()
        b["doubao"]["app_key"] = "AK"
        b["audio"]["sample_rate"] = 22050  # different audio knob
        b["hotkey"]["talk_key"] = "F3"     # different hotkey
        self.assertEqual(credentials_signature(a), credentials_signature(b))

    def test_resource_or_endpoint_change_invalidates_signature(self):
        a = default_config()
        a["doubao"]["app_key"] = "AK"
        b = default_config()
        b["doubao"]["app_key"] = "AK"
        b["doubao"]["resource_id"] = "different.resource"
        self.assertNotEqual(credentials_signature(a), credentials_signature(b))


if __name__ == "__main__":
    unittest.main()
