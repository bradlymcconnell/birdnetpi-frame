#!/usr/bin/env python3
"""
BirdFrame Control Daemon & Home Assistant Synchronizer
Listens to Home Assistant entities in real-time and provides HTTP REST API endpoints.
Supports multi-node switching between birdnetpi1 (Birds) and battybird (Bats) + Automatic Day/Night.
Supports station-specific art styles:
  - Birds: birds:sketch, birds:cartoon, birds:vintage
  - Bats: bats:cartoon, bats:vintage, bats:sketch
  - Combined Art Style: birds:*, bats:*
In Automatic mode, the frame automatically switches between bird and bat styles at scheduled times (08:30 and 17:00).
"""

import http.server
import json
import os
import subprocess
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from datetime import datetime

CONFIG_PATH = os.path.expanduser("~/.birdframe/config.toml")
TOKEN_PATH = os.path.expanduser("~/.birdframe/ha_token")
FRAME_DIR = "/home/birder/AvianVisitors/frame"
VENV_PYTHON = "/home/birder/AvianVisitors/frame/.venv/bin/python3"
HA_BASE_URL = "http://192.168.1.81:8123"

DEFAULT_TOKEN = os.environ.get("HA_TOKEN", "")

NODES = {
    "birds": {
        "label": "birdnetpi1 (Birds)",
        "base_url": "http://192.168.1.71",
        "shoot_subtitle": "Heard Today",
        "hours": "today",
    },
    "bats": {
        "label": "battybird (Bats)",
        "base_url": "http://192.168.1.65",
        "shoot_subtitle": "Heard Tonight",
        "hours": "dusk-to-dawn",
        "dusk_hour": 17,
    }
}

BIRD_STYLE_OPTIONS = ["birds:sketch", "birds:cartoon", "birds:vintage"]
BAT_STYLE_OPTIONS = ["bats:cartoon", "bats:vintage", "bats:sketch"]
ALL_STYLE_OPTIONS = [
    "birds:sketch", "birds:cartoon", "birds:vintage",
    "bats:cartoon", "bats:vintage", "bats:sketch"
]

STYLE_ALIASES = {
    "sketch": "sketch",
    "cartoon": "cartoon",
    "vintage": "vintage",
    "gothic": "sketch",
    "belfry": "sketch",
    "gothic belfry": "sketch",
    "gothic naturalist": "sketch",
}

LAST_NODE_SELECT = None
LAST_BIRD_STYLE_SELECT = None
LAST_BAT_STYLE_SELECT = None
LAST_ART_STYLE_SELECT = None
CURRENT_EFFECTIVE_NODE = None
CONFIG_LOCK = threading.Lock()

IGNORE_HA_ART_UNTIL = 0.0
IGNORE_HA_BIRD_UNTIL = 0.0
IGNORE_HA_BAT_UNTIL = 0.0
IGNORE_HA_NODE_UNTIL = 0.0


def get_ha_token():
    if os.path.exists(TOKEN_PATH):
        try:
            with open(TOKEN_PATH, "r") as f:
                t = f.read().strip()
                if t:
                    return t
        except Exception:
            pass
    return DEFAULT_TOKEN


def get_config():
    with CONFIG_LOCK:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "rb") as f:
                return tomllib.load(f)
        return {}


def trigger_refresh():
    cmd = [VENV_PYTHON, f"{FRAME_DIR}/display.py", "--config", CONFIG_PATH, "--force"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def update_config_kv(kv_dict):
    """Update or add multiple key-value pairs in config.toml while preserving format."""
    with CONFIG_LOCK:
        lines = []
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, "r") as f:
                lines = f.readlines()

        found_keys = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            matched_key = None
            for k in kv_dict:
                if stripped.startswith(k) and ("=" in stripped) and (stripped.split("=")[0].strip() == k):
                    matched_key = k
                    break
            if matched_key:
                val = kv_dict[matched_key]
                if isinstance(val, bool):
                    val_str = "true" if val else "false"
                elif isinstance(val, (int, float)):
                    val_str = str(val)
                else:
                    val_str = f'"{val}"'
                new_lines.append(f'{matched_key} = {val_str}\n')
                found_keys.add(matched_key)
            else:
                new_lines.append(line)

        for k, val in kv_dict.items():
            if k not in found_keys:
                if isinstance(val, bool):
                    val_str = "true" if val else "false"
                elif isinstance(val, (int, float)):
                    val_str = str(val)
                else:
                    val_str = f'"{val}"'
                new_lines.append(f'{k} = {val_str}\n')

        with open(CONFIG_PATH, "w") as f:
            f.writelines(new_lines)


def ha_call_service(domain, service, service_data):
    token = get_ha_token()
    if not token:
        return False
    try:
        url = f"{HA_BASE_URL}/api/services/{domain}/{service}"
        payload = json.dumps(service_data).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Error calling {domain}.{service}: {e}")
        return False


def ha_get_entity_state(entity_id):
    token = get_ha_token()
    if not token:
        return None
    try:
        url = f"{HA_BASE_URL}/api/states/{entity_id}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return data.get("state")
    except Exception:
        return None


def ha_ensure_select_options():
    """Ensure all input_select entities in Home Assistant have correct options configured."""
    ha_call_service("input_select", "set_options", {
        "entity_id": "input_select.birdframe_bird_style",
        "options": BIRD_STYLE_OPTIONS
    })
    ha_call_service("input_select", "set_options", {
        "entity_id": "input_select.birdframe_bat_style",
        "options": BAT_STYLE_OPTIONS
    })
    ha_call_service("input_select", "set_options", {
        "entity_id": "input_select.birdframe_art_style",
        "options": ALL_STYLE_OPTIONS
    })
    ha_call_service("input_select", "set_options", {
        "entity_id": "input_select.birdframe_active_node",
        "options": ["Automatic (Day/Night)", "birdnetpi1 (Birds)", "battybird (Bats)"]
    })


def parse_style(input_str, default_node=None):
    """
    Parses style string like 'birds:sketch', 'bats:cartoon', 'cartoon', 'vintage'.
    Returns (node, base_style, full_prefixed_style)
    """
    s = str(input_str).strip().lower()
    node = None
    if s.startswith("birds:") or s.startswith("bird:"):
        node = "birds"
        s = s.split(":", 1)[1].strip()
    elif s.startswith("bats:") or s.startswith("bat:"):
        node = "bats"
        s = s.split(":", 1)[1].strip()
    else:
        node = default_node or "birds"

    base = STYLE_ALIASES.get(s, None)
    if not base:
        for candidate in ("sketch", "cartoon", "vintage"):
            if candidate in s:
                base = candidate
                break
        if not base:
            base = "sketch"

    prefix = node if node in ("birds", "bats") else "birds"
    full_prefixed = f"{prefix}:{base}"
    return prefix, base, full_prefixed


def get_auto_schedule(cfg=None):
    if cfg is None:
        cfg = get_config()
    day_str = str(cfg.get("auto_day_start", "08:30")).strip()
    night_str = str(cfg.get("auto_night_start", "18:50")).strip()
    try:
        dh, dm = map(int, day_str.split(":"))
        nh, nm = map(int, night_str.split(":"))
        return dh * 60 + dm, nh * 60 + nm
    except Exception:
        return 8 * 60 + 30, 18 * 60 + 50


def resolve_node(mode_str):
    m = mode_str.lower().strip()
    if "bat" in m:
        return "bats"
    elif "bird" in m:
        return "birds"
    else:  # Automatic (Day/Night) or auto
        # Check Home Assistant sun.sun if available
        sun_state = ha_get_entity_state("sun.sun")
        now = datetime.now()
        now_mins = now.hour * 60 + now.minute
        day_mins, _ = get_auto_schedule()
        if sun_state == "below_horizon":
            return "bats"
        elif sun_state == "above_horizon":
            # Morning coffee window: show bats until morning cutoff (default 08:30)
            return "birds" if now_mins >= day_mins else "bats"

        # Fallback to local time schedule
        day_mins, night_mins = get_auto_schedule()
        if day_mins <= now_mins < night_mins:
            return "birds"
        else:
            return "bats"


def set_bird_style(style_input, notify_ha_self=True, refresh_if_active=True):
    global LAST_BIRD_STYLE_SELECT, LAST_ART_STYLE_SELECT, IGNORE_HA_ART_UNTIL, IGNORE_HA_BIRD_UNTIL
    _, base_style, full_prefixed = parse_style(style_input, default_node="birds")
    LAST_BIRD_STYLE_SELECT = full_prefixed

    update_dict = {"bird_style": base_style}
    is_active = (CURRENT_EFFECTIVE_NODE == "birds")
    if is_active:
        update_dict["style"] = base_style
        LAST_ART_STYLE_SELECT = full_prefixed

    update_config_kv(update_dict)
    print(f"Bird style set to: {full_prefixed} (base: {base_style}, active={is_active})")

    if notify_ha_self:
        IGNORE_HA_BIRD_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_bird_style",
            "option": full_prefixed
        })
    if is_active:
        IGNORE_HA_ART_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_art_style",
            "option": full_prefixed
        })

    if is_active and refresh_if_active:
        trigger_refresh()
    return True, full_prefixed


def set_bat_style(style_input, notify_ha_self=True, refresh_if_active=True):
    global LAST_BAT_STYLE_SELECT, LAST_ART_STYLE_SELECT, IGNORE_HA_ART_UNTIL, IGNORE_HA_BAT_UNTIL
    _, base_style, full_prefixed = parse_style(style_input, default_node="bats")
    LAST_BAT_STYLE_SELECT = full_prefixed

    update_dict = {"bat_style": base_style}
    is_active = (CURRENT_EFFECTIVE_NODE == "bats")
    if is_active:
        update_dict["style"] = base_style
        LAST_ART_STYLE_SELECT = full_prefixed

    update_config_kv(update_dict)
    print(f"Bat style set to: {full_prefixed} (base: {base_style}, active={is_active})")

    if notify_ha_self:
        IGNORE_HA_BAT_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_bat_style",
            "option": full_prefixed
        })
    if is_active:
        IGNORE_HA_ART_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_art_style",
            "option": full_prefixed
        })

    if is_active and refresh_if_active:
        trigger_refresh()
    return True, full_prefixed


def set_art_style(style_input, notify_ha_self=True):
    """Dispatches style change from the combined input_select.birdframe_art_style."""
    global LAST_ART_STYLE_SELECT, IGNORE_HA_ART_UNTIL, IGNORE_HA_BIRD_UNTIL, IGNORE_HA_BAT_UNTIL
    node, base_style, full_prefixed = parse_style(style_input, default_node=CURRENT_EFFECTIVE_NODE)
    LAST_ART_STYLE_SELECT = full_prefixed

    if notify_ha_self:
        IGNORE_HA_ART_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_art_style",
            "option": full_prefixed
        })

    if node == "birds":
        IGNORE_HA_BIRD_UNTIL = time.time() + 3.0
        return set_bird_style(full_prefixed, notify_ha_self=True, refresh_if_active=True)
    else:
        IGNORE_HA_BAT_UNTIL = time.time() + 3.0
        return set_bat_style(full_prefixed, notify_ha_self=True, refresh_if_active=True)


def update_node(mode_str, notify_ha=True):
    global LAST_NODE_SELECT, CURRENT_EFFECTIVE_NODE, LAST_ART_STYLE_SELECT, IGNORE_HA_NODE_UNTIL, IGNORE_HA_ART_UNTIL
    eff_node = resolve_node(mode_str)
    node_cfg = NODES[eff_node]

    LAST_NODE_SELECT = mode_str
    CURRENT_EFFECTIVE_NODE = eff_node

    cfg = get_config()
    bird_style = cfg.get("bird_style", "sketch").lower()
    bat_style = cfg.get("bat_style", "cartoon").lower()

    if eff_node == "birds":
        active_style = bird_style
        active_prefixed = f"birds:{bird_style}"
    else:
        active_style = bat_style
        active_prefixed = f"bats:{bat_style}"

    LAST_ART_STYLE_SELECT = active_prefixed

    update_dict = {
        "base_url": node_cfg["base_url"],
        "shoot_subtitle": node_cfg["shoot_subtitle"],
        "hours": node_cfg.get("hours", "dusk-to-dawn" if eff_node == "bats" else "today"),
        "active_node": mode_str,
        "style": active_style,
        "bird_style": bird_style,
        "bat_style": bat_style
    }
    if "dusk_hour" in node_cfg:
        update_dict["dusk_hour"] = node_cfg["dusk_hour"]

    update_config_kv(update_dict)

    if notify_ha:
        IGNORE_HA_NODE_UNTIL = time.time() + 3.0
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_active_node",
            "option": mode_str if mode_str in ("Automatic (Day/Night)", "birdnetpi1 (Birds)", "battybird (Bats)") else "Automatic (Day/Night)"
        })

    IGNORE_HA_ART_UNTIL = time.time() + 3.0
    ha_call_service("input_select", "select_option", {
        "entity_id": "input_select.birdframe_art_style",
        "option": active_prefixed
    })

    print(f"Switched active node to '{mode_str}' (effective: {eff_node}, style: {active_prefixed})")
    trigger_refresh()
    return True, eff_node


def ha_sync_loop():
    """Background polling loop that syncs Home Assistant select dropdowns and button presses in real time."""
    global LAST_NODE_SELECT, LAST_BIRD_STYLE_SELECT, LAST_BAT_STYLE_SELECT, LAST_ART_STYLE_SELECT, CURRENT_EFFECTIVE_NODE
    global IGNORE_HA_NODE_UNTIL, IGNORE_HA_BIRD_UNTIL, IGNORE_HA_BAT_UNTIL, IGNORE_HA_ART_UNTIL
    time.sleep(2)
    last_button_state = None

    # Step 1: Ensure options on HA input_select helpers
    ha_ensure_select_options()

    # Step 2: Establish baseline from config and HA
    cfg = get_config()
    bird_style = cfg.get("bird_style", "sketch").lower()
    bat_style = cfg.get("bat_style", "cartoon").lower()
    active_node = cfg.get("active_node", "Automatic (Day/Night)")

    LAST_NODE_SELECT = ha_get_entity_state("input_select.birdframe_active_node") or active_node
    CURRENT_EFFECTIVE_NODE = resolve_node(LAST_NODE_SELECT)

    # Sync bird and bat styles with HA
    ha_bird = ha_get_entity_state("input_select.birdframe_bird_style")
    if ha_bird and ha_bird in BIRD_STYLE_OPTIONS:
        bird_style = ha_bird.split(":")[-1]
        LAST_BIRD_STYLE_SELECT = ha_bird
    else:
        LAST_BIRD_STYLE_SELECT = f"birds:{bird_style}"
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_bird_style",
            "option": LAST_BIRD_STYLE_SELECT
        })

    ha_bat = ha_get_entity_state("input_select.birdframe_bat_style")
    if ha_bat and ha_bat in BAT_STYLE_OPTIONS:
        bat_style = ha_bat.split(":")[-1]
        LAST_BAT_STYLE_SELECT = ha_bat
    else:
        LAST_BAT_STYLE_SELECT = f"bats:{bat_style}"
        ha_call_service("input_select", "select_option", {
            "entity_id": "input_select.birdframe_bat_style",
            "option": LAST_BAT_STYLE_SELECT
        })

    active_style = bird_style if CURRENT_EFFECTIVE_NODE == "birds" else bat_style
    active_prefixed = f"{CURRENT_EFFECTIVE_NODE}:{active_style}"
    LAST_ART_STYLE_SELECT = active_prefixed

    ha_call_service("input_select", "select_option", {
        "entity_id": "input_select.birdframe_art_style",
        "option": active_prefixed
    })

    print(f"HA Sync initialized: active_node='{LAST_NODE_SELECT}', eff='{CURRENT_EFFECTIVE_NODE}', bird='{LAST_BIRD_STYLE_SELECT}', bat='{LAST_BAT_STYLE_SELECT}', active_style='{LAST_ART_STYLE_SELECT}'")

    last_sun_state = ha_get_entity_state("sun.sun")
    last_day_minute = datetime.now().hour * 60 + datetime.now().minute

    while True:
        token = get_ha_token()
        now_ts = time.time()
        if token:
            try:
                cur_sun_state = ha_get_entity_state("sun.sun")
                now_dt = datetime.now()
                now_m = now_dt.hour * 60 + now_dt.minute
                day_m, _ = get_auto_schedule()

                # Detect sunset transition (above_horizon -> below_horizon)
                sunset_triggered = (last_sun_state == "above_horizon" and cur_sun_state == "below_horizon")
                
                # Detect morning 08:30 transition
                morning_triggered = False
                if last_day_minute is not None:
                    if last_day_minute < day_m <= now_m:
                        morning_triggered = True
                last_day_minute = now_m
                last_sun_state = cur_sun_state

                # 1. Check Active Node Select Dropdown State
                if now_ts >= IGNORE_HA_NODE_UNTIL:
                    cur_node_select = ha_get_entity_state("input_select.birdframe_active_node")
                    if cur_node_select:
                        # Auto-reset manual overrides at schedule transitions
                        if (sunset_triggered or morning_triggered) and cur_node_select != "Automatic (Day/Night)":
                            print(f"Schedule transition event (sunset={sunset_triggered}, morning={morning_triggered}): resetting manual node '{cur_node_select}' to 'Automatic (Day/Night)'")
                            cur_node_select = "Automatic (Day/Night)"
                            ha_call_service("input_select", "select_option", {
                                "entity_id": "input_select.birdframe_active_node",
                                "option": "Automatic (Day/Night)"
                            })

                        eff = resolve_node(cur_node_select)
                        mode_changed = (cur_node_select != LAST_NODE_SELECT)
                        effective_changed = (eff != CURRENT_EFFECTIVE_NODE)

                        if mode_changed or effective_changed:
                            print(f"Node transition: Mode='{cur_node_select}' (mode_changed={mode_changed}), Effective='{eff}' (effective_changed={effective_changed})")
                            # If triggered from HA node change, don't echo node back to HA
                            update_node(cur_node_select, notify_ha=False)

                # 2. Check Bird Style Select Dropdown State
                if now_ts >= IGNORE_HA_BIRD_UNTIL:
                    cur_bird_select = ha_get_entity_state("input_select.birdframe_bird_style")
                    if cur_bird_select and cur_bird_select in BIRD_STYLE_OPTIONS and cur_bird_select != LAST_BIRD_STYLE_SELECT:
                        print(f"HA Bird Style changed: {LAST_BIRD_STYLE_SELECT} -> {cur_bird_select}")
                        set_bird_style(cur_bird_select, notify_ha_self=False, refresh_if_active=True)

                # 3. Check Bat Style Select Dropdown State
                if now_ts >= IGNORE_HA_BAT_UNTIL:
                    cur_bat_select = ha_get_entity_state("input_select.birdframe_bat_style")
                    if cur_bat_select and cur_bat_select in BAT_STYLE_OPTIONS and cur_bat_select != LAST_BAT_STYLE_SELECT:
                        print(f"HA Bat Style changed: {LAST_BAT_STYLE_SELECT} -> {cur_bat_select}")
                        set_bat_style(cur_bat_select, notify_ha_self=False, refresh_if_active=True)

                # 4. Check Combined Art Style Dropdown State
                if now_ts >= IGNORE_HA_ART_UNTIL:
                    cur_art_select = ha_get_entity_state("input_select.birdframe_art_style")
                    if cur_art_select and cur_art_select in ALL_STYLE_OPTIONS and cur_art_select != LAST_ART_STYLE_SELECT:
                        print(f"HA Art Style changed: {LAST_ART_STYLE_SELECT} -> {cur_art_select}")
                        set_art_style(cur_art_select, notify_ha_self=False)

                # 5. Check Refresh Button State
                btn_state = ha_get_entity_state("input_button.birdframe_refresh_display")
                if btn_state:
                    if last_button_state is not None and btn_state != last_button_state:
                        print("HA Refresh Button pressed! Triggering screen refresh...")
                        trigger_refresh()
                    last_button_state = btn_state

            except Exception as e:
                print(f"Error in HA sync loop: {e}")

        time.sleep(1.2)


class RequestHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(url.query)

        if url.path in ("/api/status", "/status"):
            cfg = get_config()
            style = cfg.get("style", "sketch")
            bird_style = cfg.get("bird_style", "sketch")
            bat_style = cfg.get("bat_style", "cartoon")
            font = cfg.get("font", "auto")
            base_url = cfg.get("base_url", "http://192.168.1.71")
            eff = CURRENT_EFFECTIVE_NODE or ("bats" if "192.168.1.65" in base_url else "birds")
            return self.send_json({
                "active_node": LAST_NODE_SELECT or cfg.get("active_node", "Automatic (Day/Night)"),
                "effective_node": eff,
                "style": style,
                "bird_style": bird_style,
                "bat_style": bat_style,
                "art_style": f"{eff}:{style}",
                "bird_style_options": BIRD_STYLE_OPTIONS,
                "bat_style_options": BAT_STYLE_OPTIONS,
                "art_style_options": ALL_STYLE_OPTIONS,
                "font": font,
                "base_url": base_url,
                "hours": cfg.get("hours", "today"),
                "auto_day_start": cfg.get("auto_day_start", "08:30"),
                "auto_night_start": cfg.get("auto_night_start", "17:00")
            })

        elif url.path in ("/api/set", "/set"):
            # Supports ?bird_style=..., ?bat_style=..., or ?style=...
            bird_param = params.get("bird_style", [None])[0]
            bat_param = params.get("bat_style", [None])[0]
            style_param = params.get("style", [None])[0]

            results = {}
            if bird_param:
                ok, res = set_bird_style(bird_param, notify_ha=True, refresh_if_active=True)
                results["bird_style"] = res
            if bat_param:
                ok, res = set_bat_style(bat_param, notify_ha=True, refresh_if_active=True)
                results["bat_style"] = res
            if style_param:
                ok, res = set_art_style(style_param, notify_ha=True)
                results["art_style"] = res

            if results:
                return self.send_json({"success": True, **results})
            return self.send_json({"error": "Missing bird_style, bat_style, or style parameter"}, 400)

        elif url.path in ("/api/node", "/node"):
            node = params.get("node", [None])[0]
            if not node:
                return self.send_json({"error": "Missing node parameter (birds, bats, or auto)"}, 400)
            ok, res = update_node(node, notify_ha=True)
            if ok:
                return self.send_json({"success": True, "effective_node": res, "active_node": LAST_NODE_SELECT})
            return self.send_json({"error": res}, 400)

        elif url.path in ("/api/toggle", "/toggle"):
            cfg = get_config()
            eff = CURRENT_EFFECTIVE_NODE or "birds"
            cycle = {"cartoon": "sketch", "sketch": "vintage", "vintage": "cartoon"}
            if eff == "birds":
                cur = cfg.get("bird_style", "sketch").lower()
                next_style = cycle.get(cur, "sketch")
                ok, res = set_bird_style(next_style, notify_ha=True, refresh_if_active=True)
            else:
                cur = cfg.get("bat_style", "cartoon").lower()
                next_style = cycle.get(cur, "cartoon")
                ok, res = set_bat_style(next_style, notify_ha=True, refresh_if_active=True)
            return self.send_json({"success": True, "effective_node": eff, "style": res})

        elif url.path in ("/api/refresh", "/refresh"):
            trigger_refresh()
            return self.send_json({"success": True, "refreshed": True})

        elif url.path in ("/", "/health"):
            return self.send_json({"status": "ok", "service": "birdframe-api"})

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        return self.do_GET()


if __name__ == "__main__":
    sync_thread = threading.Thread(target=ha_sync_loop, daemon=True)
    sync_thread.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8088), RequestHandler)
    print("BirdFrame Control Daemon running on port 8088...")
    server.serve_forever()
