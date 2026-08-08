#!/usr/bin/env python3
"""Watchdog for wireguard-pia and the containers sharing its network
namespace: restarts the VPN when it sticks unhealthy, starts dependents that
never launched, restarts dependents left on a dead netns after a VPN restart,
and reports actions to Discord."""
import calendar
import http.client
import json
import os
import re
import socket
import time
import urllib.request

VPN_CONTAINER = os.environ.get("VPN_CONTAINER", "wireguard-pia")
DEPENDENTS = os.environ.get("DEPENDENTS", "qbittorrent,slskd").split(",")
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL", "60"))
GRACE = int(os.environ.get("GRACE", "120"))
FAIL_NOTIFY_THRESHOLD = int(os.environ.get("FAIL_NOTIFY_THRESHOLD", "3"))


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


class DockerConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("docker", timeout=200)

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


def notify(msg):
    log(msg)
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        req = urllib.request.Request(
            DISCORD_WEBHOOK_URL,
            data=json.dumps({"content": f"**vpn-guard**: {msg}"}).encode(),
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15).close()
    except Exception as e:
        log(f"discord notify failed: {e}")


def started_epoch(inspect):
    # docker reports UTC RFC3339 with nanoseconds; second precision is plenty
    stamp = re.sub(r"[.].*", "", inspect["State"]["StartedAt"])
    return calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))


def check():
    """Returns a list of actions taken; raises when healing fails."""
    vpn = docker_api("GET", f"/containers/{VPN_CONTAINER}/json")
    state = vpn["State"]["Status"]
    health = vpn["State"].get("Health", {}).get("Status", "none")
    vpn_started = started_epoch(vpn)

    if state != "running":
        log(f"{VPN_CONTAINER} state={state}; starting")
        docker_api("POST", f"/containers/{VPN_CONTAINER}/start")
        return [f"started {VPN_CONTAINER} (was {state})"]

    if health != "healthy":
        # give a freshly (re)started container time to come up before acting
        if time.time() - vpn_started < GRACE:
            return []
        log(f"{VPN_CONTAINER} health={health} after grace period; restarting")
        docker_api("POST", f"/containers/{VPN_CONTAINER}/restart?t=30")
        # dependents are handled next round, once the VPN passes its healthcheck
        return [f"restarted {VPN_CONTAINER} (health={health})"]

    actions = []
    for name in DEPENDENTS:
        name = name.strip()
        dep = docker_api("GET", f"/containers/{name}/json")
        if dep["State"]["Status"] != "running":
            log(f"{name} state={dep['State']['Status']}; starting")
            docker_api("POST", f"/containers/{name}/start")
            actions.append(f"started {name} (was {dep['State']['Status']})")
        elif started_epoch(dep) < vpn_started:
            # a dependent started before the VPN holds the VPN's previous,
            # now-dead network namespace - localhost healthchecks won't notice
            log(f"{name} predates current {VPN_CONTAINER} netns; restarting")
            docker_api("POST", f"/containers/{name}/restart?t=30")
            actions.append(f"restarted {name} (stale netns)")
    return actions


def main():
    log(f"watching {VPN_CONTAINER} + {DEPENDENTS} every {CHECK_INTERVAL}s")
    failures = 0
    while True:
        try:
            for action in check():
                notify(action)
            failures = 0
        except Exception as e:
            failures += 1
            log(f"check failed ({failures}): {e}")
            if failures == FAIL_NOTIFY_THRESHOLD:
                notify(f"healing {VPN_CONTAINER} keeps failing: {e}")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
