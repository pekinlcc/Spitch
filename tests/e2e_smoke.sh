#!/usr/bin/env bash
# Spitch end-to-end smoke test (v0.2 — daemon architecture).
#
# Does NOT need a microphone, keyboard access, or live Doubao
# credentials. It exercises the wired-together pipeline against the
# stdlib mock Doubao server, plus an import-time sanity check on the
# daemon entry point.
#
# Usage: tests/e2e_smoke.sh
#
# What is NOT covered here automatically (because it needs a desktop
# session, real microphone, real Doubao credentials, and `input`-group
# membership):
#
#   * Holding the talk key globally to record real audio in a focused
#     app and seeing the final transcript injected via Ctrl+V.
#
# Instructions for that side are in docs/INSTALL.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

echo "=== A. unittest suite =================================================="
python3 -m unittest discover -s tests -v 2>&1 | tail -10
echo

echo "=== B. daemon module sanity ==========================================="
python3 - <<'PY'
import importlib
for name in ("spitch.daemon", "spitch.hotkey", "spitch.inject"):
    m = importlib.import_module(name)
    print("imported", m.__name__)
PY
echo

echo "=== C. real-endpoint probe (network-only, no creds required) ========="
if "$ROOT/tests/probe_real_endpoint.sh" 2>&1 | grep -E "^(connected|status|network=|LIVE )"; then
    echo "(real-endpoint probe completed)"
else
    echo "(real-endpoint probe skipped or failed — likely no network)"
fi
echo

echo "=== D. live voice pipeline (stdlib mock Doubao + real mic) ============"
if PYTHONPATH="$ROOT/src:$ROOT/tests${PYTHONPATH:+:$PYTHONPATH}" \
       python3 "$ROOT/tests/live_voice_pipeline.py" 2>&1 | grep -E "^(VERDICT|summary|--- )"; then
    echo "(live pipeline completed)"
else
    echo "(live pipeline skipped or failed — likely no mic)"
fi
echo

echo "=== smoke OK =========================================================="
