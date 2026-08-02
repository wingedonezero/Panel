"""Panel configuration — all via environment variables, all optional.

The goal: zero-config on a typical setup (disks, pools, CPU, network are
auto-discovered), with env overrides for labels and anything unusual.
"""
import os
import socket


def _bool(name, default):
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _parse_labels(raw):
    """PANEL_DISKS="SERIAL=label,SERIAL=label" -> {serial: label}"""
    out = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            serial, label = part.split("=", 1)
            out[serial.strip()] = label.strip()
    return out


def _parse_pools(raw):
    """PANEL_POOLS="name=/path;name2=/path2" -> [(name, path)]"""
    out = []
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            name, path = part.split("=", 1)
            out.append((name.strip(), path.strip()))
    return out


PORT = int(os.environ.get("PANEL_PORT", "8763"))
INTERVAL = float(os.environ.get("PANEL_INTERVAL", "2"))
TITLE = os.environ.get("PANEL_TITLE", "") or socket.gethostname()

# Pause all probing/sampling when no browser has polled recently
IDLE_PAUSE = _bool("PANEL_IDLE_PAUSE", True)
IDLE_WINDOW = float(os.environ.get("PANEL_IDLE_WINDOW", "15"))

# Disk display labels by serial (discovery finds the disks themselves)
DISK_LABELS = _parse_labels(os.environ.get("PANEL_DISKS", ""))
HIDE_DISKS = {s.strip() for s in os.environ.get("PANEL_HIDE_DISKS", "").split(",") if s.strip()}

# Pools: explicit list, else fuse.mergerfs mounts are auto-detected
POOLS = _parse_pools(os.environ.get("PANEL_POOLS", ""))

# LAN interfaces counted in the network panel (regex on interface name)
NET_LAN_REGEX = os.environ.get("PANEL_NET_LAN_REGEX", r"^(eth|en|bond)")

# Optional Broadcom/LSI storcli binary for HBA temperature (path inside container)
STORCLI = os.environ.get("PANEL_STORCLI", "")

# Slow-probe cadences (seconds)
SPIN_EVERY = float(os.environ.get("PANEL_SPIN_EVERY", "15"))
LSI_EVERY = float(os.environ.get("PANEL_LSI_EVERY", "60"))

# Where the host root is bind-mounted (for usage of paths outside the container)
HOSTROOT = os.environ.get("PANEL_HOSTROOT", "/hostroot")
