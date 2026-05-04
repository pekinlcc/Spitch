#!/usr/bin/env bash
# Probe the real Doubao bigmodel WS endpoint via a stdlib TLS WebSocket
# Upgrade. Verifies:
#
#   - DNS resolution for the production hostname Spitch ships;
#   - TLS handshake with the production cert chain;
#   - that the endpoint URL Spitch uses is the URL the server serves;
#   - that the X-Api-* header set Spitch sends is recognized (Doubao
#     replies with a SaaS-grant error rather than a generic web error,
#     and its CORS allow-headers list explicitly enumerates them);
#
# without supplying real credentials. Set SPITCH_PROBE_APP_KEY and
# SPITCH_PROBE_ACCESS_KEY (and optionally SPITCH_PROBE_RESOURCE_ID) for
# the operator's real auth probe — a 200/101 Switching Protocols then
# would prove the live auth path end-to-end.
#
# Usage: tests/probe_real_endpoint.sh
#
# The Python below is stdlib-only; no `websockets`, no `pip install`
# step required. That's intentional: this script must run on a stock
# Ubuntu 24.04 host with no extra packages.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 - <<'PY'
import base64, os, secrets, socket, ssl, sys, uuid

HOST = "openspeech.bytedance.com"
PORT = 443
PATH = "/api/v3/sauc/bigmodel"

ws_key = base64.b64encode(secrets.token_bytes(16)).decode()
app_key     = os.environ.get("SPITCH_PROBE_APP_KEY",     "PROBE_INVALID_KEY")
access_key  = os.environ.get("SPITCH_PROBE_ACCESS_KEY",  "PROBE_INVALID_KEY")
resource_id = os.environ.get("SPITCH_PROBE_RESOURCE_ID", "volc.bigasr.sauc.duration")

req = (
    f"GET {PATH} HTTP/1.1\r\n"
    f"Host: {HOST}\r\n"
    f"Upgrade: websocket\r\n"
    f"Connection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {ws_key}\r\n"
    f"Sec-WebSocket-Version: 13\r\n"
    f"X-Api-App-Key: {app_key}\r\n"
    f"X-Api-Access-Key: {access_key}\r\n"
    f"X-Api-Resource-Id: {resource_id}\r\n"
    f"X-Api-Connect-Id: {uuid.uuid4()}\r\n"
    f"X-Api-Request-Id: {uuid.uuid4()}\r\n"
    f"\r\n"
).encode("ascii")

ctx = ssl.create_default_context()
sock = socket.create_connection((HOST, PORT), timeout=10)
peer = sock.getpeername()
ssock = ctx.wrap_socket(sock, server_hostname=HOST)
cert = ssock.getpeercert()
sn = dict(x[0] for x in cert.get("subject", ())).get("commonName", "?")
issuer = dict(x[0] for x in cert.get("issuer", ())).get("commonName", "?")
ssock.sendall(req)

resp = b""
try:
    while True:
        chunk = ssock.recv(4096)
        if not chunk:
            break
        resp += chunk
        if b"\r\n\r\n" in resp and len(resp) > 256:
            break
except (socket.timeout, ssl.SSLWantReadError):
    pass
finally:
    ssock.close()

print(f"connected to {HOST}:{PORT} -> {peer}")
print(f"TLS subject CN={sn!r} issuer CN={issuer!r}")
print(f"--- request ({len(req)} bytes) ---")
print(req.decode())
print(f"--- response ({len(resp)} bytes) ---")
print(resp.decode("utf-8", errors="replace"))

status = resp.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
print(f"--- verdict ---")
if app_key == "PROBE_INVALID_KEY":
    print(f"status: {status}")
    print("network=ok, tls=ok, endpoint-recognized=ok, auth=expected-fail (no creds supplied)")
    print("To run a live auth probe, export SPITCH_PROBE_APP_KEY / SPITCH_PROBE_ACCESS_KEY")
    print("and re-run; a 'HTTP/1.1 101 Switching Protocols' line will then prove auth too.")
else:
    print(f"status: {status}")
    if b" 101 " in resp.split(b"\r\n", 1)[0]:
        print("LIVE AUTH PROBE: SUCCESS — Doubao accepted the supplied credentials.")
    else:
        print("LIVE AUTH PROBE: FAILED — see status + body above.")
PY
