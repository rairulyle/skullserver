#!/usr/bin/env python3
"""Watchdog for npm-docker-sync: restarts it when npm.proxy-labeled containers
are missing their NPM proxy hosts, and reports state changes to Discord."""
import http.client
import json
import os
import re
import socket
import time
import urllib.request

NPM_URL = os.environ["NPM_URL"].rstrip("/")
NPM_EMAIL = os.environ["NPM_EMAIL"]
NPM_PASSWORD = os.environ["NPM_PASSWORD"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
SYNC_CONTAINER = os.environ.get("SYNC_CONTAINER", "npm-docker-sync")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "300"))
GRACE = int(os.environ.get("GRACE", "30"))
HEAL_WAIT = int(os.environ.get("HEAL_WAIT", "45"))
AUTO_HEAL = os.environ.get("AUTO_HEAL", "true").lower() == "true"

DOMAIN_LABEL = re.compile(r"^npm\.proxy(\.\d+)?\.domain$")
CHECK_FAILED = "__check_failed__"


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=60)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
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
        # NPM answers 400 (not 401) to expired bearer tokens
        if e.code in (400, 401, 403) and retry:
            _npm_token = None
            return npm_api(path, retry=False)
        raise


def snapshot():
    """dict of missing domain -> container name, from labels vs enabled NPM hosts."""
    expected = {}
    for c in docker_api("GET", "/containers/json"):
        for key, value in (c.get("Labels") or {}).items():
            if DOMAIN_LABEL.match(key):
                for d in re.split(r"[,\s]+", value.strip()):
                    if d:
                        expected[d.lower()] = c["Names"][0].lstrip("/")
    actual = {
        d.lower()
        for h in npm_api("/api/nginx/proxy-hosts")
        if h.get("enabled")
        for d in h.get("domain_names", [])
    }
    missing = {d: c for d, c in expected.items() if d not in actual}
    return missing, len(expected)


def notify_discord(title, description, color):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [{"title": title, "description": description[:4000], "color": color}]}
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        # Discord sits behind Cloudflare, which rejects urllib's default UA
        headers={"Content-Type": "application/json", "User-Agent": "npm-sync-guard/1.0"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"discord notify failed: {e}")


def fmt(missing):
    return "\n".join(f"- `{d}` ({c})" for d, c in sorted(missing.items()))


def check_cycle(prev_state):
    missing, total = snapshot()
    if missing:
        log(f"possible drift, re-checking in {GRACE}s: {sorted(missing)}")
        time.sleep(GRACE)
        missing, total = snapshot()

    if missing and AUTO_HEAL and set(missing) != prev_state:
        log(f"drift confirmed, restarting {SYNC_CONTAINER}: {sorted(missing)}")
        try:
            docker_api("POST", f"/containers/{SYNC_CONTAINER}/restart?t=10")
        except Exception as e:
            log(f"restart failed: {e}")
        time.sleep(HEAL_WAIT)
        before = missing
        missing, total = snapshot()
        healed = {d: c for d, c in before.items() if d not in missing}
        if healed and not missing:
            log(f"auto-healed: {sorted(healed)}")
            notify_discord(
                "\U0001f527 NPM sync drift auto-healed",
                f"Proxy hosts were missing and have been recreated by restarting "
                f"`{SYNC_CONTAINER}`:\n{fmt(healed)}",
                0xE67E22,
            )
            return set()
        if healed:
            log(f"partially healed: {sorted(healed)}")

    if missing:
        log(f"missing from NPM: {sorted(missing)}")
        if set(missing) != prev_state:
            notify_discord(
                "\U0001f6a8 NPM proxy hosts missing",
                f"These domains have no proxy host in NPM (auto-heal "
                f"{'failed' if AUTO_HEAL else 'disabled'}):\n{fmt(missing)}",
                0xE74C3C,
            )
        return set(missing)

    log(f"in sync ({total} domains)")
    if prev_state:
        notify_discord(
            "✅ NPM sync recovered",
            "All labeled domains are registered in NPM again.",
            0x2ECC71,
        )
    return set()


def main():
    log(
        f"npm-sync-guard starting: npm={NPM_URL}, interval={CHECK_INTERVAL}s, "
        f"auto_heal={AUTO_HEAL}, discord={'on' if DISCORD_WEBHOOK_URL else 'off'}"
    )
    prev_state = set()
    once = os.environ.get("ONCE", "") == "1"
    while True:
        try:
            prev_state = check_cycle(prev_state)
        except Exception as e:
            log(f"check failed: {e}")
            if prev_state != {CHECK_FAILED}:
                notify_discord(
                    "\U0001f6a8 NPM sync check failed",
                    f"npm-sync-guard could not complete its check:\n```{e}```",
                    0xE74C3C,
                )
            prev_state = {CHECK_FAILED}
        if once:
            break
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
