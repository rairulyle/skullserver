# npm-sync-guard — design spec

Personal watchdog for `npm-docker-sync` on the skullserver Unraid host.
Python, stdlib only, single file (~200 LOC), runs as a sidecar container in the nginx stack.

## 1. Problem

`npm-docker-sync` (ghcr.io/redth/npm-docker-sync) keeps Nginx Proxy Manager (NPM) proxy hosts
in sync with `npm.proxy.*` docker labels, but it is purely event-driven: a container stop
deletes the proxy host, a start recreates it. If the start event is missed (NPM briefly down,
the sync container itself restarting during an update), the proxy host stays deleted and the
URL is dead until someone restarts the service manually.

Confirmed on the live server (2026-07-16): two labeled services (ynab-mcp, znc) were running
with no proxy host in NPM.

## 2. Solution

A polling sidecar. Every CHECK_INTERVAL seconds it compares expected domains (labels on
running containers) against actual domains (NPM API). On drift it restarts npm-docker-sync,
whose startup rescan recreates anything missing (verified working). Every cycle reports to an
Uptime Kuma push monitor; Kuma owns alerting (Discord) and also alarms if the guard itself
dies, because the heartbeat stops.

```
docker.sock labels ──▶ guard (compare) ──▶ NPM API proxy hosts
                          │
                          ├─ drift ──▶ restart npm-docker-sync ──▶ re-verify
                          └─ result ─▶ Uptime Kuma push ──▶ Discord
```

## 3. Check cycle

1. expected — running containers via docker socket; collect values of labels matching
   `^npm\.proxy(\.\d+)?\.domain$`; split values on commas/whitespace; lowercase.
2. actual — all `domain_names` of ENABLED proxy hosts from NPM.
3. missing = expected − actual. Extra hosts in NPM are ignored (manual entries exist).
4. If missing: wait GRACE seconds, recompute (avoids false positives mid-event).
5. If still missing, AUTO_HEAL is on, AND the missing set changed since the previous cycle:
   restart SYNC_CONTAINER, wait HEAL_WAIT seconds, recompute.
6. Push to Kuma:
   - up   "in sync (N domains)"
   - up   "auto-healed: <domains>"
   - down "missing: <domains>"
7. Sleep CHECK_INTERVAL, repeat. Log one timestamped line per cycle (flush=True).

Design rule: heal only when the missing set CHANGED since the previous cycle — a persistent
failure alerts once through Kuma instead of restart-looping the sync container every cycle.

## 4. External interfaces

### Docker Engine API (unix socket /var/run/docker.sock)
- GET /containers/json → running containers with Labels map.
- POST /containers/{SYNC_CONTAINER}/restart?t=10 → the heal action.
- stdlib only: subclass http.client.HTTPConnection, override connect() with an AF_UNIX socket.

### Nginx Proxy Manager API
- POST {NPM_URL}/api/tokens with {"identity": NPM_EMAIL, "secret": NPM_PASSWORD} → {token}.
- GET {NPM_URL}/api/nginx/proxy-hosts with "Authorization: Bearer <token>" —
  each host has domain_names[] and enabled.
- Cache the token across cycles; on 401/403 re-authenticate once and retry.

### Uptime Kuma push monitor
- GET {KUMA_PUSH_URL}?status=up|down&msg=<url-encoded, max 250 chars>
- Empty KUMA_PUSH_URL → skip silently.
- Set the monitor's heartbeat interval to ~2× CHECK_INTERVAL (660s for 300s checks) so a dead
  guard raises a missed-heartbeat alert.

## 5. Configuration (env vars)

| var            | default         | purpose                                        |
|----------------|-----------------|------------------------------------------------|
| NPM_URL        | required        | NPM base URL, e.g. http://192.168.0.10:81      |
| NPM_EMAIL      | required        | NPM admin login                                |
| NPM_PASSWORD   | required        | NPM admin password                             |
| KUMA_PUSH_URL  | ""              | Kuma push URL; empty disables reporting        |
| SYNC_CONTAINER | npm-docker-sync | container restarted on drift                   |
| CHECK_INTERVAL | 300             | seconds between checks                         |
| GRACE          | 30              | re-check delay before treating drift as real   |
| HEAL_WAIT      | 45              | wait after restarting sync before re-verifying |
| AUTO_HEAL      | true            | false = detect-and-report only                 |
| ONCE           | unset           | 1 = run one cycle and exit (manual testing)    |

## 6. Failure handling

- NPM API unreachable/erroring → push down "check failed: …", retry next cycle
  (doubles as an NPM-is-down monitor).
- Docker socket errors → log and push down.
- No exception may kill the main loop; catch broadly per cycle.
- Known behavior: npm-docker-sync exits fatally if NPM isn't ready when it starts; docker's
  restart policy brings it back and its startup rescan recovers everything. The GRACE re-check
  plus the changed-set heal rule tolerate this window without false alarms.

## 7. Deployment (stacks/nginx/)

Two files: npm-sync-guard.py and a service entry in docker-compose.yml. New env var
NPM_SYNC_GUARD_KUMA_PUSH_URL goes in .env, .env.local, and .env.example.
Repo convention: no comments in compose files.

```yaml
  npm-sync-guard:
    container_name: npm-sync-guard
    image: python:3-alpine
    command: python -u /npm-sync-guard.py
    environment:
      TZ: ${TZ}
      KUMA_PUSH_URL: ${NPM_SYNC_GUARD_KUMA_PUSH_URL:-}
      NPM_EMAIL: ${NPM_USERNAME}
      NPM_PASSWORD: ${DEFAULT_APP_PASSWORD}
      NPM_URL: http://${IP_ADDRESS}:81
    volumes:
      - ./npm-sync-guard.py:/npm-sync-guard.py:ro
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      - nginx-proxy-manager
    restart: unless-stopped
    labels:
      net.unraid.docker.icon: https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/png/nginx-proxy-manager.png
```

Setup order: create the Kuma push monitor (type Push, heartbeat 660s, attach the Discord
notification) → paste its URL into NPM_SYNC_GUARD_KUMA_PUSH_URL → docker compose up -d.

## 8. Acceptance tests (on server)

1. Healthy path — logs show "in sync (N domains)" each cycle; Kuma monitor is green.
2. Drift + heal — delete one low-risk proxy host in the NPM UI (e.g. speedtest). Within
   CHECK_INTERVAL + GRACE + HEAL_WAIT the host is recreated, its URL works, and Kuma logs an
   up-beat with "auto-healed: …".
3. Guard death — docker stop npm-sync-guard → Discord alert via Kuma after the missed
   heartbeat; docker start clears it.
4. NPM outage — stop NPM briefly → Kuma goes down with "check failed"; recovery is automatic.

## 9. Out of scope

- Direct Discord webhooks — Kuma owns all alerting.
- npm.stream.* (TCP/UDP stream) labels — none in use on this server.
- Multiple NPM instances, multi-host sync, creating the Kuma monitor via API.

## 10. Validated facts from the live prototype (2026-07-16)

- Restarting npm-docker-sync full-rescans on startup and recreated 2 genuinely lost proxy hosts.
- All proxy hosts forward to host-IP:port, so container IP churn is not a factor.
- NPM tokens authenticate with the same NPM_EMAIL/NPM_PASSWORD values npm-docker-sync uses;
  read them from env as-is, never through shell interpolation (the password breaks shell quoting).
- The grace-recheck loop produced zero false positives against 30 labeled containers.
