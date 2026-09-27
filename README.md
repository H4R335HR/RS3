# RadioScrobbler3 (RS3)

Scrobbles internet radio tracks to Last.fm. Works with any MPRIS-compatible media player via D-Bus.

## How it works

RS3 runs as a systemd user daemon, monitors your media player over MPRIS/D-Bus, and automatically identifies and scrobbles tracks using:

- **Simple split** — standard `Artist - Title` parsing
- **Pattern matching** — regex-based rules for unusual title formats
- **Preset rules** — per-station overrides for known stations
- **Shazam fallback** — audio fingerprinting when metadata is insufficient
- **Last.fm corrections** — fixes minor spelling mistakes via Last.fm's API

## Requirements

- Python 3
- `python-gobject` (GLib/Gio/D-Bus bindings)
- `charset-normalizer`
- A Last.fm API account

## Install

```bash
chmod +x install.sh
./install.sh
```

Then edit `~/.config/rs3/config.ini` with your Last.fm API credentials.

## Update

```bash
git pull
./install.sh
systemctl --user restart rs3
```

The installer will update the Python scripts but **won't overwrite** your existing `config.ini`, so your API keys and settings are preserved. If a new release adds config options, add them manually to `~/.config/rs3/config.ini`.

## Usage

```bash
# Start the daemon
systemctl --user start rs3

# Enable on login
systemctl --user enable rs3

# View logs
journalctl --user -u rs3 -f

# Run manually (for debugging)
python3 ~/.config/rs3/rs3_daemon.py
```

## Files

| File | Purpose |
|------|---------|
| `rs3_daemon.py` | MPRIS D-Bus daemon — monitors players |
| `rs3_core.py` | Scrobbling engine — splitting, matching, scrobbling |
| `S3.py` | Last.fm API interface & utilities |
| `RS3.py` | Legacy Rhythmbox plugin (optional) |
| `config.ini` | API keys and settings (not tracked in git) |
| `install.sh` | Installer script |
| `rs3.service` | systemd user service unit |

## License

Copyright © 2012 harris4got
