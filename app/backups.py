"""Backups tab: manual, per-app backup jobs.

A job backs up one app's data folder into a subfolder of the backup root
(PANEL_BACKUP_DIR, default /backups — mount a writable volume there). Each
job offers up to two kinds of backup:

  db   — a consistent copy of the app's SQLite database, taken with SQLite's
         online backup API (safe while the app is running, read-only access
         to the source is enough). Saved as a plain .sqlite3 file.
  full — a .tar.gz of the chosen files from the data folder, with the
         database (if any) included as a consistent copy instead of the
         live file.

Jobs live in /config/backups.json. Files are named
<job>_<kind>_<YYYY-MM-DD_HHMMSS>.<ext>; the "last backup" times shown in the
UI are read from the files themselves, and old files beyond the job's keep
count are pruned after each successful run.
"""
import fnmatch
import json
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import time

import config

ROOT = os.environ.get("PANEL_BACKUP_DIR", "/backups")
JOBS_FILE = os.path.join(config.CONFIG_DIR, "backups.json")
STATE_FILE = os.path.join(config.CONFIG_DIR, "backups-state.json")
_lk = threading.Lock()

# Presets only describe *what* an app keeps where (public knowledge); the
# user supplies the actual paths in the UI.
PRESETS = {
    "vaultwarden": {
        "label": "Vaultwarden",
        "db": "db.sqlite3",
        "include": ["attachments", "sends", "config.json", "rsa_key*"],
        "hint": "DB = database only (same as the admin page's backup button). "
                "Full = database + attachments, sends, config.json and rsa_key* "
                "(per the Vaultwarden wiki; icon_cache is skipped).",
    },
    "sqlite": {
        "label": "App with a SQLite database",
        "db": "",
        "include": ["*"],
        "hint": "DB = consistent copy of the database file. "
                "Full = the whole folder, with the database copied safely.",
    },
    "folder": {
        "label": "Plain folder",
        "db": "",
        "include": ["*"],
        "hint": "Full = a .tar.gz of the folder. Stop the app first if it "
                "writes constantly.",
    },
}

NAME_RE = re.compile(r"^(?P<slug>.+)_(?P<kind>db|full)_(?P<stamp>\d{4}-\d{2}-\d{2}_\d{6})\.")


# ------------------------------------------------------------------ storage

def _read(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_jobs():
    return _read(JOBS_FILE, {"jobs": []}).get("jobs", [])


def slug(name):
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "backup"


def save_job(data):
    """Create or update a job from the editor. Returns (job, error)."""
    name = str(data.get("name", "")).strip()
    source = str(data.get("source", "")).strip()
    dest = str(data.get("dest", "")).strip().strip("/")
    db = str(data.get("db", "")).strip().strip("/")
    include = [s.strip() for s in data.get("include", []) if str(s).strip()] or ["*"]
    try:
        keep = max(1, int(data.get("keep", 14)))
    except (TypeError, ValueError):
        keep = 14
    preset = data.get("preset") if data.get("preset") in PRESETS else "folder"
    if not name or not source or not dest:
        return None, "name, data folder and destination are required"
    if not _inside_root(dest):
        return None, "destination must stay inside the backup folder"
    with _lk:
        jobs = load_jobs()
        jid = data.get("id") or f"{slug(name)}-{int(time.time())}"
        others = [j for j in jobs if j["id"] != jid]
        if any(slug(j["name"]) == slug(name) and j.get("dest") == dest for j in others):
            return None, "another job with this name already uses that destination"
        job = {"id": jid, "name": name, "preset": preset, "source": source,
               "db": db, "include": include, "dest": dest, "keep": keep}
        _write(JOBS_FILE, {"jobs": others + [job]})
    return job, None


def delete_job(jid):
    """Forget a job. Existing backup files are left untouched."""
    with _lk:
        jobs = load_jobs()
        _write(JOBS_FILE, {"jobs": [j for j in jobs if j["id"] != jid]})


def find_job(jid):
    return next((j for j in load_jobs() if j["id"] == jid), None)


def _set_state(jid, kind, entry):
    with _lk:
        st = _read(STATE_FILE, {})
        st.setdefault(jid, {})[kind] = entry
        _write(STATE_FILE, st)


# ------------------------------------------------------------------ paths

def _inside_root(dest):
    root = os.path.realpath(ROOT)
    full = os.path.realpath(os.path.join(root, dest))
    return full == root or full.startswith(root + os.sep)


def resolve_source(path):
    """Paths are entered as host paths; fall back to the /hostroot view."""
    if os.path.exists(path):
        return path
    alt = os.path.join(config.HOSTROOT, path.lstrip("/"))
    return alt if os.path.exists(alt) else path


def root_status():
    ok_dir = os.path.isdir(ROOT)
    return {"root": ROOT, "exists": ok_dir,
            "writable": ok_dir and os.access(ROOT, os.W_OK),
            "owner": config.BACKUP_OWNER}


def _owner():
    try:
        u, g = config.BACKUP_OWNER.split(":")
        return int(u), int(g)
    except (ValueError, AttributeError):
        return None


def _chown(path):
    o = _owner()
    if o:
        try:
            os.chown(path, *o)
        except OSError:
            pass


def _makedirs(path):
    """mkdir -p, chowning each directory we create."""
    missing = []
    p = path
    while not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.mkdir(d)
        _chown(d)


def backups_for(job):
    """Existing backup files for a job, newest first, grouped by kind."""
    d = os.path.join(ROOT, job["dest"])
    out = {"db": [], "full": []}
    prefix = slug(job["name"]) + "_"
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for n in names:
        if not n.startswith(prefix):
            continue
        m = NAME_RE.match(n)
        if not m or m.group("slug") != slug(job["name"]):
            continue
        try:
            size = os.path.getsize(os.path.join(d, n))
            ts = time.mktime(time.strptime(m.group("stamp"), "%Y-%m-%d_%H%M%S"))
        except (OSError, ValueError):
            continue
        out[m.group("kind")].append({"file": n, "size": size, "ts": ts})
    for k in out:
        out[k].sort(key=lambda x: -x["ts"])
    return out


def overview():
    st = _read(STATE_FILE, {})
    rows = []
    for j in load_jobs():
        files = backups_for(j)
        kinds = (["db"] if j.get("db") else []) + ["full"]
        entry = {**j, "kinds": {}}
        for k in kinds:
            last = files[k][0] if files[k] else None
            entry["kinds"][k] = {
                "last": last, "count": len(files[k]),
                "total": sum(f["size"] for f in files[k]),
                "last_run": st.get(j["id"], {}).get(k),
            }
        entry["source_ok"] = os.path.isdir(resolve_source(j["source"]))
        rows.append(entry)
    return {"jobs": rows, "root": root_status(),
            "presets": {k: {kk: vv for kk, vv in v.items()} for k, v in PRESETS.items()}}


# ------------------------------------------------------------------ running

def _sqlite_copy(job, src_path, dst_path):
    job.cmd(f"sqlite3 online backup: {src_path} → {os.path.basename(dst_path)}")
    src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
            res = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()
    if res != "ok":
        raise RuntimeError(f"integrity check of the copy failed: {res}")
    job.out(f"copied {os.path.getsize(dst_path):,} bytes, integrity check: ok")


def _match(rel, patterns):
    top = rel.split(os.sep, 1)[0]
    return any(fnmatch.fnmatch(top, p) or fnmatch.fnmatch(rel, p) for p in patterns)


def _prune(job, spec, kind):
    files = backups_for(spec)[kind]
    for f in files[spec["keep"]:]:
        p = os.path.join(ROOT, spec["dest"], f["file"])
        try:
            os.remove(p)
            job.out(f"pruned old backup {f['file']}")
        except OSError as e:
            job.err(f"could not prune {f['file']}: {e}")


def run(job, jid, kind):
    spec = find_job(jid)
    if not spec:
        return "error", "job not found"
    started = time.time()
    try:
        state, result, fname, size = _run(job, spec, kind)
    except Exception as e:
        _set_state(jid, kind, {"ts": started, "state": "error", "result": str(e)})
        raise
    _set_state(jid, kind, {"ts": started, "state": state, "result": result,
                           "file": fname, "size": size})
    return state, result


def _run(job, spec, kind):
    rs = root_status()
    if not rs["writable"]:
        job.err(f"backup folder {ROOT} is missing or not writable — mount a volume there")
        raise RuntimeError("backup folder not writable")
    src = resolve_source(spec["source"])
    if not os.path.isdir(src):
        raise RuntimeError(f"data folder not found: {spec['source']}")
    if kind == "db" and not spec.get("db"):
        raise RuntimeError("this job has no database configured")

    dest_dir = os.path.join(ROOT, spec["dest"])
    _makedirs(dest_dir)
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    base = f"{slug(spec['name'])}_{kind}_{stamp}"
    job.step(f"{spec['name']}: {kind.upper()} backup")
    job.info(f"data folder: {spec['source']}")
    job.info(f"destination: {dest_dir}")

    if kind == "db":
        final = os.path.join(dest_dir, base + ".sqlite3")
        tmp = final + ".partial"
        try:
            _sqlite_copy(job, os.path.join(src, spec["db"]), tmp)
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
    else:
        final = os.path.join(dest_dir, base + ".tar.gz")
        tmp = final + ".partial"
        db = spec.get("db")
        live_db = {db, db + "-wal", db + "-shm", db + "-journal"} if db else set()
        try:
            with tempfile.TemporaryDirectory() as td, tarfile.open(tmp, "w:gz") as tar:
                job.cmd(f"tar czf {os.path.basename(final)} (from {spec['source']})")
                if db:
                    copy = os.path.join(td, os.path.basename(db))
                    _sqlite_copy(job, os.path.join(src, db), copy)
                    tar.add(copy, arcname=db)
                    job.out(f"+ {db} (consistent copy)")
                n = 0
                for top in sorted(os.listdir(src)):
                    if top in live_db or not _match(top, spec["include"]):
                        continue
                    tar.add(os.path.join(src, top), arcname=top)
                    n += 1
                    job.out(f"+ {top}{'/' if os.path.isdir(os.path.join(src, top)) else ''}")
                if not n and not db:
                    raise RuntimeError("nothing matched the include list")
            os.replace(tmp, final)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    _chown(final)
    size = os.path.getsize(final)
    job.out(f"saved {os.path.basename(final)} ({size:,} bytes)")
    _prune(job, spec, kind)
    job.step("Done")
    return "ok", f"{os.path.basename(final)}", os.path.basename(final), size
