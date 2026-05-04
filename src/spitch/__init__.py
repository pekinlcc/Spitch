"""Spitch — global-hotkey voice input for Linux desktops, powered by
Doubao (Volcano Engine) realtime ASR.

Architecture is IM-framework-independent: a long-lived user daemon
listens for the configured hotkey via /dev/input/event*, streams
captured audio to Doubao, and injects the final transcript into the
focused application via clipboard + a synthesized Ctrl+V from
/dev/uinput. Works in any GTK / Qt / Electron / native-Wayland app on
Wayland and X11 alike, regardless of whether IBus, fcitx5, or no IM is
configured.
"""

__version__ = "0.4.7"
