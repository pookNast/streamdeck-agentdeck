#!/usr/bin/env bash
# Idempotent installer for streamdeck-agentdeck.
# Installs: udev rule (uaccess), Python deps, deck.py, and the systemd --user service.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/streamdeck-agentdeck"
UNIT="streamdeck-agentdeck.service"
USER_UNIT_DIR="$HOME/.config/systemd/user"

echo "==> System packages (libhidapi backend, Pillow, fonts; konsole tools optional)"
if command -v apt-get >/dev/null; then
  sudo apt-get update -qq
  # REQUIRED: hidapi backend + Pillow + fonts + tmux. NOT guarded by `|| true` —
  # without libhidapi the Stream Deck can't open at all, so fail loudly here.
  sudo apt-get install -y libhidapi-libusb0 python3-pil fonts-dejavu-core tmux
  # OPTIONAL: names vary by distro (qdbus-qt5 / qdbus-qt6 / qdbus); tolerate absence.
  sudo apt-get install -y qdbus-qt5 xdotool || true
fi

echo "==> Python StreamDeck library"
if ! python3 -c 'import StreamDeck' 2>/dev/null; then
  # PEP 668 systems (Ubuntu 24.04+) need an explicit override or a venv.
  pip install --user streamdeck 2>/dev/null \
    || pip install --user --break-system-packages streamdeck \
    || echo "!! install 'streamdeck' manually (pip/pipx) — import StreamDeck failed"
fi

echo "==> udev rule (uaccess for Stream Deck, vendor 0fd9)"
sudo install -m 0644 "$SRC/udev/70-streamdeck.rules" /etc/udev/rules.d/70-streamdeck.rules
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=usb
sudo udevadm trigger --action=add --subsystem-match=hidraw
echo "   (replug the Stream Deck if it was already connected)"

echo "==> runtime files -> $DEST"
mkdir -p "$DEST"
# Full runtime set: deck.py imports ghibli_scenes/ghibli_beach at module load
# and core/config.py at startup — a fresh install that copied only deck.py
# could not import (C2). Seed config.example.toml too (L2).
install -m 0755 "$SRC/deck.py" "$SRC/ghibli_scenes.py" "$SRC/ghibli_beach.py" "$DEST/"
cp -r "$SRC/core" "$DEST/"
install -m 0644 "$SRC/config.example.toml" "$DEST/"

echo "==> systemd --user service"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC/systemd/$UNIT" "$USER_UNIT_DIR/$UNIT"
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

echo
echo "Done. Status:"
systemctl --user --no-pager status "$UNIT" | head -5 || true
echo "Logs: journalctl --user -u streamdeck-agentdeck -f"
