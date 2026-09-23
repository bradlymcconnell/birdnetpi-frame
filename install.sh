#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " AvianVisitors E-Ink Frame Installer & Restorer"
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="/home/birder"

# 1. Install Caveat font locally
echo "=== 1. Installing Handwritten Caveat Font ==="
mkdir -p "$HOME_DIR/.local/share/fonts"
if [ -f "$SCRIPT_DIR/fonts/Caveat.ttf" ]; then
  cp "$SCRIPT_DIR/fonts/Caveat.ttf" "$HOME_DIR/.local/share/fonts/"
  echo "✓ Installed Caveat.ttf to ~/.local/share/fonts/"
fi
fc-cache -fv "$HOME_DIR/.local/share/fonts" 2>/dev/null || true

# 2. Deploy User Config (if not already present)
echo "=== 2. Setting Up Frame Configuration ==="
mkdir -p "$HOME_DIR/.birdframe"
if [ ! -f "$HOME_DIR/.birdframe/config.toml" ] && [ -f "$SCRIPT_DIR/config/.birdframe/config.toml" ]; then
  cp "$SCRIPT_DIR/config/.birdframe/config.toml" "$HOME_DIR/.birdframe/config.toml"
  echo "✓ Installed initial config to ~/.birdframe/config.toml"
else
  echo "ℹ Preserving existing ~/.birdframe/config.toml"
fi

# 3. Deploy Helper Scripts
echo "=== 3. Deploying Master Scripts ==="
cp "$SCRIPT_DIR/scripts/patch-frame.sh" "$HOME_DIR/patch-frame.sh"
chmod +x "$HOME_DIR/patch-frame.sh"
echo "✓ Installed ~/patch-frame.sh"

if [ -f "$SCRIPT_DIR/scripts/backup-frame.sh" ]; then
  cp "$SCRIPT_DIR/scripts/backup-frame.sh" "$HOME_DIR/backup-frame.sh"
  chmod +x "$HOME_DIR/backup-frame.sh"
  echo "✓ Installed ~/backup-frame.sh"
fi

# 4. Run Master Re-Patching Script
echo "=== 4. Applying All Custom Performance & Driver Patches ==="
"$HOME_DIR/patch-frame.sh"

# 5. Check & Deploy Systemd Units
echo "=== 5. Setting Up Systemd Service & Timer ==="
if [ -f "$SCRIPT_DIR/systemd/birdframe.service" ]; then
  sudo cp "$SCRIPT_DIR/systemd/birdframe.service" /etc/systemd/system/
  sudo cp "$SCRIPT_DIR/systemd/birdframe.timer" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now birdframe.timer
  echo "✓ Enabled and started birdframe.timer"
fi

# 6. Check Boot Configuration for SPI Chip Select Overlay
echo "=== 6. Checking Hardware Boot Configuration ==="
BOOT_CONFIG="/boot/firmware/config.txt"
[ ! -f "$BOOT_CONFIG" ] && BOOT_CONFIG="/boot/config.txt"

if [ -f "$BOOT_CONFIG" ]; then
  if ! grep -q "^dtoverlay=spi0-0cs" "$BOOT_CONFIG"; then
    echo "ℹ Adding dtoverlay=spi0-0cs to $BOOT_CONFIG for Inky 13.3 software CS..."
    echo "dtoverlay=spi0-0cs" | sudo tee -a "$BOOT_CONFIG"
    echo "⚠ Note: A reboot is recommended to activate the SPI overlay if not already active."
  else
    echo "✓ $BOOT_CONFIG already configured with dtoverlay=spi0-0cs"
  fi
fi

# 7. Check /etc/hosts for birdnet.local resolution
if ! grep -q "birdnet.local" /etc/hosts; then
  echo "192.168.1.71 birdnet.local" | sudo tee -a /etc/hosts
  echo "✓ Added '192.168.1.71 birdnet.local' to /etc/hosts"
fi

echo "=========================================================="
echo " Installation Complete! Your frame is fully configured."
echo "=========================================================="
