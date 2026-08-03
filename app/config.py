"""Panel configuration.

Two layers, merged at load():
  1. Environment variables  — deploy-time defaults (all optional)
  2. /config/settings.json  — the settings page; wins over env when present

Everything is readable as module attributes (config.INTERVAL etc.) and is
refreshed in place by load(), so the sampler picks up changes on its next
loop without a restart.
"""
import glob
import json
import os
import socket
import threading

CONFIG_DIR = os.environ.get("PANEL_CONFIG_DIR", "/config")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
_write_lock = threading.Lock()


def _bool(v, default):
    if isinstance(v, bool):
        return v
    v = str(v or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _parse_labels(raw):
    out = {}
    for part in raw.split(","):
        if "=" in part:
            serial, label = part.split("=", 1)
            out[serial.strip()] = label.strip()
    return out


def _parse_pools(raw):
    out = []
    for part in raw.split(";"):
        if "=" in part:
            name, path = part.split("=", 1)
            out.append((name.strip(), path.strip()))
    return out


def _settings_from_file():
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def load():
    """(Re)compute effective config: env defaults overlaid with settings.json."""
    global PORT, INTERVAL, TITLE, IDLE_PAUSE, IDLE_WINDOW, DISK_LABELS, \
        HIDE_DISKS, POOLS, NET_LAN_REGEX, STORCLI, SPIN_EVERY, LSI_EVERY, \
        HOSTROOT, SHOW_GRAPHS, SPIN_AFTER

    env = os.environ.get
    PORT = int(env("PANEL_PORT", "8763"))          # env-only (needs restart anyway)
    HOSTROOT = env("PANEL_HOSTROOT", "/hostroot")  # env-only
    POOLS = _parse_pools(env("PANEL_POOLS", ""))   # env override; else auto-detect

    INTERVAL = float(env("PANEL_INTERVAL", "2"))
    TITLE = env("PANEL_TITLE", "") or socket.gethostname()
    IDLE_PAUSE = _bool(env("PANEL_IDLE_PAUSE"), True)
    IDLE_WINDOW = float(env("PANEL_IDLE_WINDOW", "15"))
    SHOW_GRAPHS = _bool(env("PANEL_GRAPHS"), True)
    DISK_LABELS = _parse_labels(env("PANEL_DISKS", ""))
    HIDE_DISKS = {s.strip() for s in env("PANEL_HIDE_DISKS", "").split(",") if s.strip()}
    NET_LAN_REGEX = env("PANEL_NET_LAN_REGEX", r"^(eth|en|bond)")
    SPIN_EVERY = float(env("PANEL_SPIN_EVERY", "15"))
    SPIN_AFTER = float(env("PANEL_SPIN_AFTER", "1800"))
    LSI_EVERY = float(env("PANEL_LSI_EVERY", "60"))

    STORCLI = env("PANEL_STORCLI", "")
    if not STORCLI:  # auto-detect a mounted-in binary
        hits = sorted(glob.glob(os.path.join(CONFIG_DIR, "bin", "storcli64*")))
        STORCLI = hits[0] if hits else ""

    s = _settings_from_file()
    if s.get("title"):
        TITLE = s["title"]
    if "interval" in s:
        INTERVAL = max(0.5, float(s["interval"]))
    if "idle_pause" in s:
        IDLE_PAUSE = _bool(s["idle_pause"], IDLE_PAUSE)
    if "idle_window" in s:
        IDLE_WINDOW = max(5.0, float(s["idle_window"]))
    if "show_graphs" in s:
        SHOW_GRAPHS = _bool(s["show_graphs"], SHOW_GRAPHS)
    if "spin_after" in s:
        SPIN_AFTER = max(60.0, float(s["spin_after"]))
    if "disk_labels" in s:
        DISK_LABELS = {**DISK_LABELS, **s["disk_labels"]}
    if "hide_disks" in s:
        HIDE_DISKS = set(s["hide_disks"])
    if s.get("storcli"):
        STORCLI = s["storcli"]


def save(new):
    """Merge into settings.json and re-apply. Only known keys are stored."""
    allowed = {"title", "interval", "idle_pause", "idle_window",
               "disk_labels", "hide_disks", "storcli", "show_graphs",
               "spin_after"}
    with _write_lock:
        s = _settings_from_file()
        for k, v in new.items():
            if k in allowed:
                s[k] = v
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    load()


def current():
    """Effective settings for the settings page."""
    return {
        "title": TITLE, "interval": INTERVAL,
        "idle_pause": IDLE_PAUSE, "idle_window": IDLE_WINDOW,
        "show_graphs": SHOW_GRAPHS, "spin_after": SPIN_AFTER,
        "disk_labels": dict(DISK_LABELS), "hide_disks": sorted(HIDE_DISKS),
        "storcli": STORCLI,
        "writable": os.access(CONFIG_DIR, os.W_OK) if os.path.isdir(CONFIG_DIR)
                    else os.access(os.path.dirname(CONFIG_DIR) or "/", os.W_OK),
    }


load()
