#!/bin/bash
set -e
echo "=== Applying custom AvianVisitors Frame patches ==="

FRAME_DIR="/home/birder/AvianVisitors/frame"
VENV_LIB=$(find "$FRAME_DIR/.venv/lib" -name "site-packages" 2>/dev/null | head -n 1)

# 1. Patch COLLAGE_FRAC for larger size (0.85) in display.py
sed -i 's/COLLAGE_FRAC, GAP_FRAC = 0.065, [0-9.]*/COLLAGE_FRAC, GAP_FRAC = 0.065, 0.85/g' "$FRAME_DIR/display.py"
echo "✓ Set COLLAGE_FRAC = 0.85"

# 2. Patch display.py (background engine, date rollover, today reset, species pass-through, vibrance boost, gc)
python3 -c '
file = "/home/birder/AvianVisitors/frame/display.py"
with open(file, "r") as f:
    code = f.read()

helper = """
def _resolve_hours(hours_cfg):
    if str(hours_cfg).lower() in ("today", "0", "day"):
        return "today"
    return max(1, int(hours_cfg))

def _apply_background(img, bg_cfg):
    if not bg_cfg:
        return img
    presets = {
        "creme": (223, 213, 190),
        "parchment": (223, 213, 190),
        "grey": (204, 204, 204),
        "gray": (204, 204, 204),
    }
    if isinstance(bg_cfg, str) and bg_cfg.lower() in presets:
        target_color = presets[bg_cfg.lower()]
    elif isinstance(bg_cfg, str) and bg_cfg.startswith("#"):
        h = bg_cfg.lstrip("#")
        target_color = tuple(int(h[i:i+2], 16) for i in (0, 2, 4))
    elif isinstance(bg_cfg, (list, tuple)):
        target_color = tuple(bg_cfg)
    else:
        return img

    paper = _paper(img)
    diff = ImageChops.difference(img, Image.new("RGB", img.size, paper)).convert("L")
    mask = diff.point(lambda p: min(255, p * 4))
    bg = Image.new("RGB", img.size, target_color)
    return Image.composite(img, bg, mask)
"""
if "_resolve_hours" not in code:
    code = code.replace("def fetch_recent(base, hours, timeout, auth=None):", helper + "\ndef fetch_recent(base, hours, timeout, auth=None):", 1)

code = code.replace(
    "return fetch_recent(cfg[\"base_url\"], cfg[\"hours\"], cfg[\"timeout\"], auth)",
    "hours = _resolve_hours(cfg.get(\"hours\", 24))\n    return fetch_recent(cfg[\"base_url\"], hours, cfg[\"timeout\"], auth)",
    1
)
code = code.replace(
    "window_hours=cfg[\"hours\"],",
    "window_hours=_resolve_hours(cfg.get(\"hours\", 24)),",
    1
)
if "species=species" not in code:
    code = code.replace(
        "bird_names=cfg[\"bird_names\"])",
        "bird_names=cfg[\"bird_names\"], species=species)",
        1
    )

if "import gc" not in code:
    code = code.replace(
        "    try:\n        push_panel(img,",
        "    import gc\n    gc.collect()\n    try:\n        push_panel(img,",
        1
    )

if "ImageEnhance" not in code:
    code = code.replace(
        "        buf = buf.resize((dev.width, dev.height), Image.LANCZOS)",
        "        buf = buf.resize((dev.width, dev.height), Image.LANCZOS)\n    from PIL import ImageEnhance\n    buf = ImageEnhance.Color(buf).enhance(1.4)\n    buf = ImageEnhance.Contrast(buf).enhance(1.15)",
        1
    )

# Save last_date in save_state
code = code.replace(
    "json.dump({\"signature\": sig, \"last_refresh\": when}, f)",
    "json.dump({\"signature\": sig, \"last_refresh\": when, \"last_date\": datetime.now().strftime(\"%Y-%m-%d\")}, f)"
)

# Date change check in run()
target_check = "heal_due = now - state.get(\"last_refresh\", 0) >= cfg[\"heal_hours\"] * 3600\n    changed = (not use_signature) or (sig is not None and sig != state.get(\"signature\"))"
replacement_check = "today_str = datetime.now().strftime(\"%Y-%m-%d\")\n    date_changed = (cfg.get(\"hours\") == \"today\") and (state.get(\"last_date\") != today_str)\n    heal_due = now - state.get(\"last_refresh\", 0) >= cfg[\"heal_hours\"] * 3600\n    changed = (not use_signature) or date_changed or (sig is not None and sig != state.get(\"signature\"))"
code = code.replace(target_check, replacement_check)

with open(file, "w") as f:
    f.write(code)
print("✓ Patched display.py successfully")
'

# 3. Patch shoot.py for robust label and tile rendering
python3 -c '
file = "/home/birder/AvianVisitors/frame/shoot.py"
with open(file, "r") as f:
    code = f.read()

code = code.replace("if not n:", "if not n and \"LABEL_MIN_PX\" not in pat:")

if "page.wait_for_selector(\".gtile\", state=\"attached\"" not in code:
    code = code.replace(
        "page.wait_for_selector(\".gtile, .empty\", state=\"attached\", timeout=timeout_ms)",
        "try:\n                page.wait_for_selector(\".gtile\", state=\"attached\", timeout=30000)\n            except Exception:\n                page.wait_for_selector(\".empty\", state=\"attached\", timeout=timeout_ms)"
    )

code = code.replace(
    "raise RuntimeError(\n                            \"frame labels missing for: \" + \", \".join(missing_labels))",
    "print(\"some frame labels missing: \" + \", \".join(missing_labels), file=sys.stderr)"
)

with open(file, "w") as f:
    f.write(code)
print("✓ Patched shoot.py (label tolerance, tile selector)")
'

# 4. Patch inky_el133uf1.py for 4KB chunking
INKY_FILE=$(find "$VENV_LIB" -name "inky_el133uf1.py" 2>/dev/null || true)
if [ -f "$INKY_FILE" ]; then
    python3 -c '
import sys
file = sys.argv[1]
with open(file, "r") as f:
    code = f.read()

target = """            if data is not None:
                self._gpio.set_value(self.dc_pin, Value.ACTIVE)
                self._spi_bus.xfer3(data)"""

replacement = """            if data is not None:
                self._gpio.set_value(self.dc_pin, Value.ACTIVE)
                d = data.tolist() if hasattr(data, "tolist") else list(data)
                for offset in range(0, len(d), 4096):
                    self._spi_bus.xfer3(d[offset:offset + 4096])"""

if target in code:
    code = code.replace(target, replacement, 1)
    with open(file, "w") as f:
        f.write(code)
    print("✓ Patched inky_el133uf1.py for SPI DMA chunking")
else:
    print("✓ inky_el133uf1.py already patched or chunked")
' "$INKY_FILE"
fi

echo "=== All custom patches applied successfully! ==="
