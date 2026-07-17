#!/usr/bin/env python3
"""Watchdog for npm-docker-sync. Design: docs/specs/npm-sync-guard.md"""
import http.client
import json
import os
import re
import socket
import time
import urllib.parse
import urllib.request

NPM_URL = os.environ["NPM_URL"].rstrip("/")
NPM_EMAIL = os.environ["NPM_EMAIL"]
NPM_PASSWORD = os.environ["NPM_PASSWORD"]
KUMA_PUSH_URL = os.environ.get("KUMA_PUSH_URL", "").rstrip("/")
SYNC_CONTAINER = os.environ.get("SYNC_CONTAINER", "npm-docker-sync")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
GRACE = int(os.environ.get("GRACE", "30"))
HEAL_WAIT = int(os.environ.get("HEAL_WAIT", "45"))
AUTO_HEAL = os.environ.get("AUTO_HEAL", "true").lower() == "true"

DOMAIN_LABEL = re.compile(r"^npm\.proxy(\.\d+)?\.domain$")


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker")

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


def docker_api(method, path):
    conn = DockerConnection()
    try:
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"docker {method} {path} -> {resp.status}: {body[:200]}")
        return json.loads(body) if body else None
    finally:
        conn.close()


_npm_token = None


def npm_api(path, retry=True):
    global _npm_token
    if _npm_token is None:
        req = urllib.request.Request(
            f"{NPM_URL}/api/tokens",
            data=json.dumps({"identity": NPM_EMAIL, "secret": NPM_PASSWORD}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            _npm_token = json.load(resp)["token"]
    req = urllib.request.Request(
        f"{NPM_URL}{path}", headers={"Authorization": f"Bearer {_npm_token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403) and retry:
            _npm_token = None
            return npm_api(path, retry=False)
        raise


def snapshot():
    """(missing domains, total expected) — labels on running containers vs enabled NPM hosts."""
    expected = set()
    for c in docker_api("GET", "/containers/json"):
        for key, value in (c.get("Labels") or {}).items():
            if DOMAIN_LABEL.match(key):
                expected.update(d.lower() for d in re.split(r"[,\s]+", value.strip()) if d)
    actual = {
        d.lower()
        for h in npm_api("/api/nginx/proxy-hosts")
        if h.get("enabled")
        for d in h.get("domain_names", [])
    }
    return expected - actual, len(expected)


def push_kuma(up, msg):
    if not KUMA_PUSH_URL:
        return
    query = urllib.parse.urlencode({"status": "up" if up else "down", "msg": msg[:250]})
    try:
        urllib.request.urlopen(f"{KUMA_PUSH_URL}?{query}", timeout=15).read()
    except Exception as e:
        log(f"kuma push failed: {e}")


def check_cycle(prev_missing):
    missing, total = snapshot()
    if missing:
        log(f"possible drift, re-checking in {GRACE}s: {sorted(missing)}")
        time.sleep(GRACE)
        missing, total = snapshot()

    if missing and AUTO_HEAL and missing != prev_missing:
        log(f"drift confirmed, restarting {SYNC_CONTAINER}: {sorted(missing)}")
        try:
            docker_api("POST", f"/containers/{SYNC_CONTAINER}/restart?t=10")
        except Exception as e:
            log(f"restart failed: {e}")
        time.sleep(HEAL_WAIT)
        before = missing
        missing, total = snapshot()
        healed = sorted(before - missing)
        if healed and not missing:
            log(f"auto-healed: {healed}")
            push_kuma(True, f"auto-healed: {', '.join(healed)}")
            return set()
        if healed:
            log(f"partially healed: {healed}")

    if missing:
        log(f"missing from NPM: {sorted(missing)}")
        push_kuma(False, f"missing: {', '.join(sorted(missing))}")
        return missing

    log(f"in sync ({total} domains)")
    push_kuma(True, f"in sync ({total} domains)")
    return set()


def main():
    log(
        f"npm-sync-guard starting: npm={NPM_URL}, interval={CHECK_INTERVAL}s, "
        f"auto_heal={AUTO_HEAL}, kuma={'on' if KUMA_PUSH_URL else 'off'}"
    )
    prev_missing = set()
    once = os.environ.get("ONCE", "") == "1"
    while True:
        try:
            prev_missing = check_cycle(prev_missing)
        except Exception as e:
            log(f"check failed: {e}")
            push_kuma(False, f"check failed: {e}")
        if once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
