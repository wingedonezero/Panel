"""One-click update for *standalone* containers (created with `docker run`,
not by compose/stack tools, which should redeploy their own stacks).

Flow: pull -> compare image -> stop -> rename old aside -> create an
identical container on the new image -> start -> verify it stays up ->
remove the old container (and its now-unused image). If anything fails
after the old container was stopped, the new one is removed and the old one
is renamed back and restarted.

Settings copied from the old container are filtered against the *old
image's* defaults, so values that came from the image (ENV, labels, CMD,
entrypoint, ...) are taken fresh from the new image instead of being frozen.
"""
import json
import re
import time

import config
import docker_api as D

COMPOSE_LABEL = "com.docker.compose.project"


def _self_id():
    """Our own container ID. cgroup v2 with a private cgroup namespace hides
    it from /proc/self/cgroup, but Docker's per-container resolv.conf /
    hostname bind mounts show it in mountinfo."""
    pats = [("/proc/self/mountinfo", r"/containers/([0-9a-f]{64})/(?:resolv\.conf|hostname|hosts)"),
            ("/proc/self/cgroup", r"docker[/-]([0-9a-f]{64})")]
    for path, pat in pats:
        try:
            m = re.search(pat, open(path).read())
            if m:
                return m.group(1)
        except OSError:
            pass
    return ""


SELF_ID = _self_id()


def candidates():
    """Standalone containers that could be offered for one-click update."""
    out = []
    try:
        for c in D.request("GET", "/containers/json", {"all": 1}):
            labels = c.get("Labels") or {}
            name = c["Names"][0].lstrip("/")
            out.append({
                "name": name,
                "image": c.get("Image", ""),
                "state": c.get("State", ""),
                "standalone": COMPOSE_LABEL not in labels,
                "self": bool(SELF_ID) and c["Id"] == SELF_ID,
            })
    except Exception:
        pass
    return sorted(out, key=lambda x: x["name"])


def allowed(name):
    return name in config.UPDATE_ALLOW


# ---------------------------------------------------------------- spec build

def _strip_image_defaults(cfg, img):
    """Return container Config minus values inherited from the image."""
    img = img or {}
    out = {}
    for k in ("Hostname", "Domainname", "User", "AttachStdin", "AttachStdout",
              "AttachStderr", "Tty", "OpenStdin", "StdinOnce", "Env", "Cmd",
              "Entrypoint", "WorkingDir", "Labels", "ExposedPorts", "Volumes",
              "StopSignal", "StopTimeout", "Healthcheck", "NetworkDisabled",
              "Shell"):
        if k in cfg and cfg[k] not in (None, "", [], {}):
            out[k] = cfg[k]
    for k in ("User", "Cmd", "Entrypoint", "WorkingDir", "StopSignal",
              "Healthcheck", "Shell"):
        if k in out and out[k] == img.get(k):
            del out[k]
    if "Env" in out:
        base = set(img.get("Env") or [])
        env = [e for e in out["Env"] if e not in base]
        if env:
            out["Env"] = env
        else:
            del out["Env"]
    if "Labels" in out:
        base = img.get("Labels") or {}
        lab = {k: v for k, v in out["Labels"].items() if base.get(k) != v}
        if lab:
            out["Labels"] = lab
        else:
            del out["Labels"]
    for k in ("ExposedPorts", "Volumes"):
        if k in out:
            base = img.get(k) or {}
            left = {p: v for p, v in out[k].items() if p not in base}
            if left:
                out[k] = left
            else:
                del out[k]
    return out


def build_spec(info, img_cfg):
    """Container create body + extra networks, from `docker inspect` output."""
    cfg = dict(info["Config"])
    body = _strip_image_defaults(cfg, img_cfg)
    if body.get("Hostname") == info["Id"][:12]:
        del body["Hostname"]            # docker's auto hostname, not a setting
    body["Image"] = cfg["Image"]
    hc = json.loads(json.dumps(info["HostConfig"]))

    # anonymous volumes live only in Mounts: re-attach them by name so the
    # new container keeps their data instead of getting fresh empty ones
    covered = set()
    for b in hc.get("Binds") or []:
        parts = b.split(":")
        if len(parts) >= 2:
            covered.add(parts[1])
    for m in hc.get("Mounts") or []:
        covered.add(m.get("Target"))
    for m in info.get("Mounts") or []:
        if m.get("Type") == "volume" and m.get("Destination") not in covered:
            bind = f"{m['Name']}:{m['Destination']}" + ("" if m.get("RW", True) else ":ro")
            hc.setdefault("Binds", [])
            hc["Binds"] = (hc["Binds"] or []) + [bind]
    body["HostConfig"] = hc

    extra = []
    mode = hc.get("NetworkMode") or "default"
    nets = (info.get("NetworkSettings") or {}).get("Networks") or {}
    if mode not in ("host", "none") and not mode.startswith("container:") and nets:
        eps = {}
        short = info["Id"][:12]
        for net, ep in nets.items():
            keep = {}
            for k in ("IPAMConfig", "Links", "DriverOpts"):
                if ep.get(k):
                    keep[k] = ep[k]
            aliases = [a for a in (ep.get("Aliases") or []) if a != short]
            if aliases:
                keep["Aliases"] = aliases
            eps[net] = keep
        first = mode if mode in eps else next(iter(eps))
        body["NetworkingConfig"] = {"EndpointsConfig": {first: eps[first]}}
        extra = [(n, e) for n, e in eps.items() if n != first]
    return body, extra


def plan(name):
    """Everything the update will do, for the confirmation screen."""
    info = D.request("GET", f"/containers/{name}/json")
    img = D.request("GET", f"/images/{info['Image']}/json")
    body, extra = build_spec(info, img.get("Config"))
    ref = info["Config"]["Image"]
    repo, tag = D.split_ref(ref)
    old = f"{name}-panel-old"
    running = info["State"]["Running"]
    stop_t = info["Config"].get("StopTimeout") or 30
    steps = [
        ("Pull the image", f"docker pull {ref}",
         f"POST /images/create?fromImage={repo}" + (f"&tag={tag}" if tag else "")),
        ("Compare with the running image", f"docker image inspect {ref}",
         f"GET /images/{ref}/json  (stop here if the ID is unchanged)"),
    ]
    if running:
        steps.append(("Stop the current container", f"docker stop -t {stop_t} {name}",
                      f"POST /containers/{name}/stop?t={stop_t}"))
    steps += [
        ("Keep the old container aside", f"docker rename {name} {old}",
         f"POST /containers/{name}/rename?name={old}"),
        ("Create the new container (same settings, new image)",
         f"docker create --name {name} …  (full config below)",
         f"POST /containers/create?name={name}"),
    ]
    for n, _ in extra:
        steps.append((f"Connect network {n}", f"docker network connect {n} {name}",
                      f"POST /networks/{n}/connect"))
    if running:
        steps += [("Start it", f"docker start {name}", f"POST /containers/{name}/start"),
                  ("Verify it stays running", f"docker inspect {name}",
                   f"GET /containers/{name}/json  (up to 90s; rollback on failure)")]
    steps += [
        ("Remove the old container", f"docker rm {old}", f"DELETE /containers/{old}"),
        ("Remove the old image if it is untagged and unused",
         f"docker rmi {info['Image'][7:19]}", f"DELETE /images/{info['Image'][:19]}…"),
    ]
    return {
        "name": name, "image": ref, "image_id": info["Image"],
        "running": running, "standalone": COMPOSE_LABEL not in (info["Config"].get("Labels") or {}),
        "steps": [{"what": a, "cli": b, "api": c} for a, b, c in steps],
        "create_body": body,
        "extra_networks": [n for n, _ in extra],
        "rollback": ["docker rm -f " + name, f"docker rename {old} {name}",
                     f"docker start {name}"],
    }


# ---------------------------------------------------------------- execution

def _demux(raw):
    """Docker log stream (non-TTY) has 8-byte frame headers; strip them."""
    if isinstance(raw, str):
        raw = raw.encode()
    out, i = [], 0
    while i + 8 <= len(raw) and raw[i] in (0, 1, 2) and raw[i + 1:i + 4] == b"\0\0\0":
        n = int.from_bytes(raw[i + 4:i + 8], "big")
        out.append(raw[i + 8:i + 8 + n])
        i += 8 + n
    text = b"".join(out) if out else raw
    return text.decode(errors="replace")


def _tail_logs(job, cid):
    try:
        conn = D._UnixConn(10)
        conn.request("GET", f"/containers/{cid}/logs?stdout=1&stderr=1&tail=40")
        raw = conn.getresponse().read()
        conn.close()
        job.info("last log lines of the new container:")
        for line in _demux(raw).splitlines()[-40:]:
            job.out("  " + line)
    except Exception as e:
        job.err(f"could not read its logs: {e}")


def _call(job, method, path, params=None, body=None, cli=None, timeout=60):
    if cli:
        job.cmd(f"$ {cli}")
    q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
    job.cmd(f"  → {method} {path}{q}")
    res = D.request(method, path, params, body, timeout)
    return res


def run(job, name, force=False):
    if not allowed(name):
        job.err(f"{name} is not enabled for one-click update (Settings)")
        return "error", "not allowed"
    info = D.request("GET", f"/containers/{name}/json")
    if SELF_ID and info["Id"] == SELF_ID:
        job.err("Panel cannot update its own container")
        return "error", "refusing to update Panel itself"
    labels = info["Config"].get("Labels") or {}
    if COMPOSE_LABEL in labels:
        job.err(f"{name} belongs to compose project '{labels[COMPOSE_LABEL]}' — "
                "update it by redeploying that stack instead")
        return "error", "not a standalone container"

    ref = info["Config"]["Image"]
    old_img = info["Image"]
    was_running = info["State"]["Running"]
    repo, tag = D.split_ref(ref)
    old_name = f"{name}-panel-old"
    try:
        D.request("GET", f"/containers/{old_name}/json")
        job.err(f"a container named {old_name} already exists (left over from an "
                "earlier attempt?) — remove or rename it first; nothing was changed")
        return "error", f"{old_name} exists"
    except D.DockerError as e:
        if e.status != 404:
            raise

    # 1. pull
    job.step(f"Pulling {ref}")
    params = {"fromImage": repo}
    if tag:
        params["tag"] = tag
    job.cmd(f"$ docker pull {ref}")
    job.cmd(f"  → POST /images/create?fromImage={repo}" + (f"&tag={tag}" if tag else ""))
    seen = set()
    for ev in D.stream("POST", "/images/create", params):
        if ev.get("error"):
            job.err(ev["error"])
            return "error", "pull failed"
        st, lid = ev.get("status", ""), ev.get("id", "")
        key = (lid, st.split(" ")[0])
        if st.startswith(("Downloading", "Extracting", "Waiting", "Verifying")):
            if key in seen:
                continue
        seen.add(key)
        job.out(f"{lid + ': ' if lid else ''}{st}")

    # 2. compare
    job.step("Comparing images")
    new_img = _call(job, "GET", f"/images/{ref}/json", cli=f"docker image inspect {ref}")["Id"]
    job.info(f"running image: {old_img}")
    job.info(f"pulled image:  {new_img}")
    if new_img == old_img and not force:
        job.info("Already up to date — nothing to do.")
        return "noop", "already up to date"
    if new_img == old_img:
        job.info("Same image, but recreate was forced.")

    img_cfg = D.request("GET", f"/images/{old_img}/json").get("Config")
    body, extra = build_spec(info, img_cfg)
    job.info("create body: " + json.dumps(body, indent=2))
    stop_t = info["Config"].get("StopTimeout") or 30
    new_id = None
    renamed = False

    try:
        # 3. stop + rename aside
        if was_running:
            job.step(f"Stopping {name}")
            _call(job, "POST", f"/containers/{info['Id']}/stop", {"t": stop_t},
                  cli=f"docker stop -t {stop_t} {name}", timeout=stop_t + 30)
        job.step("Renaming the old container aside")
        _call(job, "POST", f"/containers/{info['Id']}/rename", {"name": old_name},
              cli=f"docker rename {name} {old_name}")
        renamed = True

        # 4. create (+ extra networks) + start
        job.step(f"Creating new {name}")
        res = _call(job, "POST", "/containers/create", {"name": name}, body,
                    cli=f"docker create --name {name} … (body above)")
        new_id = res["Id"]
        job.info(f"new container id: {new_id[:12]}")
        for w in res.get("Warnings") or []:
            job.info(f"warning: {w}")
        for net, ep in extra:
            _call(job, "POST", f"/networks/{net}/connect", None,
                  {"Container": new_id, "EndpointConfig": ep},
                  cli=f"docker network connect {net} {name}")

        if was_running:
            job.step(f"Starting {name}")
            _call(job, "POST", f"/containers/{new_id}/start", cli=f"docker start {name}")

            # 5. verify
            job.step("Verifying it stays up")
            deadline, stable_since = time.time() + 90, None
            base_restarts = D.request("GET", f"/containers/{new_id}/json").get("RestartCount", 0)
            while True:
                cur = D.request("GET", f"/containers/{new_id}/json")
                st = cur["State"]
                health = (st.get("Health") or {}).get("Status")
                # a crash-looping container flickers "running" between restarts:
                # treat restarting / a rising restart count as failure too
                if st.get("Restarting") or cur.get("RestartCount", 0) > base_restarts:
                    raise RuntimeError(f"new container keeps restarting (last exit code "
                                       f"{st.get('ExitCode')})")
                if not st.get("Running"):
                    raise RuntimeError(f"new container exited (code {st.get('ExitCode')}): "
                                       f"{st.get('Error') or 'see logs'}")
                if health == "unhealthy":
                    raise RuntimeError("new container reports unhealthy")
                if health in (None, "healthy"):
                    stable_since = stable_since or time.time()
                    if time.time() - stable_since >= 10:
                        job.out(f"running{' and healthy' if health else ''} for 10s — OK")
                        break
                if time.time() > deadline:
                    if health == "starting":
                        job.info("health check still 'starting' after 90s — keeping it, check manually")
                        break
                    raise RuntimeError("timed out waiting for the new container")
                time.sleep(2)
    except Exception as e:
        job.err(f"FAILED: {e}")
        if new_id:
            _tail_logs(job, new_id)
        job.step("Rolling back")
        try:
            if new_id:
                _call(job, "DELETE", f"/containers/{new_id}", {"force": 1},
                      cli=f"docker rm -f {name}")
            if renamed:
                _call(job, "POST", f"/containers/{info['Id']}/rename", {"name": name},
                      cli=f"docker rename {old_name} {name}")
            if was_running:
                _call(job, "POST", f"/containers/{info['Id']}/start", cli=f"docker start {name}")
            job.info("rolled back — the old container is running again")
        except Exception as e2:
            job.err(f"ROLLBACK FAILED: {e2}")
            job.err(f"manual fix: the old container is '{old_name if renamed else name}' "
                    f"(id {info['Id'][:12]})")
        return "error", str(e)

    # 6. clean up
    job.step("Removing the old container")
    try:
        _call(job, "DELETE", f"/containers/{info['Id']}", cli=f"docker rm {old_name}")
    except Exception as e:
        job.err(f"could not remove {old_name}: {e} (safe to remove by hand)")
    if new_img != old_img:
        job.step("Removing the old image")
        try:
            tags = [t for t in (D.request("GET", f"/images/{old_img}/json").get("RepoTags") or [])
                    if t != "<none>:<none>"]
            if tags:
                job.info(f"kept old image — it is still tagged {', '.join(tags)}")
            else:
                _call(job, "DELETE", f"/images/{old_img}", cli=f"docker rmi {old_img[7:19]}")
        except D.DockerError as e:
            job.info(f"kept old image: {e}")
    job.step("Done")
    return "ok", "updated" if new_img != old_img else "recreated"
