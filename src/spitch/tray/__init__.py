"""GNOME / system-tray status indicator (libayatana-appindicator).

Surfaces the daemon state in the top-right status area. Optional —
falls back to no-op if the AppIndicator typelib is not installed.
"""

from .indicator import SpitchIndicator, try_create

__all__ = ["SpitchIndicator", "try_create"]
