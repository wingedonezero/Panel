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

import backups as B
import collectors as C
import config
import jobs as J
import updater as U

app = Flask(__name__)
STATS = {}
_lock = threading.Lock()
_last_hit = [0.0]
_rediscover = threading.Event()

# Passive spin-state tracking: a tiny always-on loop watches /proc/diskstats
# (pure memory reads — provably incapable of waking a drive) and records when
# each device last did real I/O. A disk idle longer than SPIN_AFTER is shown
# as asleep. No ATA/SCSI commands are ever sent: on some HBAs (LSI SAS) even
# "standby-safe" queries like CHECK POWER MODE wake drives.
_last_io = {}
_START = time.time()
_IOSTATE = os.path.join(config.CONFIG_DIR, "iostate.json")

# Update badges: Diun (or anything else) POSTs to /api/updates/diun when a
# newer image is published. We remember which image IDs were current at that
# moment; a container still running one of them shows an "update available"
# badge, and the entry clears itself once every container has moved off the
# old image — no manual acknowledging, and Panel never talks to registries.
_UPDATES_FILE = os.path.join(config.CONFIG_DIR, "updates.json")
_updates = {}
_upd_lock = threading.Lock()


def _load_updates():
    global _updates
    try:
        import json
        _updates = json.load(open(_UPDATES_FILE))
    except Exception:
        _updates = {}


def _save_updates():
    try:
        import json
        tmp = _UPDATES_FILE + ".tmp"
        json.dump(_updates, open(tmp, "w"), indent=2)
        os.replace(tmp, _UPDATES_FILE)
    except Exception:
        pass


def annotate_updates(containers):
    """Set c["update"] on each container; prune entries nothing matches."""
    with _upd_lock:
        stale = []
        for image, e in _updates.items():
            hit = False
            for c in containers:
                if c["image"] == image and c["image_id"] in e["old_ids"]:
                    c["update"] = True
                    hit = True
            if not hit:
                stale.append(image)
        for image in stale:
            del _updates[image]
        if stale:
            _save_updates()
    for c in containers:
        c.setdefault("update", False)
    return containers


def _load_iostate():
    """Restore last-I/O times (keyed by serial) so restarts don't reset the
    idle clocks back to 'assume active'."""
    try:
        import json
        saved = json.load(open(_IOSTATE))
        for d in C.discover_disks():
            ts = saved.get(d["serial"])
            if ts:
                _last_io[d["dev"]] = float(ts)
    except Exception:
        pass


def _save_iostate():
    try:
        import json
        by_serial = {}
        for d in C.discover_disks():
            if d["dev"] in _last_io:
                by_serial[d["serial"]] = _last_io[d["dev"]]
        tmp = _IOSTATE + ".tmp"
        json.dump(by_serial, open(tmp, "w"))
        os.replace(tmp, _IOSTATE)
    except Exception:
        pass


def io_tracker():
    _load_iostate()
    prev = C.read_diskstats()
    last_save = 0.0
    while True:
        time.sleep(5)
        cur = C.read_diskstats()
        now = time.time()
        for dev, v in cur.items():
            if dev in prev and v != prev[dev]:
                _last_io[dev] = now
        prev = cur
        if now - last_save > 60:
            last_save = now
            _save_iostate()


def passive_state(dev):
    if not C.is_rotational(dev):
        return "active"
    last = _last_io.get(dev)
    if last is None:
        # No observed I/O yet (fresh start, no saved state): assume asleep.
        # A busy disk proves itself active within seconds via diskstats; the
        # reverse mistake (assume active -> temp-probe -> wake a sleeper)
        # is the one we must never make.
        return "standby"
    return "active" if time.time() - last < config.SPIN_AFTER else "standby"


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
            alerts = C.smart_alerts(config.ALERTS_ACK)
            with _lock:
                STATS["alerts"] = alerts
            for k in disks:
                st = passive_state(k["dev"])
                spin[k["serial"]] = st
                if st == "active":
                    # drive is demonstrably doing I/O anyway; smartctl still
                    # guards with -n standby as a belt-and-braces measure
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
        containers = annotate_updates(C.docker_ps())
        gpu = C.gpu_stats()
        if gpu:
            gpu["procs"] = C.gpu_procs(containers)

        with _lock:
            STATS.update({
                "time": int(now), "title": config.TITLE, "paused": False,
                "interval": config.INTERVAL, "show_graphs": config.SHOW_GRAPHS,
                "disks": disk_rows, "pools": pool_rows,
                "cpu": cpu_pct, "cpu_temp": C.cpu_temp(hw_temps), "cpu_power": cpu_power,
                "mem": {"total": mt, "used": mu},
                "net": {"rx": max(0, (net[0] - prev_net[0]) / dt),
                        "tx": max(0, (net[1] - prev_net[1]) / dt),
                        "ts_rx": max(0, (net[2] - prev_net[2]) / dt),
                        "ts_tx": max(0, (net[3] - prev_net[3]) / dt)},
                "gpu": gpu, "lsi_temp": lsi,
                "temps": hw_temps, "fans": hw_fans,
                "containers": containers,
                "updatable": list(config.UPDATE_ALLOW),
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


@app.route("/api/updates/diun", methods=["POST"])
def diun_webhook():
    """Diun webhook notifier target. Payload has at least {image, digest};
    we baseline the image IDs currently in use so the badge can self-clear."""
    data = request.get_json(force=True, silent=True) or {}
    raw = str(data.get("image", "")).strip()
    if not raw:
        return jsonify({"ok": False, "error": "no image in payload"}), 400
    image = C.norm_image(raw)
    old_ids = sorted({c["image_id"] for c in C.docker_ps()
                      if c["image"] == image and c["image_id"]})
    if not old_ids:
        # no container uses this image (anymore) — nothing to badge
        return jsonify({"ok": True, "matched": False})
    with _upd_lock:
        _updates[image] = {"digest": data.get("digest", ""),
                           "ts": int(time.time()), "old_ids": old_ids}
        _save_updates()
    return jsonify({"ok": True, "matched": True})


@app.route("/api/alerts/ack", methods=["POST"])
def ack_alerts():
    config.save({"alerts_ack": time.time()})
    with _lock:
        STATS["alerts"] = []
    return jsonify({"ok": True})


@app.route("/api/settings", methods=["POST"])
def post_settings():
    data = request.get_json(force=True, silent=True) or {}
    try:
        config.save(data)
    except OSError as e:
        return jsonify({"ok": False, "error": f"could not write settings: {e}"}), 500
    _rediscover.set()
    return jsonify({"ok": True, "settings": config.current()})


# ---------------------------------------------------------------- jobs

@app.route("/api/jobs/<jid>")
def job_view(jid):
    j = J.get(jid)
    if not j:
        return jsonify({"ok": False, "error": "no such job"}), 404
    return jsonify(j.view(int(request.args.get("since", 0))))


# ---------------------------------------------------------------- backups

@app.route("/api/backups")
def backups_overview():
    data = B.overview()
    for row in data["jobs"]:
        for kind in row["kinds"]:
            j = J.latest(f"backup:{row['id']}:{kind}")
            row["kinds"][kind]["job"] = ({"id": j.id, "state": j.state} if j else None)
    return jsonify(data)


@app.route("/api/backups/jobs", methods=["POST"])
def backups_save():
    job, err = B.save_job(request.get_json(force=True, silent=True) or {})
    if err:
        return jsonify({"ok": False, "error": err}), 400
    return jsonify({"ok": True, "job": job})


@app.route("/api/backups/jobs/<jid>", methods=["DELETE"])
def backups_delete(jid):
    B.delete_job(jid)
    return jsonify({"ok": True})


@app.route("/api/backups/run/<jid>/<kind>", methods=["POST"])
def backups_run(jid, kind):
    spec = B.find_job(jid)
    if not spec or kind not in ("db", "full"):
        return jsonify({"ok": False, "error": "unknown job or kind"}), 404
    job, started = J.start(f"backup:{jid}:{kind}",
                           f"{spec['name']} — {kind.upper()} backup", B.run, jid, kind)
    return jsonify({"ok": True, "job": job.id, "started": started})


# ---------------------------------------------------------------- updater

@app.route("/api/update/containers")
def update_containers():
    upd = {c["name"]: c["update"] for c in annotate_updates(C.docker_ps())}
    out = []
    for c in U.candidates():
        j = J.latest(f"update:{c['name']}")
        out.append({**c, "allowed": U.allowed(c["name"]),
                    "update": upd.get(c["name"], False),
                    "job": ({"id": j.id, "state": j.state, "result": j.result,
                             "ended": j.ended} if j else None)})
    return jsonify({"containers": out})


@app.route("/api/update/<name>/plan")
def update_plan(name):
    if not U.allowed(name):
        return jsonify({"ok": False, "error": "not enabled for one-click update"}), 403
    try:
        return jsonify({"ok": True, **U.plan(name)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/update/<name>", methods=["POST"])
def update_run(name):
    if not U.allowed(name):
        return jsonify({"ok": False, "error": "not enabled for one-click update"}), 403
    force = bool((request.get_json(force=True, silent=True) or {}).get("force"))
    job, started = J.start(f"update:{name}", f"Update {name}", U.run, name, force)
    return jsonify({"ok": True, "job": job.id, "started": started})


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "static", "index.html"))


def _host_timezone():
    """Use the host's timezone (for backup file names) unless TZ is set."""
    if os.environ.get("TZ"):
        return
    try:
        tz = open(os.path.join(config.HOSTROOT, "etc/timezone")).read().strip()
        if tz:
            os.environ["TZ"] = tz
    except OSError:
        lt = os.path.join(config.HOSTROOT, "etc/localtime")
        if os.path.exists(lt):
            os.environ["TZ"] = ":" + lt
    time.tzset()


if __name__ == "__main__":
    _host_timezone()
    _last_hit[0] = time.time()  # sample immediately on startup
    _load_updates()
    threading.Thread(target=io_tracker, daemon=True).start()
    threading.Thread(target=sampler, daemon=True).start()
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
