#!/usr/bin/env bash
set -e

FRAME_DIR="/home/birder/AvianVisitors/frame"
VENV_DIR="$FRAME_DIR/.venv"

echo "=== 1. Ensuring Fonts are Installed Locally ==="
mkdir -p /home/birder/.local/share/fonts
if [ -d "$FRAME_DIR/fonts" ]; then
  cp -r "$FRAME_DIR/fonts/"*.ttf /home/birder/.local/share/fonts/ 2>/dev/null || true
fi
fc-cache -fv /home/birder/.local/share/fonts 2>/dev/null || true

echo "=== 2. Patching Inky 13.3\" Driver (4KB SPI Chunking & 2.0s Power Delay) ==="
INKY_DRIVER=$(find "$VENV_DIR" -name "inky_el133uf1.py" 2>/dev/null | head -n 1)
if [ -n "$INKY_DRIVER" ]; then
  python3 -c "
import re
with open('$INKY_DRIVER', 'r') as f:
    code = f.read()

pattern = r'([ \t]*)self\._spi_bus\.xfer3\(data\)'
replacement = r'''\1d = data.tolist() if hasattr(data, 'tolist') else list(data)
\1for offset in range(0, len(d), 4096):
\1    self._spi_bus.xfer3(d[offset:offset + 4096])'''
if re.search(pattern, code):
    code = re.sub(pattern, replacement, code)

code = code.replace('time.sleep(0.03)', 'time.sleep(0.2)')
code = code.replace('self._send_command(EL133UF1_PON, CS_BOTH_SEL)\n        self._busy_wait(0.2)', 'self._send_command(EL133UF1_PON, CS_BOTH_SEL)\n        time.sleep(2.0)')

with open('$INKY_DRIVER', 'w') as f:
    f.write(code)
print('Successfully patched inky_el133uf1.py!')
"
fi

echo "=== 3. Patching display.py (Daily Reset, Sizing, Clamping & Dual Art Styles) ==="
DISPLAY_PY="$FRAME_DIR/display.py"
python3 -c "
with open('$DISPLAY_PY', 'r') as f:
    code = f.read()

if 'def _resolve_hours(' not in code:
    helper = '''def _resolve_hours(hours_cfg):
    if str(hours_cfg).lower() in ('today', '0', 'day'):
        return max(1, datetime.now().hour + 1)
    return int(hours_cfg)

'''
    code = code.replace('def slugify(sci):', helper + 'def slugify(sci):')

code = code.replace('def _paper(img):\n    """Median of the four corners, robust to a stray inked corner."""\n    w, h = img.size\n    px = (img.getpixel(p) for p in ((4, 4), (w - 5, 4), (4, h - 5), (w - 5, h - 5)))\n    return tuple(int(statistics.median(c)) for c in zip(*px))', 'def _paper(img):\n    return (255, 255, 255)')
code = code.replace('TITLE_H_FRAC, COLLAGE_FRAC, GAP_FRAC = 0.065, 0.66, 0.1', 'TITLE_H_FRAC, COLLAGE_FRAC, GAP_FRAC = 0.055, 0.96, 0.04')
code = code.replace('TITLE_H_FRAC, COLLAGE_FRAC, GAP_FRAC = 0.065, 0.85, 0.06', 'TITLE_H_FRAC, COLLAGE_FRAC, GAP_FRAC = 0.055, 0.96, 0.04')

if 'gc.collect()' not in code:
    code = code.replace('def push_panel(img, rotate, saturation, panel=""):', 'def push_panel(img, rotate, saturation, panel=""):\n    import gc\n    gc.collect()')

enhance_target = 'buf = buf.resize((dev.width, dev.height), Image.LANCZOS)'
enhance_replace = '''buf = buf.resize((dev.width, dev.height), Image.LANCZOS)
    import numpy as np
    from PIL import ImageEnhance

    arr = np.array(buf)
    white_mask = (arr[:, :, 0] > 225) & (arr[:, :, 1] > 225) & (arr[:, :, 2] > 225)
    arr[white_mask] = [255, 255, 255]
    buf = Image.fromarray(arr)

    buf = ImageEnhance.Color(buf).enhance(1.4)
    buf = ImageEnhance.Contrast(buf).enhance(1.2)
    buf = ImageEnhance.Sharpness(buf).enhance(1.3)'''

if enhance_target in code and 'white_mask' not in code:
    code = code.replace(enhance_target, enhance_replace)

target_shoot = '''        window_hours = _resolve_hours(cfg.get("hours", 24))
        shoot(cfg["base_url"], out, title=cfg["shoot_title"], subtitle=cfg["shoot_subtitle"],'''

repl_shoot = '''        window_hours = _resolve_hours(cfg.get("hours", 24))
        style = str(cfg.get("style", cfg.get("art_style", "sketch"))).lower()
        cutout_local = "/home/birder/AvianVisitors/frame/assets/cartoon" if style == "cartoon" else None
        shoot(cfg["base_url"], out, title=cfg["shoot_title"], subtitle=cfg["shoot_subtitle"],
              cutout_local=cutout_local,'''

if target_shoot in code:
    code = code.replace(target_shoot, repl_shoot)

with open('$DISPLAY_PY', 'w') as f:
    f.write(code)
print('Successfully patched display.py!')
"

echo "=== 4. Patching shoot.py (Route Interception, Font & Pose Fallback) ==="
SHOOT_PY="$FRAME_DIR/shoot.py"
python3 -c "
with open('$SHOOT_PY', 'r') as f:
    code = f.read()

code = code.replace('if cutout_base:', 'if cutout_base or cutout_local:')

with open('$SHOOT_PY', 'w') as f:
    f.write(code)
print('Successfully patched shoot.py!')
"
chmod +x /home/birder/patch-frame.sh
