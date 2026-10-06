#!/usr/bin/env python3
"""
BirdFrame Control Daemon & Home Assistant Synchronizer
Listens to Home Assistant entities in real-time and provides HTTP REST API endpoints.
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

CONFIG_PATH = os.path.expanduser("~/.birdframe/config.toml")
TOKEN_PATH = os.path.expanduser("~/.birdframe/ha_token")
FRAME_DIR = "/home/birder/AvianVisitors/frame"
VENV_PYTHON = "/home/birder/AvianVisitors/frame/.venv/bin/python3"
HA_BASE_URL = "http://192.168.1.81:8123"

# Fallback default token (prefer storing in ~/.birdframe/ha_token or setting HA_TOKEN env var)
DEFAULT_TOKEN = os.environ.get("HA_TOKEN", "")

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
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    return {}

def trigger_refresh():
    cmd = [VENV_PYTHON, f"{FRAME_DIR}/display.py", "--config", CONFIG_PATH, "--force"]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

LAST_SELECT_STATE = None
LAST_TOGGLE_STATE = None

def update_style(new_style, notify_ha=True):
    global LAST_SELECT_STATE, LAST_TOGGLE_STATE
    new_style = new_style.lower().strip()
    if new_style not in ("cartoon", "sketch", "vintage"):
        return False, f"Invalid style: {new_style}"
    
    LAST_SELECT_STATE = new_style
    LAST_TOGGLE_STATE = ("on" if new_style == "cartoon" else "off")

    with open(CONFIG_PATH, "r") as f:
        lines = f.readlines()
    
    has_style = False
    new_lines = []
    for line in lines:
        if line.strip().startswith("style"):
            new_lines.append(f'style = "{new_style}"\n')
            has_style = True
        else:
            new_lines.append(line)
    
    if not has_style:
        new_lines.append(f'style = "{new_style}"\n')
    
    with open(CONFIG_PATH, "w") as f:
        f.writelines(new_lines)
    
    if notify_ha:
        update_ha_states(new_style)

    trigger_refresh()
    return True, new_style

def update_ha_states(style):
    token = get_ha_token()
    if not token:
        return
    style_cap = style.capitalize()
    # 1. Update input_select
    try:
        url_sel = f"{HA_BASE_URL}/api/states/input_select.birdframe_art_style"
        payload_sel = json.dumps({
            "state": style_cap,
            "attributes": {
                "icon": "mdi:palette-outline",
                "friendly_name": "BirdFrame Art Style",
                "options": ["Cartoon", "Sketch", "Vintage"]
            }
        }).encode("utf-8")
        req = urllib.request.Request(
            url_sel, data=payload_sel,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            pass
    except Exception as e:
        print(f"Error updating HA input_select: {e}")

    # 2. Update input_boolean toggle (on for cartoon, off for others)
    try:
        url_bool = f"{HA_BASE_URL}/api/states/input_boolean.birdframe_cartoon_mode"
        payload_bool = json.dumps({
            "state": "on" if style == "cartoon" else "off",
            "attributes": {
                "icon": "mdi:palette-outline",
                "friendly_name": "BirdFrame Cartoon Mode"
            }
        }).encode("utf-8")
        req = urllib.request.Request(
            url_bool, data=payload_bool,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            pass
    except Exception as e:
        print(f"Error updating HA input_boolean: {e}")

def ha_sync_loop():
    """Background polling loop that syncs Home Assistant select, toggle and button presses in real time."""
    global LAST_SELECT_STATE, LAST_TOGGLE_STATE
    time.sleep(2)
    last_button_state = None
    first_run = True

    while True:
        token = get_ha_token()
        if token:
            try:
                # 1. Check Select Dropdown State
                url_select = f"{HA_BASE_URL}/api/states/input_select.birdframe_art_style"
                req_s = urllib.request.Request(
                    url_select,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                try:
                    with urllib.request.urlopen(req_s, timeout=3) as resp:
                        s_data = json.loads(resp.read().decode())
                        cur_select = str(s_data.get("state", "")).lower()
                        if cur_select in ("cartoon", "sketch", "vintage"):
                            if first_run:
                                LAST_SELECT_STATE = cur_select
                                LAST_TOGGLE_STATE = ("on" if cur_select == "cartoon" else "off")
                                cfg = get_config()
                                local_style = cfg.get("style", "sketch").lower()
                                if local_style != cur_select:
                                    print(f"Initial sync: Setting style to {cur_select} from HA select")
                                    update_style(cur_select, notify_ha=False)
                            elif LAST_SELECT_STATE is not None and cur_select != LAST_SELECT_STATE:
                                print(f"HA Select changed from {LAST_SELECT_STATE} to {cur_select}!")
                                update_style(cur_select, notify_ha=True)
                            else:
                                LAST_SELECT_STATE = cur_select
                except Exception:
                    pass

                # 2. Check Legacy Toggle State (if select didn't fire change)
                url_toggle = f"{HA_BASE_URL}/api/states/input_boolean.birdframe_cartoon_mode"
                req_t = urllib.request.Request(
                    url_toggle,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                try:
                    with urllib.request.urlopen(req_t, timeout=3) as resp:
                        data = json.loads(resp.read().decode())
                        current_ha_state = data.get("state") # 'on' or 'off'
                        
                        if current_ha_state in ("on", "off"):
                            if first_run and LAST_SELECT_STATE is None:
                                LAST_TOGGLE_STATE = current_ha_state
                                cfg = get_config()
                                local_style = cfg.get("style", "sketch").lower()
                                expected_style = "cartoon" if current_ha_state == "on" else "sketch"
                                if local_style != expected_style:
                                    print(f"Initial sync: Setting style to {expected_style} from HA toggle {current_ha_state}")
                                    update_style(expected_style, notify_ha=False)
                            elif LAST_TOGGLE_STATE is not None and current_ha_state != LAST_TOGGLE_STATE:
                                print(f"HA Toggle changed from {LAST_TOGGLE_STATE} to {current_ha_state}!")
                                if current_ha_state == "on":
                                    update_style("cartoon", notify_ha=True)
                                else:
                                    cfg = get_config()
                                    if cfg.get("style", "").lower() == "cartoon":
                                        update_style("sketch", notify_ha=True)
                            else:
                                LAST_TOGGLE_STATE = current_ha_state
                except Exception:
                    pass

                # 3. Check Refresh Button State
                url_btn = f"{HA_BASE_URL}/api/states/input_button.birdframe_refresh_display"
                req_b = urllib.request.Request(
                    url_btn,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                try:
                    with urllib.request.urlopen(req_b, timeout=3) as resp:
                        btn_data = json.loads(resp.read().decode())
                        btn_state = btn_data.get("state") # timestamp of last press
                        
                        if first_run:
                            last_button_state = btn_state
                        elif last_button_state is not None and btn_state != last_button_state:
                            print(f"HA Refresh Button pressed! Triggering screen refresh...")
                            last_button_state = btn_state
                            trigger_refresh()
                        else:
                            last_button_state = btn_state
                except Exception:
                    pass

                first_run = False
            except Exception:
                pass

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
            font = cfg.get("font", "auto")
            is_cartoon = (style == "cartoon")
            return self.send_json({
                "style": style,
                "is_cartoon": is_cartoon,
                "font": font,
                "bird_names": cfg.get("bird_names", True),
                "hours": cfg.get("hours", "today")
            })

        elif url.path in ("/api/set", "/set"):
            style = params.get("style", [None])[0]
            if not style:
                return self.send_json({"error": "Missing style parameter"}, 400)
            ok, res = update_style(style, notify_ha=True)
            if ok:
                return self.send_json({"success": True, "style": res, "is_cartoon": (res == "cartoon")})
            return self.send_json({"error": res}, 400)

        elif url.path in ("/api/toggle", "/toggle"):
            cfg = get_config()
            current = cfg.get("style", "sketch").lower()
            cycle = {"cartoon": "sketch", "sketch": "vintage", "vintage": "cartoon"}
            next_style = cycle.get(current, "cartoon")
            ok, res = update_style(next_style, notify_ha=True)
            return self.send_json({"success": True, "style": res, "is_cartoon": (res == "cartoon")})

        elif url.path in ("/api/refresh", "/refresh"):
            trigger_refresh()
            return self.send_json({"success": True, "refreshed": True})

        elif url.path in ("/", "/health"):
            return self.send_json({"status": "ok", "service": "birdframe-api"})

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        return self.do_GET()

if __name__ == "__main__":
    # Start Home Assistant background sync thread
    sync_thread = threading.Thread(target=ha_sync_loop, daemon=True)
    sync_thread.start()

    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8088), RequestHandler)
    print("BirdFrame Control Daemon running on port 8088...")
    server.serve_forever()
