#!/usr/bin/env python3
"""Frame-Pi client: turn a collage screenshot into Inky panel pixels.

Runs on the frame Pi (a 3 A+ or Zero 2 W) on a systemd timer. Each run it decides whether a
refresh is worth it (the species set or call-count brackets changed, and it
is not quiet hours), then crops the title and collage from the screenshot,
centres and mats them, and pushes the result to the Inky Impression 13.3".
``--preview out.png`` writes an approximate 6-ink dither instead, so the
look can be checked on any machine without the panel.
"""
from __future__ import annotations

import argparse
import fcntl
import base64
import hashlib
import inspect
import io
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from datetime import datetime
import math
import random
from PIL import Image, ImageChops, ImageDraw, ImageFont

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

PANEL_W, PANEL_H = 1200, 1600  # portrait; the panel itself is 1600x1200

# Approximate Spectra-6 inks, used only for --preview. On hardware the Inky
# library maps to the panel's real palette.
SPECTRA6 = [(236, 234, 223), (26, 26, 28), (165, 60, 56),
            (198, 176, 74), (49, 71, 130), (58, 110, 72)]

DEFAULTS = {
    "base_url": "http://birdnet.local",
    "species_source": "",   # "" = the recent API; "birdweather" = one station or a ZIP
    "zip": "",              # BirdWeather ZIP / postal code (use one locator only)
    "bw_station_id": "",    # public BirdWeather station ID (use instead of zip)
    "bw_days": 7,           # BirdWeather lookback window, in days
    "bw_country": "us",     # geocoder country for the ZIP
    "hours": 24,
    "image": "",            # local PNG written by the shooter
    "image_url": "",        # or a published screenshot URL
    "shoot": False,         # or capture inline (needs a browser; the 3 A+ and Zero 2 W both handle it)
    "shoot_title": None, "shoot_subtitle": None,
    "shoot_headline_px": 42, "shoot_eyebrow_px": 18, "shoot_lowercase": False,
    "shoot_mat": 0.04, "shoot_small_floor": 0.04, "shoot_count_exp": 0.65,
    "bird_names": False,
    "mat": 0.0,             # extra global shrink of the content inside the A5 opening
    "opening": 0.7071,      # opening height as a panel fraction; 0.7071 preserves A5
    "rotate": 90,           # 90 or 270 if the frame hangs the other way up
    "saturation": 0.6,
    "panel": "",            # "el133uf1" forces the 13.3" driver if auto() fails
    "quiet_start": 0, "quiet_end": 0,    # 0/0 = no quiet hours
    "heal_hours": 24,
    "state": "~/.birdframe/state.json",
    "cache": "~/.birdframe",
    "timeout": 180,      # seconds; a Zero 2 W needs ~70-120s to shoot the collage
    "basic_user": None, "basic_pass": None,
}


def _auth(cfg):
    if not cfg.get("basic_user"):
        return None
    raw = f"{cfg['basic_user']}:{cfg.get('basic_pass') or ''}".encode()
    return "Basic " + base64.b64encode(raw).decode()


# --- change detection -------------------------------------------------------
def _resolve_hours(hours_cfg):
    if str(hours_cfg).lower() in ('today', '0', 'day'):
        return max(1, datetime.now().hour + 1)
    return int(hours_cfg)

def slugify(sci):
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def _bucket(n):
    for i, edge in enumerate((1, 2, 5, 15, 40, 100, 300, 1000)):
        if n <= edge:
            return i
    return 8


def fetch_recent(base, hours, timeout, auth=None):
    h = _resolve_hours(hours)
    url = f"{base.rstrip('/')}/avian/api/birdnet-api.php?action=recent&hours={h}"
    req = urllib.request.Request(url, headers={"User-Agent": "AvianVisitors-frame/1.0"})
    if auth:
        req.add_header("Authorization", auth)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read(2_000_000)).get("species", [])


def signature(species, scope=""):
    items = sorted((slugify(s["sci"]), _bucket(int(s.get("n") or 1))) for s in species)
    material = [scope, items] if scope else items
    return hashlib.sha256(json.dumps(material).encode()).hexdigest()[:16]


def birdweather_locator(cfg):
    """Return (kind, value) for the one configured BirdWeather source."""
    station = cfg.get("bw_station_id")
    has_station = station not in (None, "", 0)
    zip_code = cfg.get("zip")
    has_zip = isinstance(zip_code, str) and bool(zip_code.strip())
    if has_station and has_zip:
        raise ValueError("BirdWeather config must use either bw_station_id or zip, not both")
    if has_station:
        import birdweather
        return "station", birdweather.station_id(station)
    if has_zip:
        return "zip", zip_code.strip()
    raise ValueError("BirdWeather config needs bw_station_id or zip")


def birdweather_signature_scope(cfg):
    kind, value = birdweather_locator(cfg)
    if kind == "station":
        return f"birdweather:station:{value}:days:{cfg['bw_days']}"
    return f"birdweather:zip:{cfg['bw_country']}:{value}:days:{cfg['bw_days']}"


def fetch_species(cfg, auth=None):
    """The species list the signature is built from: the BirdNET-Pi recent API
    by default, or BirdWeather detections from one station or near a ZIP when
    species_source = "birdweather"."""
    if cfg.get("species_source") == "birdweather":
        import birdweather
        kind, value = birdweather_locator(cfg)
        if kind == "station":
            return birdweather.species_for_station(value, days=cfg["bw_days"])
        return birdweather.species_for_zip(value, country=cfg["bw_country"], days=cfg["bw_days"])
    return fetch_recent(cfg["base_url"], cfg["hours"], cfg["timeout"], auth)


# --- image ------------------------------------------------------------------
def get_image(src, timeout, auth=None):
    if re.match(r"^https?://", src):
        req = urllib.request.Request(src, headers={"User-Agent": "AvianVisitors-frame/1.0"})
        if auth:
            req.add_header("Authorization", auth)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return Image.open(io.BytesIO(r.read(20_000_000))).convert("RGB")
    return Image.open(os.path.expanduser(src)).convert("RGB")


def fit_panel(img):
    if img.size != (PANEL_W, PANEL_H):
        img = img.resize((PANEL_W, PANEL_H), Image.LANCZOS)
    return img


def _paper(img):
    return (255, 255, 255)


# The opening is a 1:sqrt(2) rectangle centred in the panel. `opening` sets
# how much of the panel height it covers; 0.7071 preserves the A5 default.
def opening_size(opening):
    if isinstance(opening, bool):
        raise ValueError("opening must be greater than 0 and at most 1")
    try:
        opening = float(opening)
    except (TypeError, ValueError) as exc:
        raise ValueError("opening must be greater than 0 and at most 1") from exc
    if not 0 < opening <= 1:
        raise ValueError("opening must be greater than 0 and at most 1")
    h = PANEL_H * opening
    return h / 1.41421, h


def _place(content, paper, mat, opening):
    box_w, box_h = opening_size(opening)
    s = min(box_w * (1 - mat) / content.width, box_h * (1 - mat) / content.height)
    nw, nh = max(1, round(content.width * s)), max(1, round(content.height * s))
    content = content.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), paper)
    canvas.paste(content, ((PANEL_W - nw) // 2, (PANEL_H - nh) // 2))
    return canvas


def _region_bbox(img, paper, y0, y1):
    region = img.crop((0, y0, img.width, y1))
    diff = ImageChops.difference(region, Image.new("RGB", region.size, paper))
    bb = diff.convert("L").point(lambda p: 255 if p > 34 else 0).getbbox()
    return None if not bb else (bb[0], y0 + bb[1], bb[2], y0 + bb[3])


def _scale_w(img, target_w):
    s = target_w / img.width
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def _scale_h(img, target_h):
    s = target_h / img.height
    return img.resize((max(1, round(img.width * s)), max(1, round(img.height * s))), Image.LANCZOS)


def _centroid_x(img, paper):
    """Horizontal centre of ink weight (what the eye reads as centred)."""
    m = ImageChops.difference(img, Image.new("RGB", img.size, paper)).convert("L")
    cols = list(m.resize((img.width, 1), Image.BOX).tobytes())
    total = sum(cols) or 1
    return sum(x * v for x, v in enumerate(cols)) / total


# Content layout inside the A5 opening: the title and collage are sized
# independently (as fractions of the opening width), so tuning one leaves the
# other untouched. gap is a fraction of the opening height.
TITLE_H_FRAC, COLLAGE_FRAC, GAP_FRAC = 0.055, 0.96, 0.04


def mat_and_center(img, mat, opening):
    """Crop the title and collage, size each to a fraction of the opening,
    stack with a gap, and centre on the panel."""
    img = img.convert("RGB")
    paper = _paper(img)
    mask = ImageChops.difference(img, Image.new("RGB", img.size, paper))
    mask = mask.convert("L").point(lambda p: 255 if p > 34 else 0)
    full = mask.getbbox()
    if not full:
        return img
    levels = list(mask.resize((1, img.height), Image.BOX).tobytes())  # per-row content
    top, bot = full[1], full[3]
    split, run = None, 0
    for y in range(top, bot):
        if levels[y] <= 2:
            run += 1
            if run >= 60:  # split below the headline; a 60px band clears the ~30px eyebrow/headline gap so the title stays whole
                cy = y
                while cy < bot and levels[cy] <= 2:
                    cy += 1
                split = (y - run + 1, cy)
                break
        else:
            run = 0
    tb = _region_bbox(img, paper, top, split[0]) if split else None
    cb = _region_bbox(img, paper, split[1], bot + 1) if split else None
    ow, oh = opening_size(opening)
    box_w, box_h = ow * (1 - mat), oh * (1 - mat)
    if not (tb and cb):
        return _place(img.crop(full), paper, mat, opening)
    title = _scale_h(img.crop(tb), box_h * TITLE_H_FRAC)
    gap = round(box_h * GAP_FRAC)
    # Size the collage to fill the room left under the fixed-size title,
    # binding on whichever of width or remaining height runs out first, so the
    # title stays a consistent size whether the collage is tall or compact
    # instead of ballooning when the collage happens to be short.
    coll = img.crop(cb)
    cs = min(box_w * COLLAGE_FRAC / coll.width, (box_h - title.height - gap) / coll.height)
    collage = coll.resize((max(1, round(coll.width * cs)), max(1, round(coll.height * cs))), Image.LANCZOS)
    ccx = _centroid_x(collage, paper)  # centre the collage by ink weight, not bbox
    half = max(ccx, collage.width - ccx)
    # A wildly off-centre collage can push the centroid-mirrored width (2*half)
    # past the A5 opening; shrink only the collage, never the fixed-size title,
    # so nothing spills under the physical mat.
    if 2 * half > box_w:
        s = box_w / (2 * half)
        collage = collage.resize((max(1, round(collage.width * s)), max(1, round(collage.height * s))), Image.LANCZOS)
        ccx = round(ccx * s)
        half = max(ccx, collage.width - ccx)
    cw = round(max(title.width, 2 * half))
    comp = Image.new("RGB", (cw, title.height + gap + collage.height), paper)
    comp.paste(title, ((cw - title.width) // 2, 0))
    comp.paste(collage, (round(cw / 2 - ccx), title.height + gap))
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), paper)
    canvas.paste(comp, ((PANEL_W - comp.width) // 2, (PANEL_H - comp.height) // 2))
    return canvas


def quantize_spectra6(img):
    pal = Image.new("P", (1, 1))
    flat = [c for ink in SPECTRA6 for c in ink]
    flat += list(SPECTRA6[0]) * ((768 - len(flat)) // 3)  # pad the 256-entry palette with paper
    pal.putpalette(flat[:768])
    return img.convert("RGB").quantize(palette=pal, dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")


def _draw_mat_box(img, opening):
    """Dev aid: outline the configured mat opening."""
    ow, oh = opening_size(opening)
    x0, y0 = round((PANEL_W - ow) / 2), round((PANEL_H - oh) / 2)
    ImageDraw.Draw(img).rectangle((x0, y0, PANEL_W - x0 - 1, PANEL_H - y0 - 1),
                                  outline=(170, 60, 56), width=2)


# --- hardware ---------------------------------------------------------------
def push_panel(img, rotate, saturation, panel=""):
    import gc
    gc.collect()
    """Rotate to the panel's landscape buffer and push. Lazy import so this
    module still loads on a machine without the Inky library."""
    if rotate not in (90, 270):
        print(f"rotate must be 90 or 270, not {rotate}; using 90", file=sys.stderr)
        rotate = 90
    if panel == "el133uf1":
        from inky.inky_el133uf1 import Inky
        dev = Inky(resolution=(1600, 1200))
    else:
        from inky.auto import auto
        dev = auto()
    buf = img.rotate(rotate, expand=True)
    if buf.size != (dev.width, dev.height):
        buf = buf.resize((dev.width, dev.height), Image.LANCZOS)
    import numpy as np
    from PIL import ImageEnhance

    # Clamp all near-white background pixels to 100% pure solid white (removes all stipple dots)
    arr = np.array(buf)
    white_mask = (arr[:, :, 0] > 225) & (arr[:, :, 1] > 225) & (arr[:, :, 2] > 225)
    arr[white_mask] = [255, 255, 255]
    buf = Image.fromarray(arr)

    # Boost color vibrance, tone contrast, and edge sharpness for light birds
    buf = ImageEnhance.Color(buf).enhance(1.4)
    buf = ImageEnhance.Contrast(buf).enhance(1.2)
    buf = ImageEnhance.Sharpness(buf).enhance(1.3)
    kw = {"saturation": saturation} if "saturation" in inspect.signature(dev.set_image).parameters else {}
    dev.set_image(buf, **kw)
    dev.show()


# --- state ------------------------------------------------------------------
def load_state(path):
    try:
        with open(os.path.expanduser(path)) as f:
            return json.load(f)
    except Exception:
        return {"signature": None, "last_refresh": 0}


def save_state(path, sig, when):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"signature": sig, "last_refresh": when, "last_date": datetime.now().strftime("%Y-%m-%d")}, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic: a power cut can't leave a half-written file


def in_quiet_hours(cfg, hour):
    s, e = cfg["quiet_start"], cfg["quiet_end"]
    if s == e:
        return False
    return s <= hour < e if s < e else hour >= s or hour < e


def frame_url(url, bird_names):
    """Set the frame's label preference without disturbing other URL state."""
    import urllib.parse
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if k != "labels"]
    query.append(("labels", "1" if bird_names else "0"))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))


# --- run --------------------------------------------------------------------
def obtain_image(cfg, species=None):
    if cfg.get("species_source") == "birdweather":
        from shoot import shoot_birdweather
        if species is None:  # gate skipped (--no-signature): fetch the list to render
            species = fetch_species(cfg, _auth(cfg))
        out = os.path.join(os.path.expanduser(cfg["cache"]), "frame.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        shoot_birdweather(out, species, title=cfg["shoot_title"], subtitle=cfg["shoot_subtitle"],
                          timeout_ms=cfg["timeout"] * 1000, bird_names=cfg["bird_names"])
        return Image.open(out).convert("RGB")
_DIMS_CACHE = None
_MASKS_CACHE = None
_MASK_DEC_CACHE = {}

def _load_dims_and_masks(base_dir):
    global _DIMS_CACHE, _MASKS_CACHE
    if _DIMS_CACHE is not None and _MASKS_CACHE is not None:
        return _DIMS_CACHE, _MASKS_CACHE
    dims_path = os.path.join(base_dir, "dims.json")
    masks_path = os.path.join(base_dir, "masks.json")
    if not os.path.exists(dims_path):
        dims_path = os.path.join(os.path.dirname(base_dir), "dims.json")
    if not os.path.exists(masks_path):
        masks_path = os.path.join(os.path.dirname(base_dir), "masks.json")
    if os.path.exists(dims_path):
        with open(dims_path, "r") as f:
            _DIMS_CACHE = json.load(f)
    else:
        _DIMS_CACHE = {}
    if os.path.exists(masks_path):
        with open(masks_path, "r") as f:
            _MASKS_CACHE = json.load(f)
    else:
        _MASKS_CACHE = {}
    return _DIMS_CACHE, _MASKS_CACHE

def _load_mask(slug, masks_dict):
    if slug in _MASK_DEC_CACHE:
        return _MASK_DEC_CACHE[slug]
    rec = masks_dict.get(slug)
    if not rec:
        w, h = 60, 60
        cells = [[x, y] for y in range(h) for x in range(w) if ((x-30)**2 + (y-30)**2) < 28**2]
        res = {"w": w, "h": h, "cells": cells}
        _MASK_DEC_CACHE[slug] = res
        return res
    raw = base64.b64decode(rec["bits"])
    w, h = rec["w"], rec["h"]
    cells = []
    for y in range(h):
        for x in range(w):
            i = y * w + x
            b = raw[i >> 3]
            if (b >> (7 - (i & 7))) & 1:
                cells.append([x, y])
    res = {"w": w, "h": h, "cells": cells}
    _MASK_DEC_CACHE[slug] = res
    return res

def _tuning(n):
    return {
        "packingBudgetFrac": 0.46 if n <= 4 else (0.40 if n <= 12 else (0.34 if n <= 24 else 0.28)),
        "countExp": 0.65,
        "minTileAreaFrac": 0.0100 if n <= 8 else (0.0075 if n <= 20 else 0.0055),
        "ellipseAspectBias": 2.1,
    }

def _mask_pack(tiles, W, H, x_bias=1.0, y_bias=1.5, pad=3):
    import numpy as np
    GRID_STRIDE = 4
    GW = math.ceil(W / GRID_STRIDE) + 2
    GH = math.ceil(H / GRID_STRIDE) + 2
    grid = np.zeros((GH, GW), dtype=np.uint8)

    cx = W / 2.0
    cy = H / 2.0

    tiles.sort(key=lambda t: t["fullW"] * t["fullH"], reverse=True)
    placed = []

    seed = [0x9E3779B9]
    def rand():
        seed[0] = (seed[0] * 16807) % 2147483647
        return seed[0] / 2147483647.0

    def get_cell_ranges(tile, tx, ty):
        sx = tile["fullW"] / tile["mask"]["w"]
        sy = tile["fullH"] / tile["mask"]["h"]
        cells = tile["mask"]["cells"]
        ranges = []
        for c in cells:
            x0 = max(0, min(GW - 1, int((tx + c[0] * sx) / GRID_STRIDE)))
            y0 = max(0, min(GH - 1, int((ty + c[1] * sy) / GRID_STRIDE)))
            x1 = max(0, min(GW - 1, int((tx + (c[0] + 1) * sx) / GRID_STRIDE)))
            y1 = max(0, min(GH - 1, int((ty + (c[1] + 1) * sy) / GRID_STRIDE)))
            ranges.append((x0, y0, x1, y1))
        return ranges

    def off_grid(tile, tx, ty):
        lbl_h = tile.get("lbl_h", 0)
        lbl_w = tile.get("lbl_w", 0)
        lx0 = min(0, (tile["fullW"] - lbl_w) / 2.0)
        lx1 = max(tile["fullW"], (tile["fullW"] + lbl_w) / 2.0)
        if tx + lx0 < 0 or tx + lx1 > W or ty < 0 or ty + tile["fullH"] + lbl_h > H:
            return True
        return False

    def collides(tile, tx, ty):
        ranges = get_cell_ranges(tile, tx, ty)
        for x0, y0, x1, y1 in ranges:
            if grid[y0:y1+1, x0:x1+1].any():
                return True
        lbl_h = tile.get("lbl_h", 0)
        lbl_w = tile.get("lbl_w", 0)
        if lbl_h > 0:
            lx0 = max(0, min(GW - 1, int((tx + (tile["fullW"] - lbl_w) / 2.0) / GRID_STRIDE)))
            ly0 = max(0, min(GH - 1, int((ty + tile["fullH"]) / GRID_STRIDE)))
            lx1 = max(0, min(GW - 1, int((tx + (tile["fullW"] + lbl_w) / 2.0) / GRID_STRIDE)))
            ly1 = max(0, min(GH - 1, int((ty + tile["fullH"] + lbl_h) / GRID_STRIDE)))
            if grid[ly0:ly1+1, lx0:lx1+1].any():
                return True
        return False

    def stamp(tile, tx, ty):
        ranges = get_cell_ranges(tile, tx, ty)
        for x0, y0, x1, y1 in ranges:
            sx0 = max(0, x0 - pad)
            sy0 = max(0, y0 - pad)
            sx1 = min(GW - 1, x1 + pad)
            sy1 = min(GH - 1, y1 + pad)
            grid[sy0:sy1+1, sx0:sx1+1] = 1

        lbl_h = tile.get("lbl_h", 0)
        lbl_w = tile.get("lbl_w", 0)
        if lbl_h > 0:
            lpad = min(pad, 2)
            lx0 = max(0, int((tx + (tile["fullW"] - lbl_w) / 2.0) / GRID_STRIDE) - lpad)
            ly0 = max(0, int((ty + tile["fullH"]) / GRID_STRIDE) - lpad)
            lx1 = min(GW - 1, int((tx + (tile["fullW"] + lbl_w) / 2.0) / GRID_STRIDE) + lpad)
            ly1 = min(GH - 1, int((ty + tile["fullH"] + lbl_h) / GRID_STRIDE) + lpad)
            grid[ly0:ly1+1, lx0:lx1+1] = 1

    for i, t in enumerate(tiles):
        if i == 0:
            tx = cx - t["fullW"] / 2.0
            ty = cy - t["fullH"] / 2.0
            t["x"] = tx
            t["y"] = ty
            stamp(t, tx, ty)
            placed.append(t)
            continue

        com_x, com_y, com_w = 0.0, 0.0, 0.0
        for p in placed:
            a = p["fullW"] * p["fullH"]
            com_x += (p["x"] + p["fullW"] / 2.0) * a
            com_y += (p["y"] + p["fullH"] / 2.0) * a
            com_w += a
        if com_w > 0:
            com_x /= com_w
            com_y /= com_w
        else:
            com_x, com_y = cx, cy

        best = None
        best_cost = float("inf")
        step = max(GRID_STRIDE, min(t["fullW"], t["fullH"]) * 0.05)
        max_r = max(W, H)
        found_ring = -1
        phase = rand() * math.pi * 2.0

        r = 0.0
        while r <= max_r:
            if found_ring >= 0 and r > found_ring + step * 2.0:
                break
            samples = max(36, int(r / 1.6))
            for k in range(samples):
                theta = phase + (k / float(samples)) * math.pi * 2.0
                px = cx + r * x_bias * math.cos(theta) - t["fullW"] / 2.0
                py = cy + r * y_bias * math.sin(theta) - t["fullH"] / 2.0
                if off_grid(t, px, py):
                    continue
                if collides(t, px, py):
                    continue
                dxx = (px + t["fullW"] / 2.0 - com_x)
                dyy = (py + t["fullH"] / 2.0 - com_y)
                cost = math.hypot(dxx / x_bias, dyy / y_bias) + rand() * step * 0.5
                if cost < best_cost:
                    best_cost = cost
                    best = (px, py)
            if best and found_ring < 0:
                found_ring = r
            r += step

        if best:
            t["x"] = best[0]
            t["y"] = best[1]
            stamp(t, best[0], best[1])
            placed.append(t)
        else:
            t["x"] = -99999
            t["y"] = -99999
            placed.append(t)

    return placed

def _cluster_bounds(arr):
    L, R, T2, B = float("inf"), float("-inf"), float("inf"), float("-inf")
    for t in arr:
        if t["x"] < -1000:
            continue
        lbl_h = t.get("lbl_h", 0)
        lbl_w = t.get("lbl_w", 0)
        lx0 = t["x"] + min(0, (t["fullW"] - lbl_w) / 2.0)
        lx1 = t["x"] + max(t["fullW"], (t["fullW"] + lbl_w) / 2.0)
        ly0 = t["y"]
        ly1 = t["y"] + t["fullH"] + lbl_h
        if lx0 < L: L = lx0
        if lx1 > R: R = lx1
        if ly0 < T2: T2 = ly0
        if ly1 > B: B = ly1
    return {"L": L, "R": R, "T": T2, "B": B}

def _get_font(font_path, size):
    try:
        return ImageFont.truetype(font_path, size)
    except Exception:
        return ImageFont.load_default()

def render_native_collage(species_list, cfg, style="vintage", title=None, subtitle=None, show_names=True):
    bg_color = (252, 250, 245) if style == "cartoon" else (248, 246, 240)
    text_color = (40, 35, 30)
    subtext_color = (120, 110, 100)

    here = os.path.dirname(os.path.abspath(__file__))
    for d in [
        os.path.join(here, "assets"),
        os.path.join(here, "..", "assets"),
        "/home/birder/AvianVisitors/frame/assets",
    ]:
        if os.path.exists(d):
            asset_base = d
            break
    else:
        asset_base = os.path.join(here, "assets")

    for f in [
        "/home/birder/.local/share/fonts",
        os.path.join(here, "fonts"),
        os.path.join(here, "..", "fonts"),
    ]:
        if os.path.exists(f):
            font_base = f
            break
    else:
        font_base = os.path.join(here, "fonts")

    if style == "cartoon":
        asset_dir = os.path.join(asset_base, "cartoon")
        font_main = os.path.join(font_base, "FingerPaint-Regular.ttf")
        font_title = font_main
    elif style == "vintage":
        asset_dir = os.path.join(asset_base, "vintage")
        font_main = os.path.join(font_base, "EBGaramond-Italic.ttf")
        font_title = os.path.join(font_base, "LibreBaskerville-Italic.ttf")
        if not os.path.exists(font_title):
            font_title = font_main
    else: # sketch
        asset_dir = os.path.join(asset_base, "sketch")
        font_main = os.path.join(font_base, "Caveat.ttf")
        font_title = font_main

    canvas = Image.new("RGB", (PANEL_W, PANEL_H), bg_color)
    draw = ImageDraw.Draw(canvas)

    pad_top = 70
    font_sub = _get_font(font_title, 26)
    font_head = _get_font(font_title, 54)
    font_label = _get_font(font_main, 24 if style != "sketch" else 28)

    # Subtitle
    sub_text = (subtitle or "HEARD TODAY").upper()
    sub_bbox = draw.textbbox((0, 0), sub_text, font=font_sub)
    draw.text(((PANEL_W - (sub_bbox[2] - sub_bbox[0])) // 2, pad_top), sub_text, font=font_sub, fill=subtext_color)

    # Headline
    head_text = title or "Avian Visitors"
    head_y = pad_top + 36
    head_bbox = draw.textbbox((0, 0), head_text, font=font_head)
    draw.text(((PANEL_W - (head_bbox[2] - head_bbox[0])) // 2, head_y), head_text, font=font_head, fill=text_color)

    # Rule
    rule_y = head_y + 70
    draw.line([(PANEL_W // 2 - 140, rule_y), (PANEL_W // 2 + 140, rule_y)], fill=(190, 180, 170), width=1)

    if not species_list:
        empty_font = _get_font(font_main, 32)
        msg = "listening for birds…"
        m_bbox = draw.textbbox((0, 0), msg, font=empty_font)
        draw.text(((PANEL_W - (m_bbox[2]-m_bbox[0])) // 2, PANEL_H // 2), msg, font=empty_font, fill=subtext_color)
        return canvas

    dims, masks = _load_dims_and_masks(here)

    CW = 1100
    CH = 1330
    collage_x0 = 50
    collage_y0 = rule_y + 35

    T = _tuning(len(species_list))
    vp_area = CW * CH
    budget = vp_area * T["packingBudgetFrac"]
    min_area = vp_area * T["minTileAreaFrac"]

    tiles = []
    for s in species_list:
        sci = s.get("sci", "")
        slug = slugify(sci)
        pose = 1
        has_flight = f"{slug}-2" in dims
        if has_flight and random.random() < 0.2:
            pose = 2
            slug = f"{slug}-2"

        mask = _load_mask(slug, masks)
        d = dims.get(slug)
        ar = (d[0] / float(d[1])) if d else 1.4
        n = float(s.get("n") or 1)
        score = math.pow(max(1.0, n), T["countExp"])

        com = s.get("com", sci)
        l_bbox = draw.textbbox((0, 0), com, font=font_label)
        lbl_w = (l_bbox[2] - l_bbox[0]) if show_names else 0
        lbl_h = (l_bbox[3] - l_bbox[1] + 6) if show_names else 0

        tiles.append({
            "mask": mask, "data": s, "pose": pose, "slug": slug, "sci": sci, "com": com,
            "ar": ar, "score": score, "lbl_w": lbl_w, "lbl_h": lbl_h
        })

    sum_score = sum(t["score"] for t in tiles) or 1.0
    for t in tiles:
        t["area"] = max(min_area, budget * t["score"] / sum_score)

    sum_a = sum(t["area"] for t in tiles)
    if sum_a > budget:
        fixed_sum = sum(t["area"] for t in tiles if t["area"] <= min_area + 1e-9)
        flex_sum = sum_a - fixed_sum
        flex_budget = max(0, budget - fixed_sum)
        shrink = (flex_budget / flex_sum) if flex_sum > 0 else 1.0
        for t in tiles:
            if t["area"] > min_area + 1e-9:
                t["area"] *= shrink

    for t in tiles:
        t["fullW"] = math.sqrt(t["area"] * t["ar"])
        t["fullH"] = t["fullW"] / t["ar"]

    x_bias = 1.0
    y_bias = 1.5
    pad = 3

    placed = _mask_pack(tiles, CW, CH, x_bias, y_bias, pad)
    b = _cluster_bounds(placed)

    for iter_idx in range(10):
        missing = any(t["x"] < -1000 for t in placed)
        overflow = b["L"] < 0 or b["T"] < 0 or b["R"] > CW or b["B"] > CH
        if not missing and not overflow:
            break
        scale = 0.93
        if overflow:
            cl_w = b["R"] - b["L"]
            cl_h = b["B"] - b["T"]
            sx = (CW * 0.96) / max(cl_w, CW * 0.96)
            sy = (CH * 0.94) / max(cl_h, CH * 0.94)
            scale = min(scale, sx, sy)
        for t in tiles:
            t["fullW"] *= scale
            t["fullH"] *= scale
        placed = _mask_pack(tiles, CW, CH, x_bias, y_bias, pad)
        b = _cluster_bounds(placed)

    cl_cx = (b["L"] + b["R"]) / 2.0
    cl_cy = (b["T"] + b["B"]) / 2.0
    dx = (CW / 2.0) - cl_cx
    dy = (CH / 2.0) - cl_cy

    placed_count = 0
    for t in placed:
        if t["x"] < -1000:
            continue
        final_x = int(collage_x0 + t["x"] + dx)
        final_y = int(collage_y0 + t["y"] + dy)
        nw = max(10, int(t["fullW"]))
        nh = max(10, int(t["fullH"]))

        slug = t["slug"]
        base_slug = slugify(t["sci"])
        img_path = os.path.join(asset_dir, f"{slug}.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(asset_dir, f"{base_slug}.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(asset_dir, "default.png")

        if os.path.exists(img_path):
            try:
                b_img = Image.open(img_path).convert("RGBA")
                b_img = b_img.resize((nw, nh), Image.Resampling.LANCZOS)
                canvas.paste(b_img, (final_x, final_y), b_img)
                placed_count += 1
            except Exception as e:
                pass

        if show_names:
            com = t["com"]
            l_bbox = draw.textbbox((0, 0), com, font=font_label)
            lw = l_bbox[2] - l_bbox[0]
            lx = final_x + (nw - lw) // 2
            ly = final_y + nh + 3
            draw.text((lx, ly), com, font=font_label, fill=text_color)

    footer_text = f"{placed_count} of {len(species_list)} species heard today"
    font_foot = _get_font(font_title, 20)
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_foot)
    draw.text(((PANEL_W - (f_bbox[2] - f_bbox[0])) // 2, PANEL_H - 50), footer_text, font=font_foot, fill=subtext_color)

    return canvas


def obtain_image(cfg, species=None):
    if cfg.get("shoot") or not (cfg.get("image_url") or cfg.get("image")):
        if species is None:
            species = fetch_species(cfg, _auth(cfg))
        style = str(cfg.get("style", cfg.get("art_style", "vintage"))).lower()
        title = cfg.get("shoot_title") or "Avian Visitors"
        subtitle = cfg.get("shoot_subtitle") or "Heard Today"
        show_names = bool(cfg.get("bird_names", True))
        return render_native_collage(species, cfg, style=style, title=title, subtitle=subtitle, show_names=show_names)
    src = cfg["image_url"] or cfg["image"]
    if not src:
        raise ValueError("set image, image_url, or shoot in config")
    # A pre-rendered frame is still someone's render, so ask it for names the
    # same way this Pi asks its own browser. A source that does not know the
    # parameter ignores it and sends what it always sent, so this is safe
    # against anything. URLs only: a local file path has no query string.
    if cfg["image_url"]:
        src = frame_url(src, cfg["bird_names"])
    return get_image(src, cfg["timeout"], _auth(cfg))


def run(cfg, preview=None, force=False, use_signature=True, mat_box=False):
    now = time.time()
    state = load_state(cfg["state"])
    sig = None
    species = None
    if use_signature:
        try:
            species = fetch_species(cfg, _auth(cfg))
            scope = birdweather_signature_scope(cfg) if cfg.get("species_source") == "birdweather" else ""
            sig = signature(species, scope)
        except Exception as e:
            print(f"signature fetch failed: {e}", file=sys.stderr)  # treat as no change
    today_str = datetime.now().strftime("%Y-%m-%d")
    date_changed = (cfg.get("hours") == "today") and (state.get("last_date") != today_str)
    heal_due = now - state.get("last_refresh", 0) >= cfg["heal_hours"] * 3600
    changed = (not use_signature) or date_changed or (sig is not None and sig != state.get("signature"))
    if not force and not preview:
        if in_quiet_hours(cfg, datetime.now().hour):
            print("quiet hours; skip")
            return
        if not changed and not heal_due:
            print("no change; skip")
            return
        print("refresh:", "changed" if changed else "heal")

    try:
        img = obtain_image(cfg, species)
        if img.size != (PANEL_W, PANEL_H):
            img = fit_panel(img)
            img = mat_and_center(img, cfg["mat"], cfg["opening"])
    except Exception as e:
        print(f"could not get image: {e}", file=sys.stderr)  # keep last panel image
        return
    if preview:
        out = quantize_spectra6(img)
        if mat_box:
            _draw_mat_box(out, cfg["opening"])
        out.save(preview)
        print(f"wrote preview {preview}")
        return
    try:
        push_panel(img, cfg["rotate"], cfg["saturation"], cfg.get("panel", ""))
    except Exception as e:
        print(f"panel push failed: {e}", file=sys.stderr)
        return
    state["last_date"] = datetime.now().strftime("%Y-%m-%d"); save_state(cfg["state"], sig if sig is not None else state.get("signature"), now)
    print("panel updated")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(os.path.expanduser(path), "rb") as f:
            cfg.update(tomllib.load(f))
    return cfg


def main():
    ap = argparse.ArgumentParser(description="Push the collage screenshot to the Inky panel.")
    ap.add_argument("--config")
    ap.add_argument("--base-url")
    ap.add_argument("--image")
    ap.add_argument("--image-url")
    ap.add_argument("--preview", help="write a 6-ink preview PNG instead of pushing")
    ap.add_argument("--rotate", type=int)
    ap.add_argument("--force", action="store_true", help="refresh even if unchanged")
    ap.add_argument("--no-signature", action="store_true", help="skip change detection")
    ap.add_argument("--mat-box", action="store_true", help="dev: outline the mat window on the preview")
    args = ap.parse_args()

    cfg = load_config(args.config)
    for key in ("base_url", "image", "image_url"):
        val = getattr(args, key)
        if val:
            cfg[key] = val
    if args.rotate is not None:
        cfg["rotate"] = args.rotate
    # One render at a time. A manual --force colliding with the timer's run
    # pushes two refreshes into the panel mid-cycle; on the 13.3" (two
    # half-panel controllers) that shows a split image and can wedge one
    # controller until a full power cycle. The lock lives in the cache dir
    # and is dropped automatically on exit.
    lock_path = os.path.join(os.path.expanduser(cfg["cache"]), ".render.lock")
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another render is in progress; skipping")
        return
    run(cfg, preview=args.preview, force=args.force, use_signature=not args.no_signature, mat_box=args.mat_box)


if __name__ == "__main__":
    main()
