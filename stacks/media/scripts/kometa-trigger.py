#!/usr/bin/env python3
"""Event-driven Kometa runs: polls Plex for new additions per library and
execs a scoped Kometa run after a quiet period. The daily scheduled run
(KOMETA_TIME) remains the backstop."""
import http.client
import json
import os
import socket
import time
import urllib.request

PLEX_URL = os.environ["PLEX_URL"].rstrip("/")
PLEX_TOKEN = os.environ["PLEX_TOKEN"]
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
KOMETA_CONTAINER = os.environ.get("KOMETA_CONTAINER", "kometa")
LIBRARIES = [x.strip() for x in os.environ.get("LIBRARIES", "Movies,TV Shows").split(",") if x.strip()]
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
QUIET_PERIOD = int(os.environ.get("QUIET_PERIOD", "600"))
BLACKOUT = os.environ.get("BLACKOUT", "01:50-03:30")
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"

TYPE_FOR_SECTION = {"movie": "1", "show": "4"}


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def plex_api(path):
    req = urllib.request.Request(
        f"{PLEX_URL}{path}",
        headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def resolve_sections():
    while True:
        try:
            dirs = plex_api("/library/sections")["MediaContainer"]["Directory"]
            by_title = {d["title"]: d for d in dirs}
            sections = {}
            for title in LIBRARIES:
                if title not in by_title:
                    log(f"FATAL: library '{title}' not found in Plex (have: {sorted(by_title)})")
                    raise SystemExit(1)
                d = by_title[title]
                sections[title] = (str(d["key"]), TYPE_FOR_SECTION[d["type"]])
            log(f"resolved sections: {{{', '.join(f'{t}: {s[0]}' for t, s in sections.items())}}}")
            return sections
        except SystemExit:
            raise
        except Exception as e:
            log(f"cannot reach Plex for section resolution ({e}); retrying in {POLL_INTERVAL}s")
            time.sleep(POLL_INTERVAL)


def newest_added(key, item_type):
    data = plex_api(
        f"/library/sections/{key}/all?type={item_type}"
        f"&sort=addedAt:desc&X-Plex-Container-Start=0&X-Plex-Container-Size=1"
    )
    meta = data["MediaContainer"].get("Metadata", [])
    return int(meta[0].get("addedAt", 0)) if meta else 0


def load_state():
    try:
        with open(STATE_FILE) as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}
    except Exception as e:
        log(f"state file unreadable ({e}); reinitializing")
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE_FILE + ".tmp", STATE_FILE)


def in_blackout():
    try:
        start_s, end_s = BLACKOUT.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
    except ValueError:
        return False
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end


def notify_discord(title, description, color):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {"embeds": [{"title": title, "description": description[:4000], "color": color}]}
    req = urllib.request.Request(
        DISCORD_WEBHOOK_URL,
        data=json.dumps(payload).encode(),
        # Discord sits behind Cloudflare, which rejects urllib's default UA
        headers={"Content-Type": "application/json", "User-Agent": "kometa-trigger/1.0"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:
        log(f"discord notify failed: {e}")


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=60)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")


def docker_api(method, path, body=None):
    conn = DockerConnection()
    try:
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"docker {method} {path} -> {resp.status}: {raw[:200]}")
        return json.loads(raw) if raw else None
    finally:
        conn.close()


def kometa_run(scope, timeout=3600):
    exec_id = docker_api(
        "POST",
        f"/containers/{KOMETA_CONTAINER}/exec",
        {
            "AttachStdout": False,
            "AttachStderr": False,
            "Cmd": [
                "python3", "/app/kometa/kometa.py",
                "--config", "/config/config.yml",
                "--run", "--run-libraries", scope,
            ],
        },
    )["Id"]
    docker_api("POST", f"/exec/{exec_id}/start", {"Detach": True})
    waited = 0
    while waited < timeout:
        time.sleep(15)
        waited += 15
        info = docker_api("GET", f"/exec/{exec_id}/json")
        if not info.get("Running"):
            return info.get("ExitCode")
    raise RuntimeError(f"kometa run still going after {timeout}s")


def trigger_run(libraries):
    scope = "|".join(libraries)
    if DRY_RUN:
        log(f"DRY RUN: would exec kometa for {scope}")
        return
    log(f"triggering kometa run for {scope}")
    try:
        code = kometa_run(scope)
    except Exception as e:
        log(f"triggered run failed to execute: {e}")
        notify_discord(
            "\U0001f6a8 Kometa triggered run failed",
            f"Could not exec run for `{scope}`:\n```{e}```",
            0xE74C3C,
        )
        return
    if code == 0:
        log(f"triggered run for {scope} finished ok")
    else:
        log(f"triggered run for {scope} exited {code}")
        notify_discord(
            "\U0001f6a8 Kometa triggered run failed",
            f"Run for `{scope}` exited with code {code}.",
            0xE74C3C,
        )


def main():
    log(
        f"kometa-trigger starting: plex={PLEX_URL}, libraries={LIBRARIES}, "
        f"poll={POLL_INTERVAL}s, quiet={QUIET_PERIOD}s, blackout={BLACKOUT}, dry={DRY_RUN}"
    )
    sections = resolve_sections()
    state = load_state()
    if not state:
        for title, (key, typ) in sections.items():
            state[title] = newest_added(key, typ)
        save_state(state)
        log(f"initialized watermarks without triggering: {state}")
    for title in sections:
        state.setdefault(title, 0)
    seen = dict(state)
    pending = {}
    once = os.environ.get("ONCE", "") == "1"
    while True:
        for title, (key, typ) in sections.items():
            try:
                newest = newest_added(key, typ)
            except Exception as e:
                log(f"poll failed for {title}: {e}")
                continue
            if newest > seen[title]:
                seen[title] = newest
                pending[title] = time.monotonic()
                log(f"new additions in {title} (addedAt {newest}); quiet timer reset")
        ripe = sorted(t for t, ts in pending.items() if time.monotonic() - ts >= QUIET_PERIOD)
        if ripe:
            if in_blackout():
                log(f"holding {ripe} during blackout {BLACKOUT}")
            else:
                for t in ripe:
                    state[t] = seen[t]
                    pending.pop(t)
                save_state(state)
                trigger_run(ripe)
        if once:
            break
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
