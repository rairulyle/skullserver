#!/usr/bin/env python3
"""Webhook receiver for the seedbox arrs: on import/rename/delete it
refreshes the rclone VFS dir cache for the affected folder via the RC on
the host, waits until the file is visible through the mount, then asks
Plex for a partial scan of that folder. Plex's periodic scans and the
mount's dir-cache TTL remain the backstop."""
import hmac
import json
import os
import queue
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

TOKEN = os.environ["SEEDBOX_TRIGGER_TOKEN"]
PLEX_URL = os.environ.get("PLEX_URL", "http://localhost:32400").rstrip("/")
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
RCLONE_RC_URL = os.environ.get("RCLONE_RC_URL", "http://localhost:5572").rstrip("/")
SEEDBOX_PREFIX = os.environ.get("SEEDBOX_PREFIX", "/home/skullpluggery/media").rstrip("/")
MOUNT_PREFIX = os.environ.get("MOUNT_PREFIX", "/remote/seedbox1/data").rstrip("/")
LISTEN_PORT = int(os.environ.get("LISTEN_PORT", "9595"))
VERIFY_ATTEMPTS = int(os.environ.get("VERIFY_ATTEMPTS", "6"))
VERIFY_DELAY = int(os.environ.get("VERIFY_DELAY", "5"))

jobs = queue.Queue()
queued = set()
queued_lock = threading.Lock()
sections_cache = None


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def rc(call, params):
    req = urllib.request.Request(
        f"{RCLONE_RC_URL}/{call}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.load(resp)


def refresh_dir(rel, recursive=True):
    params = {"dir": rel}
    if recursive:
        params["recursive"] = "true"
    try:
        result = rc("vfs/refresh", params).get("result", {})
    except Exception as e:
        log(f"vfs/refresh {rel!r} failed: {e}")
        return False
    status = result.get(rel) or next(iter(result.values()), "")
    if status == "OK":
        return True
    log(f"vfs/refresh {rel!r}: {status!r}")
    return False


def plex_api(path, method="GET"):
    req = urllib.request.Request(
        f"{PLEX_URL}{path}",
        headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read()
        if resp.headers.get("Content-Type", "").startswith("application/json"):
            return json.loads(body)
        return body


def section_for(path):
    global sections_cache
    for attempt in range(2):
        if sections_cache is None or attempt:
            dirs = plex_api("/library/sections")["MediaContainer"]["Directory"]
            sections_cache = [
                (loc["path"].rstrip("/"), str(d["key"]), d["title"])
                for d in dirs
                for loc in d.get("Location", [])
            ]
        best = max(
            (s for s in sections_cache if path == s[0] or path.startswith(s[0] + "/")),
            key=lambda s: len(s[0]),
            default=None,
        )
        if best:
            return best
    return None


def to_rel(abs_path):
    if abs_path == SEEDBOX_PREFIX or abs_path.startswith(SEEDBOX_PREFIX + "/"):
        return abs_path[len(SEEDBOX_PREFIX):].strip("/")
    return None


def extract(payload):
    folder = payload.get("movie", {}).get("folderPath") or payload.get("series", {}).get("path")
    if folder:
        folder = folder.rstrip("/")
    media_file = payload.get("movieFile") or payload.get("episodeFile") or {}
    file_path = media_file.get("path")
    if not file_path and folder and media_file.get("relativePath"):
        file_path = f"{folder}/{media_file['relativePath']}"
    if payload.get("eventType") != "Download":
        file_path = None
    return folder, file_path


def process(event, folder, file_path):
    rel = to_rel(folder)
    if not rel:
        log(f"{event}: folder {folder!r} outside {SEEDBOX_PREFIX!r}; skipping")
        return
    scan_path = f"{MOUNT_PREFIX}/{rel}"
    verify = None
    if file_path:
        rel_file = to_rel(file_path)
        if rel_file:
            verify = f"{MOUNT_PREFIX}/{rel_file}"
    if verify and os.path.exists(verify):
        log(f"{event}: {verify} already visible; skipping refresh")
    else:
        if not refresh_dir(rel):
            parent = os.path.dirname(rel)
            if parent:
                refresh_dir(parent, recursive=False)
            if not refresh_dir(rel) and not verify:
                scan_path = f"{MOUNT_PREFIX}/{parent}".rstrip("/")
                log(f"{event}: {rel!r} gone from remote; scanning parent instead")
        if verify:
            for _ in range(VERIFY_ATTEMPTS):
                if os.path.exists(verify):
                    break
                time.sleep(VERIFY_DELAY)
                refresh_dir(rel)
            else:
                log(f"{event}: {verify} still not visible after refreshes; scanning anyway")
    section = section_for(scan_path)
    if not section:
        log(f"{event}: no Plex section covers {scan_path!r}; skipping scan")
        return
    _, key, title = section
    plex_api(f"/library/sections/{key}/refresh?path={urllib.parse.quote(scan_path)}")
    log(f"{event}: triggered scan of {title!r} at {scan_path}")
    if "Delete" in event:
        # global auto-empty-trash is off so a dead mount can't purge
        # libraries; arr-confirmed deletes clean up their section instead
        time.sleep(15)
        plex_api(f"/library/sections/{key}/emptyTrash", method="PUT")
        log(f"{event}: emptied trash for {title!r}")


def worker():
    while True:
        event, folder, file_path = jobs.get()
        try:
            process(event, folder, file_path)
        except Exception as e:
            log(f"{event}: processing failed for {folder}: {e}")
        finally:
            with queued_lock:
                queued.discard((folder, file_path))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _respond(self, code, body=b"ok\n"):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urllib.parse.urlparse(self.path).path == "/health":
            return self._respond(200)
        self._respond(404, b"not found\n")

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path != "/webhook":
            return self._respond(404, b"not found\n")
        token = (urllib.parse.parse_qs(url.query).get("token") or [""])[0]
        if not hmac.compare_digest(token, TOKEN):
            log(f"unauthorized request from {self.client_address[0]}")
            return self._respond(403, b"forbidden\n")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._respond(400, b"bad json\n")
        event = payload.get("eventType", "unknown")
        if event == "Test":
            log(f"test event from {payload.get('instanceName', 'arr')}")
            return self._respond(200)
        folder, file_path = extract(payload)
        if not folder:
            log(f"{event}: no folder in payload (keys: {sorted(payload)})")
            return self._respond(200)
        job_key = (folder, file_path)
        with queued_lock:
            if job_key in queued:
                log(f"{event}: {file_path or folder} already queued; ignoring duplicate")
                return self._respond(200)
            queued.add(job_key)
        jobs.put((event, folder, file_path))
        log(f"{event}: queued {file_path or folder}")
        self._respond(200)


def main():
    log(
        f"seedbox-trigger starting: port={LISTEN_PORT}, plex={PLEX_URL}, "
        f"rc={RCLONE_RC_URL}, map={SEEDBOX_PREFIX} -> {MOUNT_PREFIX}"
    )
    threading.Thread(target=worker, daemon=True).start()
    HTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
