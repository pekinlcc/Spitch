# Installing Spitch on Ubuntu 24.04

Spitch v0.2 ships as a Python source tree with a user-level install
script. There is no compilation step — everything runs from Python and
the system tools `wl-copy` (clipboard) and `/dev/uinput` (key
injection) that ship with stock Ubuntu plus one apt package.

## 1. System packages

```bash
sudo apt-get install -y python3-evdev wl-clipboard
```

* `python3-evdev` — read keyboard events from `/dev/input/event*`
  (global hotkey detection) and write to `/dev/uinput` (synthetic
  Ctrl+V).
* `wl-clipboard` — provides `wl-copy` / `wl-paste` for clipboard I/O.

Optional but recommended:

```bash
sudo apt-get install -y libnotify-bin   # adds desktop notifications
```

PyGObject (for the GTK config dialog) is supplied by the system
package `python3-gi` if you want a GUI; otherwise `spitch-config`
falls back to a CLI prompt.

## 2. Install Spitch

From a clone of the repo:

```bash
cd /path/to/Spitch
./scripts/install.sh
```

This installs `~/.local/bin/spitch-daemon` and
`~/.local/bin/spitch-config` launchers. Both point `PYTHONPATH` at
this repo's `src/`, so no `pip install` is required.

If `~/.local/bin` is not on your `PATH`, add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
exec bash
```

## 3. Configure Doubao credentials

```bash
spitch-config
```

A small GTK dialog opens (or a CLI fallback if PyGObject is missing).

| Field            | What it is                                                    |
|------------------|---------------------------------------------------------------|
| X-Api-App-Key    | Volcano Engine BigASR **APP ID** from the console            |
| X-Api-Access-Key | Volcano Engine BigASR **Access Token** from the console      |
| Resource ID      | Default `volc.bigasr.sauc.duration` (BigModel realtime)       |
| WS endpoint      | Default `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| Audio sample rate| 16000 (recommended)                                           |
| Push-to-talk key | Hold this combo to record. Default `Ctrl+Alt`.                |

The console's "Secret Key" is **not** used by this realtime endpoint —
leave it where it is.

Click **Test connection**. The dialog opens a real WebSocket to Doubao
using your credentials and verifies that the server accepts the
handshake. Only on success is the config saved (chmod 600) at
`~/.config/spitch/config.json`.

## 4. Grant `/dev/input` read access

The daemon listens for the global hotkey by reading
`/dev/input/event*`. On stock Ubuntu these are `root:input 0660`, so
your user needs to be in the `input` group:

```bash
sudo usermod -aG input $USER
```

Log out and back in for the new group membership to take effect. To
test without logging out, run the daemon under `sg` once:

```bash
sg input -c 'spitch-daemon'
```

## 5. Run the daemon

```bash
spitch-daemon &
```

You should see a "Spitch ready" desktop notification. Test it:

* Focus any text input (browser address bar, gedit, terminal, Feishu,
  VS Code, Slack, …).
* Hold the configured talk key (default Ctrl+Alt — both modifiers,
  no third key). A "🎙 Spitch listening…" notification appears.
* Speak. Release. A "✍ Spitch finalizing…" notification appears
  briefly, then the punctuated final transcript is pasted into the
  focused field.

If you press a third key during the chord (e.g. Ctrl+Alt+T to launch
a terminal), the recording is automatically cancelled and the
shortcut passes through normally.

## 6. (Optional) auto-start at login

Create a systemd user unit at `~/.config/systemd/user/spitch.service`:

```ini
[Unit]
Description=Spitch voice-input daemon
After=graphical-session.target

[Service]
ExecStart=%h/.local/bin/spitch-daemon
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
```

Then:

```bash
systemctl --user daemon-reload
systemctl --user enable --now spitch.service
journalctl --user -u spitch.service -f    # tail logs
```

## Troubleshooting

* **"no readable keyboard devices found"** when launching the daemon
  → your user is not in the `input` group, or the new membership has
  not taken effect. Verify with `id | grep input`; if missing, run
  the `usermod` from step 4 and log out / back in.
* **Pasting fails (focused app shows nothing)** → check
  `/dev/uinput` is writable: `getfacl /dev/uinput | grep $USER`.
  On Ubuntu 24.04 logind sets the ACL automatically; on other
  distros you may need a udev rule.
* **Hotkey does nothing** → tail
  `${XDG_STATE_HOME:-$HOME/.local/state}/spitch/daemon.log`. If you
  see no key events at all when pressing Ctrl+Alt, the daemon is not
  reading the right device — confirm with
  `cat /proc/bus/input/devices` that your keyboard is enumerated.
* **Clipboard contents are surprising afterwards** → Spitch saves
  and restores the clipboard around each paste, with a 0.3 s
  settle delay. If your focused app is slow to consume the paste,
  the saved clipboard may overwrite Spitch's text. Increase
  `time.sleep(0.3)` in `src/spitch/inject/text_injector.py`.

## Uninstall

```bash
./scripts/uninstall.sh
```

Removes the launcher scripts and any systemd unit. Does **not** remove
`~/.config/spitch/` (your saved Doubao credentials).
