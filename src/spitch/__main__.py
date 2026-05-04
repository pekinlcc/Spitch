"""Entry point: ``python3 -m spitch`` runs the Spitch daemon.

The daemon listens for the configured global hotkey (default
``Ctrl+Alt``), streams captured audio to Doubao for realtime ASR, and
injects the final transcript into the focused app via clipboard +
synthetic Ctrl+V. It is IM-framework-independent — works in any
GTK / Qt / Electron / native-Wayland app on Wayland and X11 alike.
"""

from __future__ import annotations

import sys

from . import daemon


def main(argv: list[str] | None = None) -> int:
    return daemon.main()


if __name__ == "__main__":
    sys.exit(main())
