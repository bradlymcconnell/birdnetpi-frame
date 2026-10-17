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
import numpy as np
import math

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
from datetime import datetime, timedelta
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
    "dusk_hour": 17,
    "dawn_hour": 7,
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
def active_night_date(dt=None, dusk_hour=17):
    if dt is None:
        dt = datetime.now()
    if dt.hour < dusk_hour:
        return (dt - timedelta(days=1)).strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d")


def get_dusk_to_dawn_dates(dt=None, dusk_hour=17):
    if dt is None:
        dt = datetime.now()
    if dt.hour < dusk_hour:
        start_d = dt - timedelta(days=1)
        end_d = dt
    else:
        start_d = dt
        end_d = dt + timedelta(days=1)
    return start_d, end_d


def _resolve_hours(hours_cfg, dusk_hour=17):
    cfg_str = str(hours_cfg).lower().strip()
    if cfg_str in ('dusk-to-dawn', 'dusk_to_dawn', 'night', 'tonight', 'dusk'):
        now = datetime.now()
        if now.hour >= dusk_hour:
            start_dt = now.replace(hour=dusk_hour, minute=0, second=0, microsecond=0)
        else:
            start_dt = (now - timedelta(days=1)).replace(hour=dusk_hour, minute=0, second=0, microsecond=0)
        elapsed = (now - start_dt).total_seconds()
        return max(1, int(math.ceil(elapsed / 3600.0)))
    if cfg_str in ('today', '0', 'day'):
        return max(1, datetime.now().hour + 1)
    try:
        return int(hours_cfg)
    except (ValueError, TypeError):
        return 24

def slugify(sci):
    return re.sub(r"[^a-z0-9]+", "-", sci.lower()).strip("-")


def _bucket(n):
    for i, edge in enumerate((1, 2, 5, 15, 40, 100, 300, 1000)):
        if n <= edge:
            return i
    return 8


def fetch_recent(base, hours, timeout, auth=None, dusk_hour=17):
    hours_mode = str(hours).lower().strip()
    if hours_mode in ('dusk-to-dawn', 'dusk_to_dawn', 'night', 'tonight', 'dusk'):
        now = datetime.now()
        if now.hour >= dusk_hour:
            start_dt = now.replace(hour=dusk_hour, minute=0, second=0, microsecond=0)
        else:
            start_dt = (now - timedelta(days=1)).replace(hour=dusk_hour, minute=0, second=0, microsecond=0)
        since_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        url = f"{base.rstrip('/')}/avian/api/birdnet-api.php?action=recent&hours=dusk-to-dawn&dusk_hour={dusk_hour}&since={urllib.parse.quote(since_str)}"
    elif hours_mode in ('today', '0', 'day'):
        url = f"{base.rstrip('/')}/avian/api/birdnet-api.php?action=recent&hours=today"
    else:
        h = _resolve_hours(hours, dusk_hour=dusk_hour)
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
    dusk_hour = int(cfg.get("dusk_hour", 17))
    return fetch_recent(cfg["base_url"], cfg["hours"], cfg["timeout"], auth, dusk_hour=dusk_hour)


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


def save_state(path, sig, when, last_night=None):
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        data = {
            "signature": sig,
            "last_refresh": when,
            "last_date": datetime.now().strftime("%Y-%m-%d"),
            "last_night": last_night or datetime.now().strftime("%Y-%m-%d")
        }
        json.dump(data, f)
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

def _tuning(n, cfg=None):
    cfg = cfg or {}
    return {
        "packingBudgetFrac": 0.52 if n <= 4 else (0.46 if n <= 8 else (0.40 if n <= 16 else (0.35 if n <= 24 else 0.30))),
        "countExp": float(cfg.get("shoot_count_exp", 0.22)),
        "smallFloor": float(cfg.get("shoot_small_floor", 0.15)),
        "minTileAreaFrac": 0.045 if n <= 4 else (0.022 if n <= 8 else (0.012 if n <= 16 else (0.008 if n <= 24 else 0.005))),
        "ellipseAspectBias": 1.2,
    }

def _get_font(font_path, size):
    try:
        return ImageFont.truetype(font_path, size)
    except Exception:
        return ImageFont.load_default()

def render_native_collage(species_list, cfg, style="vintage", title=None, subtitle=None, show_names=True):
    import numpy as np
    from PIL import ImageFilter
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

    CW = 1100
    CH = 1320
    collage_x0 = 50
    collage_y0 = rule_y + 35
    GRID_STRIDE = 6

    T = _tuning(len(species_list), cfg)
    vp_area = CW * CH
    # Scale vintage birds up by ~38% in linear dimension (1.90x area) to fill the 13.3" frame
    style_area_scale = 1.90 if style == "vintage" else 1.0
    budget = vp_area * T["packingBudgetFrac"] * style_area_scale
    min_area = vp_area * T["minTileAreaFrac"] * style_area_scale

    # Apply rare-bird floor fraction (small_floor) matching sketch mode
    if species_list and T["smallFloor"] > 0:
        max_calls = max(float(s.get("n") or 1) for s in species_list)
        floor_calls = max_calls * T["smallFloor"]
    else:
        floor_calls = 1.0

    # Pre-load cutouts & alpha masks
    raw_tiles = []
    for s in species_list:
        sci = s.get("sci", "")
        slug = slugify(sci)
        img_path = os.path.join(asset_dir, f"{slug}.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(asset_dir, f"{slug}-2.png")
        if not os.path.exists(img_path):
            img_path = os.path.join(asset_dir, "default.png")

        if not os.path.exists(img_path):
            continue

        try:
            im = Image.open(img_path).convert("RGBA")
        except Exception:
            continue

        bbox = im.getbbox()
        if bbox:
            im = im.crop(bbox)
        
        bw, bh = im.size
        ar = bw / float(bh)
        raw_n = float(s.get("n") or 1)
        eff_n = max(raw_n, floor_calls)
        score = math.pow(max(1.0, eff_n), T["countExp"])

        com = s.get("com", sci)
        l_bbox = draw.textbbox((0, 0), com, font=font_label)
        lbl_w = (l_bbox[2] - l_bbox[0]) if show_names else 0
        lbl_h = (l_bbox[3] - l_bbox[1] + 6) if show_names else 0

        raw_tiles.append({
            "im": im, "data": s, "sci": sci, "com": com, "slug": slug,
            "ar": ar, "score": score, "lbl_w": lbl_w, "lbl_h": lbl_h
        })

    if not raw_tiles:
        return canvas

    sum_score = sum(t["score"] for t in raw_tiles) or 1.0
    for t in raw_tiles:
        t["area"] = max(min_area, budget * t["score"] / sum_score)

    sum_a = sum(t["area"] for t in raw_tiles)
    if sum_a > budget:
        fixed_sum = sum(t["area"] for t in raw_tiles if t["area"] <= min_area + 1e-9)
        flex_sum = sum_a - fixed_sum
        flex_budget = max(0, budget - fixed_sum)
        shrink = (flex_budget / flex_sum) if flex_sum > 0 else 1.0
        for t in raw_tiles:
            if t["area"] > min_area + 1e-9:
                t["area"] *= shrink

    for t in raw_tiles:
        t["fullW"] = math.sqrt(t["area"] * t["ar"])
        t["fullH"] = t["fullW"] / t["ar"]

    def do_pack(tiles, scale_factor=1.0):
        GW = math.ceil(CW / GRID_STRIDE) + 4
        GH = math.ceil(CH / GRID_STRIDE) + 4
        grid = np.zeros((GH, GW), dtype=np.uint8)

        tile_objs = []
        for t in tiles:
            tw = max(16, int(t["fullW"] * scale_factor))
            th = max(16, int(t["fullH"] * scale_factor))
            
            alpha = t["im"].split()[-1].resize((tw, th), Image.Resampling.BILINEAR)
            gw = math.ceil(tw / GRID_STRIDE)
            gh = math.ceil(th / GRID_STRIDE)
            
            alpha_grid = alpha.resize((gw, gh), Image.Resampling.BILINEAR)
            arr = np.array(alpha_grid)
            bird_mask = (arr > 35).astype(np.uint8)

            lbl_gw = math.ceil(t["lbl_w"] / GRID_STRIDE) if show_names else 0
            lbl_gh = math.ceil(t["lbl_h"] / GRID_STRIDE) if show_names else 0

            tot_gw = max(gw, lbl_gw)
            tot_gh = gh + (lbl_gh + 1 if show_names else 0)

            c_mask = np.zeros((tot_gh, tot_gw), dtype=np.uint8)
            bx_off = (tot_gw - gw) // 2
            c_mask[:gh, bx_off:bx_off+gw] = bird_mask
            if show_names and lbl_gh > 0:
                lx_off = (tot_gw - lbl_gw) // 2
                c_mask[gh:gh+lbl_gh, lx_off:lx_off+lbl_gw] = 1

            pil_c = Image.fromarray(c_mask * 255)
            pil_dilated = pil_c.filter(ImageFilter.MaxFilter(5))
            s_mask = (np.array(pil_dilated) > 0).astype(np.uint8)

            tile_objs.append({
                "raw": t, "tw": tw, "th": th, "gw": tot_gw, "gh": tot_gh,
                "bird_gw": gw, "bird_gh": gh, "bx_off": bx_off,
                "c_mask": c_mask, "s_mask": s_mask,
                "area": tot_gw * tot_gh
            })

        tile_objs.sort(key=lambda o: o["area"], reverse=True)

        cx = GW // 2
        cy = GH // 2
        placed = []

        for i, obj in enumerate(tile_objs):
            gw, gh = obj["gw"], obj["gh"]
            c_mask = obj["c_mask"]
            s_mask = obj["s_mask"]

            if i == 0:
                tx = cx - gw // 2
                ty = cy - gh // 2 - 4
                grid[ty:ty+gh, tx:tx+gw] = np.maximum(grid[ty:ty+gh, tx:tx+gw], s_mask)
                obj["gx"] = tx
                obj["gy"] = ty
                placed.append(obj)
                continue

            best = None
            step_r = 2
            max_r = max(GW, GH)

            for r in range(4, max_r, step_r):
                samples = max(24, min(64, int(r * 2.2)))
                for k in range(samples):
                    theta = (i * 2.39996) + (k / float(samples)) * math.pi * 2.0
                    px = int(cx + r * 1.0 * math.cos(theta) - gw / 2.0)
                    py = int(cy + r * 1.35 * math.sin(theta) - gh / 2.0)

                    if px < 2 or px + gw >= GW - 2 or py < 2 or py + gh >= GH - 2:
                        continue

                    sub = grid[py:py+gh, px:px+gw]
                    if not sub.any():
                        best = (px, py)
                        break
                    if not np.bitwise_and(sub, c_mask).any():
                        best = (px, py)
                        break
                if best:
                    break

            if best:
                px, py = best
                grid[py:py+gh, px:px+gw] = np.maximum(grid[py:py+gh, px:px+gw], s_mask)
                obj["gx"] = px
                obj["gy"] = py
                placed.append(obj)
            else:
                return None

        return placed

    scale = 1.0
    placed_tiles = None
    for _ in range(12):
        placed_tiles = do_pack(raw_tiles, scale_factor=scale)
        if placed_tiles is not None and len(placed_tiles) == len(raw_tiles):
            break
        scale *= 0.91

    if not placed_tiles:
        placed_tiles = do_pack(raw_tiles, scale_factor=0.5)

    min_x, max_x = float("inf"), float("-inf")
    min_y, max_y = float("inf"), float("-inf")
    for obj in placed_tiles:
        px = obj["gx"] * GRID_STRIDE
        py = obj["gy"] * GRID_STRIDE
        pw = obj["gw"] * GRID_STRIDE
        ph = obj["gh"] * GRID_STRIDE
        if px < min_x: min_x = px
        if px + pw > max_x: max_x = px + pw
        if py < min_y: min_y = py
        if py + ph > max_y: max_y = py + ph

    cl_w = max_x - min_x
    cl_h = max_y - min_y
    off_x = collage_x0 + (CW - cl_w) // 2 - min_x
    off_y = collage_y0 + (CH - cl_h) // 2 - min_y

    placed_count = 0
    for obj in placed_tiles:
        raw = obj["raw"]
        bx = int(obj["gx"] * GRID_STRIDE + obj["bx_off"] * GRID_STRIDE + off_x)
        by = int(obj["gy"] * GRID_STRIDE + off_y)
        tw, th = obj["tw"], obj["th"]

        scaled_im = raw["im"].resize((tw, th), Image.Resampling.LANCZOS)
        canvas.paste(scaled_im, (bx, by), scaled_im)
        placed_count += 1

        if show_names:
            com = raw["com"]
            l_bbox = draw.textbbox((0, 0), com, font=font_label)
            lw = l_bbox[2] - l_bbox[0]
            lx = bx + (tw - lw) // 2
            ly = by + th + 3
            draw.text((lx, ly), com, font=font_label, fill=text_color)

    footer_text = f"{placed_count} of {len(species_list)} species heard today"
    font_foot = _get_font(font_title, 20)
    f_bbox = draw.textbbox((0, 0), footer_text, font=font_foot)
    draw.text(((PANEL_W - (f_bbox[2] - f_bbox[0])) // 2, PANEL_H - 50), footer_text, font=font_foot, fill=subtext_color)

    return canvas


def obtain_image(cfg, species=None):
    style = str(cfg.get("style", cfg.get("art_style", "vintage"))).lower()
    base_url = str(cfg.get("base_url", "")).lower()
    is_bats = ("192.168.1.65" in base_url or "bat" in str(cfg.get("active_node", "")).lower() or cfg.get("hours") == "dusk-to-dawn")
    
    here = os.path.dirname(os.path.abspath(__file__))
    assets_root = os.path.join(here, "assets")

    if is_bats:
        if species is None:
            species = fetch_species(cfg, _auth(cfg))
        if style in ("cartoon", "cave"):
            return render_cartoon_cave_bats(species, os.path.join(assets_root, "cartoon_bats"), cfg=cfg)
        elif style == "vintage":
            return render_vintage_audubon_bats(species, os.path.join(assets_root, "vintage_bats"), cfg=cfg)
        else: # "sketch", "gothic", "belfry", default
            return render_naturalist_specimen_plate(species, os.path.join(assets_root, "gothic"), cfg=cfg)

    # Otherwise Birds:
    if style == "sketch" and (cfg.get("shoot") or not (cfg.get("image_url") or cfg.get("image"))):
        from shoot import shoot
        out = os.path.join(os.path.expanduser(cfg["cache"]), "shot.png")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if species is None:
            species = fetch_species(cfg, _auth(cfg))
        window_hours = _resolve_hours(cfg.get("hours", 24))
        shoot(
            cfg["base_url"],
            out,
            title=cfg.get("shoot_title"),
            subtitle=cfg.get("shoot_subtitle"),
            headline_px=cfg.get("shoot_headline_px", 42),
            eyebrow_px=cfg.get("shoot_eyebrow_px", 18),
            lowercase=cfg.get("shoot_lowercase", False),
            mat=cfg.get("shoot_mat", 0.04),
            small_floor=cfg.get("shoot_small_floor", 0.15),
            count_exp=cfg.get("shoot_count_exp", 0.22),
            timeout_ms=cfg.get("timeout", 180) * 1000,
            user=cfg.get("basic_user"),
            password=cfg.get("basic_pass"),
            window_hours=window_hours,
            species=species,
            bird_names=cfg.get("bird_names", True),
        )
        return Image.open(out).convert("RGB")
    elif style in ("vintage", "cartoon") and (cfg.get("shoot") or not (cfg.get("image_url") or cfg.get("image"))):
        if species is None:
            species = fetch_species(cfg, _auth(cfg))
        title = cfg.get("shoot_title") or "Avian Visitors"
        subtitle = cfg.get("shoot_subtitle") or "Heard Today"
        show_names = bool(cfg.get("bird_names", True))
        return render_native_collage(species, cfg, style=style, title=title, subtitle=subtitle, show_names=show_names)
    src = cfg["image_url"] or cfg["image"]
    if not src:
        raise ValueError("set image, image_url, or shoot in config")
    if cfg["image_url"]:
        src = frame_url(src, cfg["bird_names"])
    return get_image(src, cfg["timeout"], _auth(cfg))



def _load_font(candidates, size):
    for fp in candidates:
        if fp and os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                pass
    return ImageFont.load_default()

FONTS_BOLD = [
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Baskerville.ttc",
    "/Library/Fonts/Garamond.ttf",
    "/System/Library/Fonts/Times.ttc"
]

FONTS_ITALIC = [
    "/home/birder/.local/share/fonts/LibreBaskerville-Italic.ttf",
    "/home/birder/.local/share/fonts/EBGaramond-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    "/System/Library/Fonts/Supplemental/Baskerville.ttc"
]

def get_moon_phase(dt=None):
    if dt is None:
        dt = datetime.now()
    ref = datetime(2000, 1, 6, 18, 14)
    diff = (dt - ref).total_seconds() / 86400.0
    synodic = 29.53058867
    cycle = (diff % synodic) / synodic
    angle = cycle * 2 * math.pi
    illum = (1 - math.cos(angle)) / 2.0
    
    if cycle < 0.03 or cycle > 0.97:
        phase_name = "New Moon"
    elif cycle < 0.22:
        phase_name = "Waxing Crescent"
    elif cycle < 0.28:
        phase_name = "First Quarter"
    elif cycle < 0.47:
        phase_name = "Waxing Gibbous"
    elif cycle < 0.53:
        phase_name = "Full Moon"
    elif cycle < 0.72:
        phase_name = "Waning Gibbous"
    elif cycle < 0.78:
        phase_name = "Last Quarter"
    else:
        phase_name = "Waning Crescent"
        
    return cycle, illum, phase_name


def draw_moon_medallion(canvas, cx, cy, r=52, dt=None):
    cycle, illum, phase_name = get_moon_phase(dt)
    draw = ImageDraw.Draw(canvas)
    
    # Outer astrological rings
    draw.ellipse([cx - r - 16, cy - r - 16, cx + r + 16, cy + r + 16], outline=(70, 55, 40, 255), width=2)
    draw.ellipse([cx - r - 12, cy - r - 12, cx + r + 12, cy + r + 12], outline=(140, 115, 85, 180), width=1)
    draw.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4], outline=(50, 40, 30, 255), width=2)
    
    # Tick marks around circle
    for deg in range(0, 360, 15):
        rad = math.radians(deg)
        r1 = r + 5
        r2 = r + 11 if deg % 90 == 0 else (r + 9 if deg % 45 == 0 else r + 7)
        x1 = cx + r1 * math.cos(rad)
        y1 = cy + r1 * math.sin(rad)
        x2 = cx + r2 * math.cos(rad)
        y2 = cy + r2 * math.sin(rad)
        draw.line([x1, y1, x2, y2], fill=(60, 45, 30, 255), width=1 if deg % 45 != 0 else 2)
        
    # Night sky disc inside moon medallion
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(22, 28, 44, 255), outline=(40, 30, 20, 255), width=2)
    
    # Stars
    np.random.seed(42)
    for _ in range(25):
        sx = cx + np.random.uniform(-r * 0.85, r * 0.85)
        sy = cy + np.random.uniform(-r * 0.85, r * 0.85)
        if (sx - cx)**2 + (sy - cy)**2 < (r - 4)**2:
            draw.point((sx, sy), fill=(220, 225, 235, 200))
            if np.random.rand() > 0.7:
                draw.line([sx-1, sy, sx+1, sy], fill=(255, 255, 255, 220))
                draw.line([sx, sy-1, sx, sy+1], fill=(255, 255, 255, 220))
                
    # Moon illuminated shape
    m_size = r * 4
    m_mask = Image.new("L", (m_size, m_size), 0)
    m_draw = ImageDraw.Draw(m_mask)
    mc = m_size // 2
    mr = r * 2
    
    for y_rel in range(-mr, mr + 1):
        y_sq = mr * mr - y_rel * y_rel
        if y_sq < 0:
            continue
        x_limb = math.sqrt(y_sq)
        x_term = x_limb * math.cos(cycle * 2 * math.pi)
        
        my = mc + y_rel
        if cycle < 0.5:
            x_start = mc + x_term
            x_end = mc + x_limb
        else:
            x_start = mc - x_limb
            x_end = mc + x_term
            
        if x_start > x_end:
            x_start, x_end = x_end, x_start
        m_draw.line([x_start, my, x_end, my], fill=255, width=1)
        
    m_mask = m_mask.resize((r * 2, r * 2), Image.Resampling.LANCZOS)
    
    moon_surface = Image.new("RGBA", (r * 2, r * 2), (245, 240, 222, 255))
    s_draw = ImageDraw.Draw(moon_surface)
    craters = [
        (-0.2, -0.3, 0.22), (0.1, -0.1, 0.3), (-0.3, 0.2, 0.25),
        (0.25, 0.3, 0.18), (0.0, 0.4, 0.2), (-0.1, -0.05, 0.15)
    ]
    for cx_f, cy_f, cr_f in craters:
        ccx = r + cx_f * r
        ccy = r + cy_f * r
        ccr = cr_f * r
        s_draw.ellipse([ccx - ccr, ccy - ccr, ccx + ccr, ccy + ccr], fill=(225, 218, 198, 255), outline=(195, 188, 168, 180), width=1)
        
    moon_surface.putalpha(m_mask)
    canvas.alpha_composite(moon_surface, (cx - r, cy - r))
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(60, 45, 30, 255), width=1)
    
    font_sub = _load_font(FONTS_BOLD, 14)
    p_text = f"{phase_name} • {int(illum * 100)}%"
    bbox = draw.textbbox((0, 0), p_text, font=font_sub)
    tw = bbox[2] - bbox[0]
    draw.text((cx - tw // 2, cy + r + 20), p_text, fill=(45, 35, 25, 255), font=font_sub)


def draw_thorny_border(canvas, W=1200, H=1600):
    draw = ImageDraw.Draw(canvas)
    margin = 35
    x1, y1 = margin, margin
    x2, y2 = W - margin, H - margin
    
    draw.rectangle([x1, y1, x2, y2], outline=(50, 38, 25, 255), width=2)
    draw.rectangle([x1 + 6, y1 + 6, x2 - 6, y2 - 6], outline=(140, 115, 85, 160), width=1)
    draw.rectangle([x1 + 18, y1 + 18, x2 - 18, y2 - 18], outline=(60, 45, 30, 255), width=1)
    
    for cx, cy in [(x1+18, y1+18), (x2-18, y1+18), (x1+18, y2-18), (x2-18, y2-18)]:
        draw.ellipse([cx-10, cy-10, cx+10, cy+10], outline=(60, 45, 30, 255), width=2)
        draw.ellipse([cx-5, cy-5, cx+5, cy+5], fill=(70, 50, 35, 255))
        
    vx1, vx2 = x1 + 25, x2 - 25
    vy = y1 + 12
    draw.line([vx1, vy, vx2, vy], fill=(45, 32, 20, 255), width=3)
    for tx in range(vx1 + 30, vx2 - 30, 40):
        tip_dir = -8 if (tx // 40) % 2 == 0 else 8
        draw.polygon([(tx - 4, vy), (tx, vy + tip_dir), (tx + 4, vy)], fill=(45, 32, 20, 255))
        
    vy_b = y2 - 12
    draw.line([vx1, vy_b, vx2, vy_b], fill=(45, 32, 20, 255), width=3)
    for tx in range(vx1 + 30, vx2 - 30, 40):
        tip_dir = -8 if (tx // 40) % 2 == 0 else 8
        draw.polygon([(tx - 4, vy_b), (tx, vy_b + tip_dir), (tx + 4, vy_b)], fill=(45, 32, 20, 255))
        
    vy1, vy2 = y1 + 25, y2 - 25
    vx_l = x1 + 12
    draw.line([vx_l, vy1, vx_l, vy2], fill=(45, 32, 20, 255), width=3)
    for ty in range(vy1 + 30, vy2 - 30, 45):
        tip_dir = -8 if (ty // 45) % 2 == 0 else 8
        draw.polygon([(vx_l, ty - 4), (vx_l + tip_dir, ty), (vx_l, ty + 4)], fill=(45, 32, 20, 255))
        
    vx_r = x2 - 12
    draw.line([vx_r, vy1, vx_r, vy2], fill=(45, 32, 20, 255), width=3)
    for ty in range(vy1 + 30, vy2 - 30, 45):
        tip_dir = -8 if (ty // 45) % 2 == 0 else 8
        draw.polygon([(vx_r, ty - 4), (vx_r + tip_dir, ty), (vx_r, ty + 4)], fill=(45, 32, 20, 255))


def draw_thorny_perch_branch(draw, x_start, x_end, y_pos):
    draw.line([x_start, y_pos, x_end, y_pos], fill=(50, 36, 24, 255), width=6)
    draw.line([x_start + 4, y_pos - 1, x_end - 4, y_pos - 1], fill=(130, 105, 75, 200), width=2)
    for bx in range(x_start + 25, x_end - 20, 35):
        draw.line([bx, y_pos - 3, bx + 12, y_pos + 3], fill=(35, 25, 16, 255), width=1)
    for tx in [x_start + 15, x_start + 45, x_end - 45, x_end - 15]:
        draw.polygon([(tx - 3, y_pos - 2), (tx, y_pos - 10), (tx + 3, y_pos - 2)], fill=(45, 32, 20, 255))


def draw_ornate_header(canvas, cx=520, cy=115, subtitle=None):
    draw = ImageDraw.Draw(canvas)
    font_title = _load_font(FONTS_BOLD, 46)
    font_sub = _load_font(FONTS_ITALIC, 20)
        
    title = "GothicVisitors"
    if not subtitle:
        subtitle = "• Nocturnal Naturalist Field Plate •"
    
    bbox1 = draw.textbbox((0, 0), title, font=font_title)
    tw1, th1 = bbox1[2] - bbox1[0], bbox1[3] - bbox1[1]
    
    bbox2 = draw.textbbox((0, 0), subtitle, font=font_sub)
    tw2, th2 = bbox2[2] - bbox2[0], bbox2[3] - bbox2[1]
    
    banner_w = max(tw1, tw2) + 120
    banner_h = th1 + th2 + 36
    bx1 = cx - banner_w // 2
    by1 = cy - banner_h // 2
    
    draw.rounded_rectangle([bx1, by1, bx1 + banner_w, by1 + banner_h], radius=8, fill=(244, 238, 224, 255), outline=(50, 38, 25, 255), width=2)
    draw.rounded_rectangle([bx1 + 4, by1 + 4, bx1 + banner_w - 4, by1 + banner_h - 4], radius=6, outline=(140, 115, 85, 180), width=1)
    
    draw.text((cx - tw1 // 2, by1 + 10), title, fill=(25, 18, 12, 255), font=font_title)
    draw.text((cx - tw2 // 2, by1 + 10 + th1 + 8), subtitle, fill=(90, 70, 50, 255), font=font_sub)


def create_species_plaque(com_name, sci_name, call_count, width=650, height=66, num=3):
    plaque = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(plaque)
    
    draw.rounded_rectangle([2, 2, width - 3, height - 3], radius=8, fill=(245, 239, 226, 255), outline=(45, 32, 20, 255), width=2)
    draw.rounded_rectangle([5, 5, width - 6, height - 6], radius=6, outline=(140, 115, 85, 200), width=1)
    
    f1_size = 21 if num >= 4 else 26
    f2_size = 14 if num >= 4 else 18
    font_main = _load_font(FONTS_BOLD, f1_size)
    font_sub = _load_font(FONTS_ITALIC, f2_size)
        
    line1 = com_name.title()
    count_str = f"{call_count:,} Calls Heard Tonight" if call_count > 1 else "1 Call Heard Tonight"
    line2 = f"{count_str}   •   {sci_name}"
    
    b1 = draw.textbbox((0, 0), line1, font=font_main)
    tw1, th1 = b1[2] - b1[0], b1[3] - b1[1]
    
    b2 = draw.textbbox((0, 0), line2, font=font_sub)
    tw2, th2 = b2[2] - b2[0], b2[3] - b2[1]
    
    top_pad = 5 if num >= 4 else 7
    line_gap = 3 if num >= 4 else 6
    draw.text(((width - tw1) // 2, top_pad), line1, fill=(24, 16, 10, 255), font=font_main)
    draw.text(((width - tw2) // 2, top_pad + th1 + line_gap), line2, fill=(90, 70, 50, 255), font=font_sub)
    
    return plaque


def render_naturalist_specimen_plate(species_list, asset_dir="/home/birder/AvianVisitors/frame/assets/gothic", cfg=None):
    W, H = 1200, 1600
    canvas = Image.new("RGBA", (W, H), (247, 243, 233, 255))
    draw = ImageDraw.Draw(canvas)
    
    # Outer thorny vine border framing entire plate
    draw_thorny_border(canvas, W, H)
    
    # Real-time celestial lunar phase medallion
    draw_moon_medallion(canvas, cx=1060, cy=115, r=52)
    
    dusk_hour = int(cfg.get("dusk_hour", 17)) if cfg else 18
    start_d, end_d = get_dusk_to_dawn_dates(dusk_hour=dusk_hour)
    date_span_str = f"{start_d.strftime('%b')} {start_d.day}–{end_d.day}"
    header_subtitle = f"• Dusk to Dawn • {date_span_str} •"
    
    # Gothic header
    draw_ornate_header(canvas, cx=520, cy=115, subtitle=header_subtitle)
    
    num = max(1, min(5, len(species_list)))
    tier_top = 215
    tier_bottom = 1535
    total_avail_h = tier_bottom - tier_top
    tier_h = total_avail_h // num
    
    plaque_h = 52 if num >= 4 else 64
    avail_bat_h = tier_h - plaque_h - 10
    
    for i in range(num):
        sp = species_list[i]
        sci = sp.get("sci", "")
        com = sp.get("com", sci)
        n = sp.get("n", 1)
        slug = sci.lower().replace(" ", "-")
        genus = slug.split("-")[0] if "-" in slug else slug
        
        y_start = tier_top + i * tier_h
        
        # --- ROOSTING BAT (LEFT SIDE) ---
        roost_path = os.path.join(asset_dir, f"{slug}.png")
        if not os.path.exists(roost_path):
            roost_path = os.path.join(asset_dir, f"{genus}.png")
        if not os.path.exists(roost_path):
            roost_path = os.path.join(asset_dir, "tadarida-brasiliensis.png")
            
        roost_img = Image.open(roost_path).convert("RGBA")
        rw, rh = roost_img.size
        # Scaled roosting bat
        if num == 1:
            target_rh = min(avail_bat_h - 10, 520)
        elif num == 2:
            target_rh = min(avail_bat_h - 10, 420)
        elif num == 3:
            target_rh = min(avail_bat_h - 10, 305)
        elif num == 4:
            target_rh = min(avail_bat_h - 10, 230)
        else: # 5
            target_rh = min(avail_bat_h - 10, 180)
            
        target_rw = int(target_rh * (rw / rh))
        roost_scaled = roost_img.resize((target_rw, target_rh), Image.Resampling.LANCZOS)
        
        left_cx = 250
        rx = left_cx - target_rw // 2
        ry = y_start + (8 if num >= 4 else 15)
        
        # Horizontal thorny perch branch for claws to grip
        branch_y = ry + 8
        draw_thorny_perch_branch(draw, rx - 30, rx + target_rw + 30, branch_y)
        canvas.alpha_composite(roost_scaled, (rx, ry))
        
        # --- FLYING BAT (RIGHT SIDE) ---
        flight_path = os.path.join(asset_dir, f"{slug}-flight.png")
        if not os.path.exists(flight_path):
            flight_path = os.path.join(asset_dir, f"{genus}-flight.png")
        if not os.path.exists(flight_path):
            flight_path = os.path.join(asset_dir, "tadarida-brasiliensis-flight.png")
            
        flight_img = Image.open(flight_path).convert("RGBA")
        fw, fh = flight_img.size
        
        # Scaled flight wingspan
        if num == 1:
            target_fw = 760
        elif num == 2:
            target_fw = 620
        elif num == 3:
            target_fw = 520
        elif num == 4:
            target_fw = 450
        else: # 5
            target_fw = 380
            
        target_fh = int(target_fw / (fw / fh))
        if target_fh > avail_bat_h:
            target_fh = avail_bat_h
            target_fw = int(target_fh * (fw / fh))
        flight_scaled = flight_img.resize((target_fw, target_fh), Image.Resampling.LANCZOS)
        
        right_cx = 780
        fx = right_cx - target_fw // 2
        fy = y_start + (avail_bat_h - target_fh) // 2 + (4 if num >= 4 else 5)
        canvas.alpha_composite(flight_scaled, (fx, fy))
        
        # --- LARGE SPECIES PLAQUE (BELOW) ---
        plaque_w = min(780, max(520, int(len(com) * (14 if num >= 4 else 16)) + 120))
        plaque = create_species_plaque(com, sci, n, width=plaque_w, height=plaque_h, num=num)
        pl_x = (W - plaque_w) // 2
        pl_y = y_start + avail_bat_h + 6
        canvas.alpha_composite(plaque, (pl_x, pl_y))
        
    site_label = cfg.get("active_node", "Battybird").split("(")[0].strip() if cfg else "Battybird"
    footer_text = f"• {site_label} Acoustic Station • Dusk to Dawn • {date_span_str} •"
    f_font = _load_font(FONTS_BOLD, 15)
    bbox_f = draw.textbbox((0, 0), footer_text, font=f_font)
    fw = bbox_f[2] - bbox_f[0]
    draw.text(((W - fw) // 2, H - 30), footer_text, fill=(100, 80, 60, 255), font=f_font)
    
    return canvas.convert("RGB")


def render_cartoon_cave_bats(species_list, asset_dir, cfg=None):
    bg_path = os.path.join(asset_dir, "cave_background.png")
    if os.path.exists(bg_path):
        canvas = Image.open(bg_path).convert("RGBA").resize((PANEL_W, PANEL_H), Image.Resampling.LANCZOS)
    else:
        canvas = Image.new("RGBA", (PANEL_W, PANEL_H), (20, 25, 35, 255))
    
    draw = ImageDraw.Draw(canvas)
    
    head_w, head_h = 580, 95
    hx = (PANEL_W - head_w) // 2
    hy = 35
    draw.rounded_rectangle([hx, hy, hx + head_w, hy + head_h], radius=16, fill=(35, 30, 45, 230), outline=(230, 200, 150), width=3)
    
    font_cartoon = "/home/birder/.local/share/fonts/FingerPaint-Regular.ttf"
    if not os.path.exists(font_cartoon):
        font_cartoon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "FingerPaint-Regular.ttf")
    f_title = _load_font([font_cartoon], 36)
    f_sub = _load_font([font_cartoon], 18)
    
    t_text = (cfg.get("shoot_title") or "Gothic Visitors") if cfg else "Gothic Visitors"
    tb = draw.textbbox((0, 0), t_text, font=f_title)
    draw.text((hx + (head_w - (tb[2]-tb[0]))//2, hy + 12), t_text, font=f_title, fill=(255, 240, 210))
    
    dusk_hour = int(cfg.get("dusk_hour", 17)) if cfg else 17
    now = datetime.now()
    active_date = active_night_date(now, dusk_hour=dusk_hour)
    dt_active = datetime.strptime(active_date, "%Y-%m-%d")
    next_day = dt_active + timedelta(days=1)
    date_span_str = f"{dt_active.strftime('%b %d')}–{next_day.strftime('%d')}"
    
    sub_text = f"Heard Tonight • Dusk to Dawn • {date_span_str}"
    sb = draw.textbbox((0, 0), sub_text, font=f_sub)
    draw.text((hx + (head_w - (sb[2]-sb[0]))//2, hy + 58), sub_text, font=f_sub, fill=(220, 190, 150))
    
    N = min(len(species_list), 5) if species_list else 0
    if N == 0:
        empty_font = _load_font([font_cartoon], 30)
        msg = "listening for bats in the cavern…"
        mb = draw.textbbox((0, 0), msg, font=empty_font)
        draw.text(((PANEL_W - (mb[2]-mb[0]))//2, PANEL_H // 2), msg, font=empty_font, fill=(230, 210, 180))
        return canvas.convert("RGB")
        
    if N == 1:
        coords = [(600, 220, 360)]
    elif N == 2:
        coords = [(380, 240, 310), (820, 270, 310)]
    elif N == 3:
        coords = [(280, 220, 270), (600, 320, 280), (920, 220, 270)]
    elif N == 4:
        coords = [
            (230, 200, 235),  # Left high stalactite
            (470, 360, 235),  # Center-left lower arch
            (730, 200, 235),  # Center-right high stalactite
            (970, 360, 235)   # Right lower rock overhang
        ]
    else: # 5 bats
        coords = [
            (180, 190, 215),  # Far left upper stalactite
            (390, 340, 225),  # Mid-left lower ledge
            (600, 180, 230),  # Center high stalactite
            (810, 340, 225),  # Mid-right lower ledge
            (1020, 190, 215)  # Far right upper stalactite
        ]
        
    for i in range(N):
        sp = species_list[i]
        slug = slugify(sp["sci"])
        fn = os.path.join(asset_dir, f"{slug}.png")
        if not os.path.exists(fn):
            genus = slug.split("-")[0] if "-" in slug else slug
            fn = os.path.join(asset_dir, f"{genus}.png")
        if not os.path.exists(fn):
            fn = os.path.join(asset_dir, "default.png")
        if not os.path.exists(fn):
            for cand in ["tadarida-brasiliensis.png", "lasiurus-borealis.png", "lasiurus-seminolus.png"]:
                cand_path = os.path.join(asset_dir, cand)
                if os.path.exists(cand_path):
                    fn = cand_path
                    break
        if not os.path.exists(fn):
            continue
            
        bat_img = Image.open(fn).convert("RGBA")
        cx, top_y, tw = coords[i]
        scale = tw / float(bat_img.width)
        th = int(bat_img.height * scale)
        bat_scaled = bat_img.resize((tw, th), Image.Resampling.LANCZOS)
        
        bx = cx - tw // 2
        by = top_y + 40
        
        foot_lx = cx - int(tw * 0.12)
        foot_rx = cx + int(tw * 0.12)
        anchor_x = cx
        anchor_y = top_y - 20
        draw.line([(anchor_x - 10, anchor_y), (foot_lx, by + 15)], fill=(60, 45, 35), width=3)
        draw.line([(anchor_x + 10, anchor_y), (foot_rx, by + 15)], fill=(60, 45, 35), width=3)
        
        canvas.alpha_composite(bat_scaled, (bx, by))
        
        pl_w = min(210, max(195, tw + 5)) if N >= 5 else min(280, max(210, tw + 20))
        pl_h = 58 if N >= 5 else 60
        pl_x = cx - pl_w // 2
        pl_y = by + th + 12
        
        draw.line([(cx - int(tw * 0.25), by + th - 10), (pl_x + 20, pl_y)], fill=(80, 60, 40), width=2)
        draw.line([(cx + int(tw * 0.25), by + th - 10), (pl_x + pl_w - 20, pl_y)], fill=(80, 60, 40), width=2)
        
        draw.rounded_rectangle([pl_x, pl_y, pl_x + pl_w, pl_y + pl_h], radius=10, fill=(250, 242, 225, 245), outline=(70, 50, 35), width=2)
        f_name = _load_font([font_cartoon], 14 if len(sp["com"]) > 18 else 16) if N >= 5 else _load_font([font_cartoon], 15 if len(sp["com"]) > 18 else 17)
        f_cnt = _load_font([font_cartoon], 12 if N >= 5 else 13)
        
        c_com = sp["com"]
        cb = draw.textbbox((0, 0), c_com, font=f_name)
        draw.text((pl_x + (pl_w - (cb[2]-cb[0]))//2, pl_y + (6 if N >= 5 else 8)), c_com, font=f_name, fill=(40, 25, 15))
        
        calls = int(sp.get("n", 1))
        cnt_str = f"{calls:,} Calls Tonight" if calls != 1 else "1 Call Tonight"
        cnb = draw.textbbox((0, 0), cnt_str, font=f_cnt)
        draw.text((pl_x + (pl_w - (cnb[2]-cnb[0]))//2, pl_y + (30 if N >= 5 else 32)), cnt_str, font=f_cnt, fill=(110, 75, 40))
        
    site_label = cfg.get("active_node", "Battybird").split("(")[0].strip() if cfg else "Battybird"
    foot_text = f"• {site_label} Acoustic Station • Dusk to Dawn • {date_span_str} •"
    f_ft = _load_font([font_cartoon], 16)
    fb = draw.textbbox((0, 0), foot_text, font=f_ft)
    fw = fb[2] - fb[0]
    draw.rounded_rectangle([(PANEL_W - fw)//2 - 15, PANEL_H - 45, (PANEL_W + fw)//2 + 15, PANEL_H - 15], radius=8, fill=(30, 25, 38, 220), outline=(200, 175, 130), width=1)
    draw.text(((PANEL_W - fw)//2, PANEL_H - 40), foot_text, font=f_ft, fill=(240, 225, 200))
    
    return canvas.convert("RGB")


def render_vintage_audubon_bats(species_list, asset_dir, cfg=None):
    bg_color = (250, 247, 240)
    canvas = Image.new("RGB", (PANEL_W, PANEL_H), bg_color)
    draw = ImageDraw.Draw(canvas)
    
    m = 25
    draw.rectangle([m, m, PANEL_W - m, PANEL_H - m], outline=(80, 65, 50), width=3)
    draw.rectangle([m + 8, m + 8, PANEL_W - m - 8, PANEL_H - m - 8], outline=(160, 140, 120), width=1)
    
    head_w = 640
    hx = (PANEL_W - head_w) // 2
    hy = 45
    f_eyebrow = _load_font(FONTS_BOLD, 16)
    f_title = _load_font(FONTS_BOLD, 36)
    f_sub = _load_font(FONTS_ITALIC, 17)
    
    eyebrow = "CHIROPTERA • NOCTURNAL VISITORS"
    eb = draw.textbbox((0, 0), eyebrow, font=f_eyebrow)
    draw.text((hx + (head_w - (eb[2]-eb[0]))//2, hy), eyebrow, font=f_eyebrow, fill=(130, 105, 80))
    
    title = (cfg.get("shoot_title") or "Gothic Visitors") if cfg else "Gothic Visitors"
    tb = draw.textbbox((0, 0), title, font=f_title)
    draw.text((hx + (head_w - (tb[2]-tb[0]))//2, hy + 26), title, font=f_title, fill=(40, 30, 20))
    
    dusk_hour = int(cfg.get("dusk_hour", 17)) if cfg else 17
    now = datetime.now()
    active_date = active_night_date(now, dusk_hour=dusk_hour)
    dt_active = datetime.strptime(active_date, "%Y-%m-%d")
    next_day = dt_active + timedelta(days=1)
    date_span_str = f"{dt_active.strftime('%B %d')}–{next_day.strftime('%d')}"
    
    sub = f"• Dusk to Dawn • {date_span_str} •"
    sb = draw.textbbox((0, 0), sub, font=f_sub)
    draw.text((hx + (head_w - (sb[2]-sb[0]))//2, hy + 72), sub, font=f_sub, fill=(100, 80, 60))
    
    N = min(len(species_list), 5) if species_list else 0
    if N == 0:
        empty_font = _load_font(FONTS_ITALIC, 28)
        msg = "listening for nocturnal visitors…"
        mb = draw.textbbox((0, 0), msg, font=empty_font)
        draw.text(((PANEL_W - (mb[2]-mb[0]))//2, PANEL_H // 2), msg, font=empty_font, fill=(130, 110, 90))
        return canvas
        
    if N == 1:
        grid_cells = [(170, 180, 860, 1260)]
    elif N == 2:
        grid_cells = [
            (100, 180, 1000, 630),
            (100, 830, 1000, 630)
        ]
    elif N == 3:
        grid_cells = [
            (340, 170, 520, 640),
            (65, 840, 520, 640),
            (615, 840, 520, 640)
        ]
    elif N == 4:
        grid_cells = [
            (65, 170, 520, 640),    # Top-Left
            (615, 170, 520, 640),   # Top-Right
            (65, 840, 520, 640),    # Bottom-Left
            (615, 840, 520, 640)    # Bottom-Right
        ]
    else: # 5 bats
        grid_cells = [
            (65, 170, 520, 640),    # Top Row - Left
            (615, 170, 520, 640),   # Top Row - Right
            (50, 840, 350, 660),    # Bottom Row - Left
            (425, 840, 350, 660),   # Bottom Row - Center
            (800, 840, 350, 660)    # Bottom Row - Right
        ]
        
    for i in range(N):
        sp = species_list[i]
        slug = slugify(sp["sci"])
        plate_fn = os.path.join(asset_dir, f"{slug}.png")
        if not os.path.exists(plate_fn):
            genus = slug.split("-")[0] if "-" in slug else slug
            plate_fn = os.path.join(asset_dir, f"{genus}.png")
        if not os.path.exists(plate_fn):
            plate_fn = os.path.join(asset_dir, "default.png")
        if not os.path.exists(plate_fn):
            for cand in ["tadarida-brasiliensis.png", "lasiurus-borealis.png", "lasiurus-seminolus.png"]:
                cand_path = os.path.join(asset_dir, cand)
                if os.path.exists(cand_path):
                    plate_fn = cand_path
                    break
        if not os.path.exists(plate_fn):
            continue
            
        gx, gy, gw, gh = grid_cells[i]
        draw.rectangle([gx, gy, gx + gw, gy + gh], outline=(200, 185, 165), width=1)
        
        plate_img = Image.open(plate_fn).convert("RGB")
        avail_img_h = gh - 85
        scale = min(gw / plate_img.width, avail_img_h / plate_img.height) * 0.96
        pw = int(plate_img.width * scale)
        ph = int(plate_img.height * scale)
        plate_scaled = plate_img.resize((pw, ph), Image.Resampling.LANCZOS)
        
        px = gx + (gw - pw) // 2
        py = gy + (avail_img_h - ph) // 2 + 5
        canvas.paste(plate_scaled, (px, py))
        
        cn = sp["com"]
        sn = sp["sci"]
        calls = int(sp.get("n", 1))
        
        if gw < 400:
            f_cname = _load_font(FONTS_BOLD, 15 if len(cn) > 18 else 16)
            f_sname = _load_font(FONTS_ITALIC, 13)
            f_count = _load_font(FONTS_BOLD, 12)
            y_cname = gy + gh - 66
            y_sname = gy + gh - 45
            y_count = gy + gh - 24
        elif N > 1:
            f_cname = _load_font(FONTS_BOLD, 18)
            f_sname = _load_font(FONTS_ITALIC, 15)
            f_count = _load_font(FONTS_BOLD, 13)
            y_cname = gy + gh - 68
            y_sname = gy + gh - 46
            y_count = gy + gh - 24
        else:
            f_cname = _load_font(FONTS_BOLD, 24)
            f_sname = _load_font(FONTS_ITALIC, 19)
            f_count = _load_font(FONTS_BOLD, 16)
            y_cname = gy + gh - 68
            y_sname = gy + gh - 46
            y_count = gy + gh - 24
            
        cb = draw.textbbox((0, 0), cn, font=f_cname)
        draw.text((gx + (gw - (cb[2]-cb[0]))//2, y_cname), cn, font=f_cname, fill=(35, 25, 15))
        
        sb = draw.textbbox((0, 0), sn, font=f_sname)
        draw.text((gx + (gw - (sb[2]-sb[0]))//2, y_sname), sn, font=f_sname, fill=(90, 75, 60))
        
        cnt = f"{calls:,} Calls Heard Tonight" if calls != 1 else "1 Call Heard Tonight"
        cntb = draw.textbbox((0, 0), cnt, font=f_count)
        draw.text((gx + (gw - (cntb[2]-cntb[0]))//2, y_count), cnt, font=f_count, fill=(135, 105, 75))
        
    site_label = cfg.get("active_node", "Battybird").split("(")[0].strip() if cfg else "Battybird"
    foot_text = f"• Plate IV • {site_label} Acoustic Station • Dusk to Dawn • {date_span_str} •"
    f_ft = _load_font(FONTS_ITALIC, 15)
    fb = draw.textbbox((0, 0), foot_text, font=f_ft)
    fw = fb[2] - fb[0]
    draw.text(((PANEL_W - fw)//2, PANEL_H - 42), foot_text, font=f_ft, fill=(120, 100, 80))
    
    return canvas


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
    hours_mode = str(cfg.get("hours", "")).lower().strip()
    dusk_hour = int(cfg.get("dusk_hour", 17))
    if hours_mode in ('dusk-to-dawn', 'dusk_to_dawn', 'night', 'tonight', 'dusk'):
        current_night = active_night_date(datetime.now(), dusk_hour=dusk_hour)
        date_changed = (state.get("last_night") != current_night)
    else:
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
        style = str(cfg.get("style", cfg.get("art_style", "vintage"))).lower()
        base_url = str(cfg.get("base_url", "")).lower()
        is_bats = ("192.168.1.65" in base_url or "bat" in str(cfg.get("active_node", "")).lower() or cfg.get("hours") == "dusk-to-dawn")
        if style == "sketch" and not is_bats:
            img = fit_panel(img)
            img = mat_and_center(img, cfg["mat"], cfg["opening"])
        elif img.size != (PANEL_W, PANEL_H):
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
    current_night = active_night_date(datetime.now(), dusk_hour=dusk_hour); state["last_date"] = today_str; state["last_night"] = current_night; save_state(cfg["state"], sig if sig is not None else state.get("signature"), now, last_night=current_night)
    print("panel updated")


def load_config(path):
    cfg = dict(DEFAULTS)
    if path:
        with open(os.path.expanduser(path), "rb") as f:
            cfg.update(tomllib.load(f))
    active_node = str(cfg.get("active_node", "")).lower()
    if "auto" in active_node:
        day_str = str(cfg.get("auto_day_start", "08:30")).strip()
        night_str = str(cfg.get("auto_night_start", "17:00")).strip()
        try:
            dh, dm = map(int, day_str.split(":"))
            nh, nm = map(int, night_str.split(":"))
            day_mins = dh * 60 + dm
            night_mins = nh * 60 + nm
        except Exception:
            day_mins, night_mins = 8 * 60 + 30, 17 * 60
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        eff = "birds" if (day_mins <= now_mins < night_mins) else "bats"
        if eff == "birds":
            cfg["base_url"] = "http://192.168.1.71"
            cfg["hours"] = "today"
            cfg["shoot_subtitle"] = "Heard Today"
            cfg["shoot_title"] = cfg.get("shoot_title") or "Avian Visitors"
            b_style = cfg.get("bird_style") or cfg.get("style", "sketch")
            if str(b_style).lower() in ("gothic", "belfry"):
                b_style = "sketch"
            cfg["style"] = b_style
        else:
            cfg["base_url"] = "http://192.168.1.65"
            cfg["hours"] = "dusk-to-dawn"
            cfg["shoot_subtitle"] = "Heard Tonight"
            cfg["shoot_title"] = cfg.get("shoot_title") or "Gothic Visitors"
            b_style = cfg.get("bat_style") or cfg.get("style", "sketch")
            if str(b_style).lower() in ("gothic", "belfry"):
                b_style = "sketch"
            cfg["style"] = b_style
            cfg["dusk_hour"] = 17
    elif "bat" in active_node:
        cfg["shoot_title"] = cfg.get("shoot_title") or "Gothic Visitors"
        if "bat_style" in cfg:
            cfg["style"] = cfg["bat_style"]
    elif "bird" in active_node:
        cfg["shoot_title"] = cfg.get("shoot_title") or "Avian Visitors"
        if "bird_style" in cfg:
            cfg["style"] = cfg["bird_style"]

    # Normalize style: strip prefixes like 'birds:' or 'bats:' and map aliases
    if "style" in cfg:
        s_val = str(cfg["style"]).strip().lower()
        if ":" in s_val:
            s_val = s_val.split(":")[-1]
        if s_val in ("gothic", "belfry"):
            s_val = "sketch"
        cfg["style"] = s_val

    return cfg


def main():
    ap = argparse.ArgumentParser(description="Push the collage screenshot to the Inky panel.")
    ap.add_argument("--config")
    ap.add_argument("--base-url")
    ap.add_argument("--style", help="override rendering style (sketch, cartoon, vintage)")
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
    if args.style:
        s_arg = str(args.style).strip().lower()
        if ":" in s_arg:
            s_arg = s_arg.split(":")[-1]
        if s_arg in ("gothic", "belfry"):
            s_arg = "sketch"
        cfg["style"] = s_arg
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
