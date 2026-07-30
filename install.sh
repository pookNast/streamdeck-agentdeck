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
# Match the interpreter the unit ExecStart hardcodes (/usr/bin/python3): a bare
# PATH `python3` check could pass against a different interpreter (e.g. a pyenv
# shim) while the service still fails to import. Fall back to PATH python3 only
# if /usr/bin/python3 isn't present (non-FHS distros). (FIX E2)
PY=/usr/bin/python3
command -v "$PY" >/dev/null 2>&1 || PY=python3
if ! "$PY" -c 'import StreamDeck' 2>/dev/null; then
  # PEP 668 systems (Ubuntu 24.04+) need an explicit override or a venv.
  # FATAL: without this library deck.py can't import StreamDeck and the enabled
  # service hits its start-limit lockout. Abort here (set -e) BEFORE enabling
  # the unit, instead of exiting 0 via a trailing `|| echo`.
  pip install --user streamdeck 2>/dev/null \
    || pip install --user --break-system-packages streamdeck \
    || { echo "!! install 'streamdeck' manually (pip/pipx) — import StreamDeck failed" >&2; exit 1; }
fi

echo "==> Verify runtime deps (PIL + tmux — distro-agnostic)"
# The apt branch above installs python3-pil + tmux, but a non-Debian host (or
# one where apt-get isn't in PATH) silently skips them and the enabled service
# can't import PIL / can't drive tmux sessions. Verify against the SAME python
# the unit ExecStarts ($PY) and fail loudly BEFORE enabling the unit — mirrors
# the StreamDeck import gate above. Distro-flexible: no apt hardcode, just the
# import/availability check. F12: never let the service start import-blind.
if ! "$PY" -c 'import PIL' 2>/dev/null; then
  echo "!! PIL (Pillow) not importable by $PY — install python3-pil / python-pillow for this distro" >&2
  exit 1
fi
if ! command -v tmux >/dev/null 2>&1; then
  echo "!! tmux not found on PATH — install it for this distro" >&2
  exit 1
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
# Guard the whole copy block: when run from the install target itself
# (SRC == DEST — the obvious clone path the unit hardcodes) `install` aborts
# on same-file. Skip the self-copy; files are already in place. (FIX E3)
if ! [[ "$SRC" -ef "$DEST" ]]; then
  install -m 0755 "$SRC/deck.py" "$SRC/ghibli_scenes.py" "$SRC/ghibli_beach.py" "$DEST/"
  cp -r "$SRC/core" "$DEST/"
  install -m 0644 "$SRC/config.example.toml" "$DEST/"
else
  echo "   SRC == DEST ($SRC); self-copy skipped (files already in place)"
fi

echo "==> systemd --user service"
mkdir -p "$USER_UNIT_DIR"
install -m 0644 "$SRC/systemd/$UNIT" "$USER_UNIT_DIR/$UNIT"
# enable-linger keeps the --user service alive after logout (survive-reboot
# guarantee). It can need root on some systems; keep it non-fatal but VISIBLE
# so a silent failure doesn't mask the survive-reboot guarantee. (FIX E4)
loginctl enable-linger "$USER" \
  || echo "  warn: enable-linger failed (may need sudo / root); service won't survive logout until enabled" >&2
systemctl --user daemon-reload
# enable --now only STARTS when not already running, so re-running install.sh
# to deploy a code fix would leave the OLD process live. `restart` both starts
# a freshly-installed (inactive) unit and restarts an already-running one, so
# a re-install picks up the new code. (FIX E1)
systemctl --user enable "$UNIT"
systemctl --user restart "$UNIT"

echo
echo "Done. Status:"
systemctl --user --no-pager status "$UNIT" | head -5 || true
echo "Logs: journalctl --user -u streamdeck-agentdeck -f"
