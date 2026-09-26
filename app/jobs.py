"""Background jobs with a live, line-by-line log the UI can poll.

Used by the Backups tab and the container updater. One job per "key"
(e.g. "update:<container>") may run at a time; the most recent job per key is
kept in memory and its log is also written to /config/logs/ for later.
"""
import os
import re
import threading
import time
import traceback

import config

_jobs = {}          # id -> Job
_latest = {}        # key -> id
_lock = threading.Lock()
_seq = [0]
LOG_DIR = os.path.join(config.CONFIG_DIR, "logs")
KEEP_LOGS = 50


class Job:
    def __init__(self, key, title):
        _seq[0] += 1
        self.id = f"{int(time.time())}-{_seq[0]}"
        self.key = key
        self.title = title
        self.state = "running"      # running | ok | error | noop
        self.result = ""
        self.started = time.time()
        self.ended = None
        self.lines = []             # [ts, kind, text]  kind: step|cmd|out|ok|err|info
        self._lk = threading.Lock()

    def log(self, kind, text):
        with self._lk:
            for part in str(text).splitlines() or [""]:
                self.lines.append([round(time.time(), 2), kind, part])

    def step(self, text):
        self.log("step", text)

    def cmd(self, text):
        self.log("cmd", text)

    def out(self, text):
        self.log("out", text)

    def info(self, text):
        self.log("info", text)

    def err(self, text):
        self.log("err", text)

    def view(self, since=0):
        with self._lk:
            return {"id": self.id, "key": self.key, "title": self.title,
                    "state": self.state, "result": self.result,
                    "started": self.started, "ended": self.ended,
                    "lines": self.lines[since:], "count": len(self.lines)}

    def _write_log(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", self.key)
            stamp = time.strftime("%Y-%m-%d_%H%M%S", time.localtime(self.started))
            path = os.path.join(LOG_DIR, f"{stamp}_{safe}.log")
            with open(path, "w") as f:
                f.write(f"{self.title}\nresult: {self.state} {self.result}\n\n")
                for ts, kind, text in self.lines:
                    t = time.strftime("%H:%M:%S", time.localtime(ts))
                    f.write(f"{t} [{kind}] {text}\n")
            logs = sorted(os.listdir(LOG_DIR))
            for old in logs[:-KEEP_LOGS]:
                os.remove(os.path.join(LOG_DIR, old))
        except OSError:
            pass


def running(key):
    with _lock:
        jid = _latest.get(key)
        j = _jobs.get(jid)
        return j if j and j.state == "running" else None


def start(key, title, fn, *args):
    """Run fn(job, *args) in a thread. fn returns (state, result) or raises."""
    with _lock:
        cur = _jobs.get(_latest.get(key))
        if cur and cur.state == "running":
            return cur, False
        job = Job(key, title)
        _jobs[job.id] = job
        _latest[key] = job.id
        # keep memory bounded: drop finished jobs that are no longer "latest"
        live = set(_latest.values())
        for jid in [j for j in _jobs if j not in live]:
            del _jobs[jid]

    def run():
        try:
            state, result = fn(job, *args)
        except Exception as e:  # noqa: BLE001 - surface everything in the log
            job.err(f"unexpected error: {e}")
            job.err(traceback.format_exc().rstrip())
            state, result = "error", str(e)
        job.state, job.result, job.ended = state, result, time.time()
        job._write_log()

    threading.Thread(target=run, daemon=True).start()
    return job, True


def get(jid):
    with _lock:
        return _jobs.get(jid)


def latest(key):
    with _lock:
        return _jobs.get(_latest.get(key))
