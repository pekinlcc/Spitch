"""``spitch-cli`` — small command-line client for the running daemon.

Wraps :mod:`spitch.cmdsock` so users can:

  * ``spitch-cli repaste``           — re-paste the most recent transcript
  * ``spitch-cli repaste --index N`` — re-paste a specific history entry
  * ``spitch-cli list``              — print history (newest last)
  * ``spitch-cli clear``             — wipe history
  * ``spitch-cli ping``              — verify daemon is reachable

Useful by itself, but the main reason this exists is so users can bind
``spitch-cli repaste`` to a system shortcut (GNOME Settings → Custom
Shortcuts) and trigger a re-paste from anywhere without opening the
console.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from .cmdsock import call as cmd_call


def _print_err(msg: str) -> None:
    sys.stderr.write(f"spitch-cli: {msg}\n")


def _format_entry(idx: int, e: dict[str, Any]) -> str:
    text = e.get("text", "")
    if len(text) > 60:
        text = text[:57] + "…"
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("timestamp", 0)))
    flag = "✓" if e.get("inject_ok", True) else "✗"
    app = e.get("target_app") or "-"
    return f"[{idx:>3}] {when}  {flag}  ({app})  {text}"


def cmd_ping(args: argparse.Namespace) -> int:
    try:
        resp = cmd_call("ping")
    except ConnectionError as exc:
        _print_err(str(exc))
        return 2
    if resp.get("ok"):
        print(f"daemon ok, version={resp.get('version', '?')}")
        return 0
    _print_err(resp.get("error", "unknown error"))
    return 1


def cmd_list(args: argparse.Namespace) -> int:
    try:
        resp = cmd_call("list")
    except ConnectionError as exc:
        _print_err(str(exc))
        return 2
    if not resp.get("ok"):
        _print_err(resp.get("error", "unknown error"))
        return 1
    entries = resp.get("entries") or []
    if args.json:
        print(json.dumps(entries, ensure_ascii=False, indent=2))
        return 0
    if not entries:
        print("(no history)")
        return 0
    for i, e in enumerate(entries):
        print(_format_entry(i, e))
    return 0


def cmd_repaste(args: argparse.Namespace) -> int:
    try:
        resp = cmd_call("repaste", index=args.index)
    except ConnectionError as exc:
        _print_err(str(exc))
        return 2
    if not resp.get("ok"):
        _print_err(resp.get("error", "unknown error"))
        return 1
    preview = resp.get("text_preview", "")
    if preview:
        print(f"repaste scheduled: {preview}")
    return 0


def cmd_clear(args: argparse.Namespace) -> int:
    try:
        resp = cmd_call("clear")
    except ConnectionError as exc:
        _print_err(str(exc))
        return 2
    if not resp.get("ok"):
        _print_err(resp.get("error", "unknown error"))
        return 1
    print("history cleared")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    try:
        resp = cmd_call("delete", index=args.index)
    except ConnectionError as exc:
        _print_err(str(exc))
        return 2
    if not resp.get("ok"):
        _print_err(resp.get("error", "unknown error"))
        return 1
    print(f"deleted entry {args.index}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="spitch-cli",
        description=(
            "Talk to the running spitch-daemon over its Unix socket. "
            "Useful for binding 'repaste' to a system-level shortcut "
            "or scripting transcript management."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="check the daemon is reachable")

    list_p = sub.add_parser("list", help="show recent transcripts (oldest first)")
    list_p.add_argument(
        "--json", action="store_true",
        help="emit raw JSON instead of formatted lines",
    )

    repaste_p = sub.add_parser(
        "repaste",
        help="re-paste a history entry into the focused app (default: latest)",
    )
    repaste_p.add_argument(
        "--index", type=int, default=-1,
        help="chronological index; -1 (default) = latest, 0 = oldest in ring",
    )

    delete_p = sub.add_parser("delete", help="delete a history entry by index")
    delete_p.add_argument("index", type=int, help="chronological index to delete")

    sub.add_parser("clear", help="wipe all history")

    args = parser.parse_args(argv)
    handlers = {
        "ping":    cmd_ping,
        "list":    cmd_list,
        "repaste": cmd_repaste,
        "delete":  cmd_delete,
        "clear":   cmd_clear,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
