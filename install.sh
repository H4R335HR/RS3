#!/bin/bash
# install.sh — Install RadioScrobbler3 as a standalone MPRIS daemon
#
# Usage:
#   chmod +x install.sh
#   ./install.sh

set -e

CONFIG_DIR="$HOME/.config/rs3"
DATA_DIR="$HOME/.local/share/rs3"
SERVICE_DIR="$HOME/.config/systemd/user"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "╔══════════════════════════════════════════╗"
echo "║   RadioScrobbler3 — MPRIS Daemon Setup   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# --- Create directories ---
echo "→ Creating directories..."
mkdir -p "$CONFIG_DIR"
mkdir -p "$DATA_DIR"
mkdir -p "$SERVICE_DIR"

# --- Copy Python scripts ---
echo "→ Installing Python scripts to $CONFIG_DIR..."
cp "$SCRIPT_DIR/rs3_daemon.py" "$CONFIG_DIR/"
cp "$SCRIPT_DIR/rs3_core.py"   "$CONFIG_DIR/"
cp "$SCRIPT_DIR/S3.py"         "$CONFIG_DIR/"

# --- Copy config (don't overwrite existing) ---
if [ -f "$CONFIG_DIR/config.ini" ]; then
    echo "→ config.ini already exists — skipping (your settings are preserved)"
else
    echo "→ Installing default config.ini..."
    cp "$SCRIPT_DIR/config.ini" "$CONFIG_DIR/"
    echo ""
    echo "  ⚠  IMPORTANT: Edit $CONFIG_DIR/config.ini"
    echo "     and add your Last.fm API credentials!"
    echo ""
fi

# --- Copy patterns and rules (don't overwrite existing) ---
for f in patterns rules; do
    if [ -f "$SCRIPT_DIR/$f" ]; then
        if [ -f "$CONFIG_DIR/$f" ]; then
            echo "→ $f already exists — skipping"
        else
            cp "$SCRIPT_DIR/$f" "$CONFIG_DIR/"
            echo "→ Installed $f"
        fi
    fi
done

# --- Install systemd service ---
echo "→ Installing systemd user service..."
cp "$SCRIPT_DIR/rs3.service" "$SERVICE_DIR/"
systemctl --user daemon-reload

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║             Installation done!           ║"
echo "╚══════════════════════════════════════════╝"
echo ""
echo "Next steps:"
echo ""
echo "  1. Edit your config:"
echo "     \$ nano $CONFIG_DIR/config.ini"
echo ""
echo "  2. Start the daemon:"
echo "     \$ systemctl --user start rs3"
echo ""
echo "  3. Enable auto-start on login:"
echo "     \$ systemctl --user enable rs3"
echo ""
echo "  4. View logs:"
echo "     \$ journalctl --user -u rs3 -f"
echo ""
echo "  5. To run manually (for debugging):"
echo "     \$ python3 $CONFIG_DIR/rs3_daemon.py"
echo ""
