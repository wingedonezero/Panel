"""Stat collectors. Everything reads standard Linux interfaces:
/proc, /sys/class/hwmon, /dev/disk/by-id, the docker socket, nvidia-smi,
and (optionally) storcli. All collectors degrade gracefully — a missing
tool or sensor just means that field is absent.
"""
import json
import os
import re
import socket
import subprocess

import config


def sh(cmd, timeout=10):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return ""


def host_mounts():
    """Device -> mountpoint map from the host's mount table.
    With pid: host we can read the host's own table via /proc/1/mounts."""
    mounts = {}
    for path in ("/proc/1/mounts", "/proc/mounts"):
        try:
            for line in open(path):
                dev, mnt, fstype = line.split()[:3]
                mounts.setdefault(dev, (mnt, fstype))
            break
        except Exception:
            continue
    return mounts


def resolve_usage_path(mnt):
    """A host mountpoint may not exist in the container namespace directly;
    fall back to the host-root bind mount."""
    if os.path.isdir(mnt) and os.path.ismount(mnt):
        return mnt
    alt = config.HOSTROOT + ("" if mnt == "/" else mnt)
    if os.path.isdir(alt):
        return alt
    return mnt if os.path.isdir(mnt) else None


def usage(path):
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        return total, total - st.f_bfree * st.f_frsize
    except Exception:
        return 0, 0


def discover_disks():
    """Find physical disks: serial, device node, filesystem mountpoint (if any).
    Returns [{serial, dev (sda), node (/dev/sda), label, mount}]."""
    by_id = {}
    base = "/dev/disk/by-id"
    try:
        for n in os.listdir(base):
            if n.startswith(("ata-", "nvme-", "scsi-", "wwn-")) and "-part" not in n:
                if n.startswith("wwn-"):
                    continue  # duplicate alias; prefer model_serial names
                real = os.path.realpath(os.path.join(base, n))
                serial = n.rsplit("_", 1)[-1]
                by_id.setdefault(real, serial)
    except Exception:
        pass

    mounts = host_mounts()
    disks = []
    for node, serial in sorted(by_id.items()):
        if serial in config.HIDE_DISKS:
            continue
        dev = os.path.basename(node)
        mount = None
        for mdev, (mnt, _fs) in mounts.items():
            if os.path.basename(mdev).startswith(dev):
                mount = mnt
                # prefer a data mount over ESP-style mounts
                if not mnt.startswith(("/boot", "/efi")):
                    break
        try:
            model = open(f"/sys/block/{dev}/device/model").read().strip()
        except Exception:
            model = ""
        try:
            size = int(open(f"/sys/block/{dev}/size").read()) * 512
        except Exception:
            size = 0
        auto = model or serial
        if size:
            auto += f" · {size / 1e12:.0f}T" if size >= 1e12 else f" · {size / 1e9:.0f}G"
        disks.append({
            "serial": serial,
            "dev": dev,
            "node": node,
            "model": model,
            "size": size,
            "auto": auto,
            "label": config.DISK_LABELS.get(serial, auto),
            "custom": serial in config.DISK_LABELS,
            "mount": mount,
        })
    return disks


def discover_pools():
    if config.POOLS:
        return list(config.POOLS)
    pools = []
    for dev, (mnt, fstype) in host_mounts().items():
        if fstype == "fuse.mergerfs":
            pools.append((os.path.basename(mnt) or "pool", mnt))
    return pools


def read_diskstats():
    out = {}
    for line in open("/proc/diskstats"):
        f = line.split()
        if re.fullmatch(r"sd[a-z]+|nvme\d+n\d+", f[2]):
            out[f[2]] = (int(f[5]) * 512, int(f[9]) * 512)  # bytes read, written
    return out


_lan_re_cache = {}


def _lan_re():
    pat = config.NET_LAN_REGEX
    if pat not in _lan_re_cache:
        _lan_re_cache[pat] = re.compile(pat)
    return _lan_re_cache[pat]


def read_netdev():
    rx = tx = ts_rx = ts_tx = 0
    for line in open("/proc/net/dev"):
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        f = rest.split()
        if _lan_re().match(name):
            rx += int(f[0]); tx += int(f[8])
        elif name.startswith("tailscale"):
            ts_rx += int(f[0]); ts_tx += int(f[8])
    return rx, tx, ts_rx, ts_tx


def read_cpu():
    f = open("/proc/stat").readline().split()[1:]
    v = list(map(int, f))
    return sum(v), v[3] + v[4]  # total, idle+iowait


def read_mem():
    mi = {}
    for line in open("/proc/meminfo"):
        k, val = line.split(":", 1)
        mi[k] = int(val.split()[0]) * 1024
    return mi["MemTotal"], mi["MemTotal"] - mi.get("MemAvailable", 0)


RAPL_CANDIDATES = [
    "/sys/class/powercap/intel-rapl:0/energy_uj",   # covers Intel and modern AMD
]


def rapl_energy():
    for path in RAPL_CANDIDATES:
        try:
            return int(open(path).read())
        except Exception:
            continue
    return None


def read_hwmon():
    """All hwmon temps + fans, filtered for obviously-bogus values."""
    temps, fans = [], []
    root = "/sys/class/hwmon"
    if not os.path.isdir(root):
        return temps, fans
    for h in sorted(os.listdir(root)):
        base = os.path.join(root, h)
        try:
            chip = open(os.path.join(base, "name")).read().strip()
        except Exception:
            continue
        if chip in ("drivetemp",):
            continue  # per-disk temps handled via smartctl (standby-safe)
        for f in sorted(os.listdir(base)):
            try:
                if f.startswith("temp") and f.endswith("_input"):
                    v = int(open(os.path.join(base, f)).read()) / 1000
                    if v <= 1 or v > 120:
                        continue
                    try:
                        label = open(os.path.join(base, f[:-6] + "_label")).read().strip()
                    except Exception:
                        label = f[:-6]
                    temps.append({"chip": chip, "label": label, "temp": round(v)})
                elif f.startswith("fan") and f.endswith("_input"):
                    rpm = int(open(os.path.join(base, f)).read())
                    if rpm > 0:
                        fans.append({"label": f[:-6], "rpm": rpm})
            except Exception:
                pass
    return temps, fans


def cpu_temp(temps):
    """Best-effort 'the CPU temp' from the hwmon sweep."""
    prefs = [("k10temp", "Tctl"), ("zenpower", "Tdie"), ("coretemp", "Package id 0")]
    for chip, label in prefs:
        for t in temps:
            if t["chip"] == chip and t["label"] == label:
                return t["temp"]
    core = [t["temp"] for t in temps if t["chip"] in ("k10temp", "coretemp", "zenpower")]
    return max(core) if core else None


def spin_state(node):
    out = sh(["hdparm", "-C", node], timeout=6)
    if "standby" in out:
        return "standby"
    if "active" in out:
        return "active"
    return "unknown"


def disk_temp(node):
    """smartctl -n standby: never wakes a sleeping drive."""
    out = sh(["smartctl", "-n", "standby", "-A", node], timeout=10)
    for line in out.splitlines():
        f = line.split()
        if len(f) > 9 and f[0] in ("194", "190"):
            try:
                return int(f[9])
            except ValueError:
                pass
    m = re.search(r"Current Temperature:\s+(\d+)", out)
    return int(m.group(1)) if m else None


def is_rotational(dev):
    try:
        return open(f"/sys/block/{dev}/queue/rotational").read().strip() == "1"
    except Exception:
        return False


def gpu_stats():
    out = sh(["nvidia-smi",
              "--query-gpu=name,utilization.gpu,memory.used,memory.total,"
              "temperature.gpu,power.draw,encoder.stats.sessionCount",
              "--format=csv,noheader,nounits"])
    try:
        name, u, mu, mt, t, p, enc = [x.strip() for x in out.strip().split(",")]
        return {"name": name, "util": int(u), "vram_used": int(mu), "vram_total": int(mt),
                "temp": int(t), "power": float(p), "enc_sessions": int(enc)}
    except Exception:
        return None


def gpu_procs(containers):
    """GPU-using processes, attributed to containers via /proc/<pid>/cgroup
    (works because we run with pid: host)."""
    out = sh(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
              "--format=csv,noheader,nounits"])
    procs = []
    by_id = {c["id"]: c["name"] for c in containers if c.get("id")}
    for line in out.strip().splitlines():
        try:
            pid, pname, mem = [x.strip() for x in line.split(",")]
            owner = None
            try:
                cg = open(f"/proc/{pid}/cgroup").read()
                m = re.search(r"docker[/-]([0-9a-f]{12})", cg)
                if m:
                    owner = by_id.get(m.group(1), m.group(1))
            except Exception:
                pass
            procs.append({"pid": int(pid), "name": os.path.basename(pname),
                          "container": owner, "mem": int(mem)})
        except Exception:
            continue
    return procs


def lsi_temp():
    if not config.STORCLI or not os.path.isfile(config.STORCLI):
        return None
    out = sh([config.STORCLI, "/c0", "show", "temperature"], timeout=15)
    m = re.search(r"ROC temperature\(Degree Celsius\)\s+(\d+)", out)
    return int(m.group(1)) if m else None


def docker_ps():
    try:
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(3)
        s.connect("/var/run/docker.sock")
        s.sendall(b"GET /containers/json?all=1 HTTP/1.0\r\nHost: docker\r\n\r\n")
        buf = b""
        while True:
            d = s.recv(65536)
            if not d:
                break
            buf += d
        s.close()
        data = json.loads(buf.split(b"\r\n\r\n", 1)[1])
        return sorted(
            [{"name": c["Names"][0].lstrip("/"), "state": c["State"],
              "id": c["Id"][:12]} for c in data],
            key=lambda x: (x["state"] != "running", x["name"]))
    except Exception:
        return []
