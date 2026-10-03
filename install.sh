#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " AvianVisitors E-Ink Frame Installer & Restorer"
echo "=========================================================="

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="/home/birder"

# 1. Install Typography Suite locally
echo "=== 1. Installing Typography Font Suite ==="
mkdir -p "$HOME_DIR/.local/share/fonts"
mkdir -p "$HOME_DIR/AvianVisitors/frame/fonts"
if [ -d "$SCRIPT_DIR/fonts" ]; then
  cp "$SCRIPT_DIR/fonts/"*.ttf "$HOME_DIR/.local/share/fonts/" 2>/dev/null || true
  cp "$SCRIPT_DIR/fonts/"*.ttf "$HOME_DIR/AvianVisitors/frame/fonts/" 2>/dev/null || true
  echo "✓ Installed fonts to ~/.local/share/fonts/ and ~/AvianVisitors/frame/fonts/"
fi
fc-cache -fv "$HOME_DIR/.local/share/fonts" 2>/dev/null || true

# 2. Deploy Pre-Rendered Cartoon Assets (138 Species)
echo "=== 2. Deploying Pre-Rendered Cartoon Asset Library ==="
mkdir -p "$HOME_DIR/AvianVisitors/frame/assets/cartoon"
if [ -d "$SCRIPT_DIR/assets/cartoon" ]; then
  cp -r "$SCRIPT_DIR/assets/cartoon/"*.png "$HOME_DIR/AvianVisitors/frame/assets/cartoon/" 2>/dev/null || true
  echo "✓ Deployed cartoon species cutouts to ~/AvianVisitors/frame/assets/cartoon/"
fi

# 3. Deploy User Config (if not already present)
echo "=== 3. Setting Up Frame Configuration ==="
mkdir -p "$HOME_DIR/.birdframe"
if [ ! -f "$HOME_DIR/.birdframe/config.toml" ] && [ -f "$SCRIPT_DIR/config/.birdframe/config.toml" ]; then
  cp "$SCRIPT_DIR/config/.birdframe/config.toml" "$HOME_DIR/.birdframe/config.toml"
  echo "✓ Installed initial config to ~/.birdframe/config.toml"
else
  echo "ℹ Preserving existing ~/.birdframe/config.toml"
fi

# 4. Deploy Helper & Control Daemon Scripts
echo "=== 4. Deploying Master Scripts & Control API ==="
cp "$SCRIPT_DIR/scripts/patch-frame.sh" "$HOME_DIR/patch-frame.sh"
chmod +x "$HOME_DIR/patch-frame.sh"
echo "✓ Installed ~/patch-frame.sh"

if [ -f "$SCRIPT_DIR/scripts/birdframe-api.py" ]; then
  cp "$SCRIPT_DIR/scripts/birdframe-api.py" "$HOME_DIR/birdframe-api.py"
  chmod +x "$HOME_DIR/birdframe-api.py"
  echo "✓ Installed ~/birdframe-api.py"
fi

if [ -f "$SCRIPT_DIR/scripts/backup-frame.sh" ]; then
  cp "$SCRIPT_DIR/scripts/backup-frame.sh" "$HOME_DIR/backup-frame.sh"
  chmod +x "$HOME_DIR/backup-frame.sh"
  echo "✓ Installed ~/backup-frame.sh"
fi

# 5. Run Master Re-Patching Script
echo "=== 5. Applying All Custom Performance & Driver Patches ==="
"$HOME_DIR/patch-frame.sh"

# 6. Deploy Patched Python Sources Directly (if available)
if [ -d "$SCRIPT_DIR/patched_source" ]; then
  echo "=== 6. Deploying Direct Patched Sources ==="
  [ -f "$SCRIPT_DIR/patched_source/display.py" ] && cp "$SCRIPT_DIR/patched_source/display.py" "$HOME_DIR/AvianVisitors/frame/display.py"
  [ -f "$SCRIPT_DIR/patched_source/shoot.py" ] && cp "$SCRIPT_DIR/patched_source/shoot.py" "$HOME_DIR/AvianVisitors/frame/shoot.py"
  INKY_PATH=$(find "$HOME_DIR/AvianVisitors/frame/.venv" -name "inky_el133uf1.py" 2>/dev/null | head -n 1)
  if [ -n "$INKY_PATH" ] && [ -f "$SCRIPT_DIR/patched_source/inky_el133uf1.py" ]; then
    cp "$SCRIPT_DIR/patched_source/inky_el133uf1.py" "$INKY_PATH"
  fi
  echo "✓ Synced patched source files"
fi

# 7. Check & Deploy Systemd Units
echo "=== 7. Setting Up Systemd Services & Timers ==="
if [ -f "$SCRIPT_DIR/systemd/birdframe.service" ]; then
  sudo cp "$SCRIPT_DIR/systemd/birdframe.service" /etc/systemd/system/
  sudo cp "$SCRIPT_DIR/systemd/birdframe.timer" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now birdframe.timer
  echo "✓ Enabled and started birdframe.timer"
fi

if [ -f "$SCRIPT_DIR/systemd/birdframe-api.service" ]; then
  sudo cp "$SCRIPT_DIR/systemd/birdframe-api.service" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now birdframe-api.service
  echo "✓ Enabled and started birdframe-api.service"
fi

# 8. Check Boot Configuration for SPI Chip Select Overlay
echo "=== 8. Checking Hardware Boot Configuration ==="
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

# 9. Check /etc/hosts for birdnet.local resolution
if ! grep -q "birdnet.local" /etc/hosts; then
  echo "192.168.1.71 birdnet.local" | sudo tee -a /etc/hosts
  echo "✓ Added '192.168.1.71 birdnet.local' to /etc/hosts"
fi

echo "=========================================================="
echo " Installation Complete! Your frame is fully configured."
echo "=========================================================="
