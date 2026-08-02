#!/usr/bin/env python3
"""Panel — a lightweight, Unraid-style live status page for a home server.

No history, no database, no agents: a background sampler reads /proc, hwmon,
and friends every couple of seconds while someone is watching, and pauses
entirely when nobody is (PANEL_IDLE_PAUSE).
"""
import os
import threading
import time

from flask import Flask, jsonify, request, send_file

import collectors as C
import config

app = Flask(__name__)
STATS = {}
_lock = threading.Lock()
_last_hit = [0.0]
_rediscover = threading.Event()


def sampler():
    disks = C.discover_disks()
    pools = C.discover_pools()
    prev_ds = C.read_diskstats()
    prev_net = C.read_netdev()
    prev_cpu = C.read_cpu()
    prev_energy = C.rapl_energy()
    prev_t = time.time()
    spin, temps = {}, {}
    last_slow = last_lsi = last_rediscover = 0.0
    lsi = None

    while True:
        time.sleep(config.INTERVAL)
        now = time.time()

        if config.IDLE_PAUSE and now - _last_hit[0] > config.IDLE_WINDOW:
            with _lock:
                STATS["paused"] = True
            continue

        dt = now - prev_t
        if dt > config.INTERVAL * 5:
            # waking from idle: re-baseline counters instead of averaging the gap
            prev_ds = C.read_diskstats()
            prev_net = C.read_netdev()
            prev_cpu = C.read_cpu()
            prev_energy = C.rapl_energy()
            prev_t = now
            continue
        prev_t = now

        if now - last_rediscover > 300 or _rediscover.is_set():
            # pick up hotplugged disks / new pools / settings changes
            _rediscover.clear()
            last_rediscover = now
            disks = C.discover_disks()
            pools = C.discover_pools()

        ds = C.read_diskstats()
        net = C.read_netdev()
        cpu = C.read_cpu()

        cpu_power = None
        if prev_energy is not None:
            e = C.rapl_energy()
            if e is not None:
                d = e - prev_energy
                if d >= 0:
                    cpu_power = round(d / dt / 1e6, 1)
                prev_energy = e

        if now - last_lsi > config.LSI_EVERY:
            last_lsi = now
            lsi = C.lsi_temp()

        if now - last_slow > config.SPIN_EVERY:
            last_slow = now
            for k in disks:
                if C.is_rotational(k["dev"]):
                    st = C.spin_state(k["node"])
                else:
                    st = "active"
                spin[k["serial"]] = st
                if st == "active":
                    t = C.disk_temp(k["node"])
                    if t:
                        temps[k["serial"]] = t
                else:
                    temps.pop(k["serial"], None)

        disk_rows = []
        for k in disks:
            r = w = 0
            if k["dev"] in ds and k["dev"] in prev_ds:
                r = max(0, (ds[k["dev"]][0] - prev_ds[k["dev"]][0]) / dt)
                w = max(0, (ds[k["dev"]][1] - prev_ds[k["dev"]][1]) / dt)
            tot = used = 0
            if k["mount"]:
                p = C.resolve_usage_path(k["mount"])
                if p:
                    tot, used = C.usage(p)
            disk_rows.append({"label": k["label"], "serial": k["serial"], "dev": k["dev"],
                              "state": spin.get(k["serial"], "?"),
                              "temp": temps.get(k["serial"]),
                              "read": r, "write": w, "total": tot, "used": used})

        pool_rows = []
        for name, path in pools:
            p = C.resolve_usage_path(path)
            if p:
                tot, used = C.usage(p)
                pool_rows.append({"name": name, "total": tot, "used": used})

        ct, ci = cpu
        pt, pi = prev_cpu
        cpu_pct = round(100 * (1 - (ci - pi) / max(1, ct - pt)), 1)
        mt, mu = C.read_mem()
        hw_temps, hw_fans = C.read_hwmon()

        with _lock:
            STATS.update({
                "time": int(now), "title": config.TITLE, "paused": False,
                "interval": config.INTERVAL,
                "disks": disk_rows, "pools": pool_rows,
                "cpu": cpu_pct, "cpu_temp": C.cpu_temp(hw_temps), "cpu_power": cpu_power,
                "mem": {"total": mt, "used": mu},
                "net": {"rx": max(0, (net[0] - prev_net[0]) / dt),
                        "tx": max(0, (net[1] - prev_net[1]) / dt),
                        "ts_rx": max(0, (net[2] - prev_net[2]) / dt),
                        "ts_tx": max(0, (net[3] - prev_net[3]) / dt)},
                "gpu": C.gpu_stats(), "lsi_temp": lsi,
                "temps": hw_temps, "fans": hw_fans,
                "containers": C.docker_ps(),
            })
        prev_ds, prev_net, prev_cpu = ds, net, cpu


@app.route("/api/stats")
def api_stats():
    _last_hit[0] = time.time()
    with _lock:
        return jsonify(STATS)


@app.route("/api/settings", methods=["GET"])
def get_settings():
    s = config.current()
    s["disks"] = [{"serial": d["serial"], "dev": d["dev"], "auto": d["auto"],
                   "label": d["label"], "custom": d["custom"], "hidden": False}
                  for d in C.discover_disks()]
    # include hidden disks so they can be un-hidden from the settings page
    shown = {d["serial"] for d in s["disks"]}
    for serial in config.HIDE_DISKS:
        if serial not in shown:
            s["disks"].append({"serial": serial, "dev": "",
                               "label": config.DISK_LABELS.get(serial, serial),
                               "hidden": True})
    return jsonify(s)


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(force=True, silent=True) or {}
    try:
        config.save(data)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not write settings: {e}"}), 500
    _rediscover.set()
    return jsonify({"ok": True, "settings": config.current()})


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "static", "index.html"))


if __name__ == "__main__":
    _last_hit[0] = time.time()  # sample immediately on startup
    threading.Thread(target=sampler, daemon=True).start()
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
