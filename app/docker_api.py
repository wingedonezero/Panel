"""Minimal Docker Engine API client over the unix socket (stdlib only).

Every call is recorded as "METHOD /path" so the UI can show exactly what
Panel asked the daemon to do.
"""
import http.client
import json
import socket
import urllib.parse

SOCK = "/var/run/docker.sock"


class _UnixConn(http.client.HTTPConnection):
    def __init__(self, timeout):
        super().__init__("docker", timeout=timeout)

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(SOCK)
        self.sock = s


class DockerError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def _path(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    return path


def request(method, path, params=None, body=None, timeout=60):
    """One API call; returns decoded JSON (or None for empty bodies)."""
    conn = _UnixConn(timeout)
    try:
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, _path(path, params), body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 400:
            try:
                msg = json.loads(raw).get("message", raw.decode(errors="replace"))
            except Exception:
                msg = raw.decode(errors="replace")
            raise DockerError(resp.status, msg.strip())
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")
    finally:
        conn.close()


def stream(method, path, params=None, timeout=900):
    """Yield JSON objects from a streaming endpoint (e.g. image pull)."""
    conn = _UnixConn(timeout)
    try:
        conn.request(method, _path(path, params))
        resp = conn.getresponse()
        if resp.status >= 400:
            raw = resp.read()
            try:
                msg = json.loads(raw).get("message", raw.decode(errors="replace"))
            except Exception:
                msg = raw.decode(errors="replace")
            raise DockerError(resp.status, msg.strip())
        buf = b""
        while True:
            chunk = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if line.strip():
                    try:
                        yield json.loads(line)
                    except ValueError:
                        yield {"status": line.decode(errors="replace")}
        if buf.strip():
            try:
                yield json.loads(buf)
            except ValueError:
                yield {"status": buf.decode(errors="replace")}
    finally:
        conn.close()


def split_ref(ref):
    """'repo/name:tag' -> ('repo/name', 'tag'); digests are kept whole."""
    if "@" in ref:
        return ref, ""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"
