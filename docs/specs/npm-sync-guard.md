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
whose startup rescan recreates anything missing (verified working). State changes (drift,
heal, recovery, check failure) are announced directly to a Discord webhook. Silent while
healthy; there is deliberately no heartbeat channel.

```
docker.sock labels ──▶ guard (compare) ──▶ NPM API proxy hosts
                          │
                          ├─ drift ──▶ restart npm-docker-sync ──▶ re-verify
                          └─ state change ─▶ Discord webhook
```

## 3. Check cycle

1. expected — running containers via docker socket; collect values of labels matching
   `^npm\.proxy(\.\d+)?\.domain$`; split values on commas/whitespace; lowercase.
2. actual — all `domain_names` of ENABLED proxy hosts from NPM.
3. missing = expected − actual. Extra hosts in NPM are ignored (manual entries exist).
4. If missing: wait GRACE seconds, recompute (avoids false positives mid-event).
5. If still missing, AUTO_HEAL is on, AND the missing set changed since the previous cycle:
   restart SYNC_CONTAINER, wait HEAL_WAIT seconds, recompute.
6. Notify Discord ONLY on state changes (never per-cycle, so no spam):
   - 🔧 "NPM sync drift auto-healed" + healed domain list (orange)
   - 🚨 "NPM proxy hosts missing" + missing list, when heal failed/disabled (red)
   - ✅ "NPM sync recovered", when a previously-bad state clears (green)
   - 🚨 "NPM sync check failed", once per failure episode, e.g. NPM API down (red)
7. Sleep CHECK_INTERVAL, repeat. Log one timestamped line per cycle (flush=True).

Design rule: heal and notify only when the missing set CHANGED since the previous cycle — a
persistent failure alerts once instead of restart-looping the sync or spamming Discord.

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

### Discord webhook
- POST {DISCORD_WEBHOOK_URL} with JSON {"embeds": [{"title", "description", "color"}]}.
- MUST send a custom User-Agent header — Discord sits behind Cloudflare, which 403s
  python-urllib's default UA (verified live).
- Empty DISCORD_WEBHOOK_URL → skip silently.
- Trade-off (accepted): no heartbeat means a dead guard is indistinguishable from a healthy
  quiet one.

## 5. Configuration (env vars)

| var            | default         | purpose                                        |
|----------------|-----------------|------------------------------------------------|
| NPM_URL        | required        | NPM base URL, e.g. http://192.168.0.10:81      |
| NPM_EMAIL      | required        | NPM admin login                                |
| NPM_PASSWORD   | required        | NPM admin password                             |
| DISCORD_WEBHOOK_URL | ""         | Discord webhook; empty disables notifications  |
| SYNC_CONTAINER | npm-docker-sync | container restarted on drift                   |
| CHECK_INTERVAL | 300             | seconds between checks                         |
| GRACE          | 30              | re-check delay before treating drift as real   |
| HEAL_WAIT      | 45              | wait after restarting sync before re-verifying |
| AUTO_HEAL      | true            | false = detect-and-report only                 |
| ONCE           | unset           | 1 = run one cycle and exit (manual testing)    |

## 6. Failure handling

- NPM API unreachable/erroring → 🚨 Discord notification once per episode, retry next cycle
  (doubles as an NPM-is-down monitor).
- Docker socket errors → same path.
- No exception may kill the main loop; catch broadly per cycle.
- Known behavior: npm-docker-sync exits fatally if NPM isn't ready when it starts; docker's
  restart policy brings it back and its startup rescan recovers everything. The GRACE re-check
  plus the changed-set heal rule tolerate this window without false alarms.

## 7. Deployment (stacks/nginx/)

Two files: npm-sync-guard.py and a service entry in docker-compose.yml. The webhook URL is
assembled from the existing DISCORD_ID/DISCORD_TOKEN env vars (same pair watchtower uses).
Repo convention: no comments in compose files.

```yaml
  npm-sync-guard:
    container_name: npm-sync-guard
    image: python:3-alpine
    command: python -u /npm-sync-guard.py
    environment:
      TZ: ${TZ}
      DISCORD_WEBHOOK_URL: https://discord.com/api/webhooks/${DISCORD_ID}/${DISCORD_TOKEN}
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

Setup: docker compose up -d. No external setup needed.

## 8. Acceptance tests (on server)

1. Healthy path — logs show "in sync (N domains)" each cycle; no Discord messages.
2. Drift + heal — stage a labeled container with the sync stopped (or delete a low-risk proxy
   host in the NPM UI). Within CHECK_INTERVAL + GRACE + HEAL_WAIT the host is recreated and a
   🔧 auto-healed embed arrives on Discord.
3. NPM outage — stop NPM briefly → one 🚨 check-failed embed; ✅ recovery embed after restart.

## 9. Out of scope

- Uptime Kuma integration — removed by decision 2026-07-18; Discord-only, no heartbeat.
- npm.stream.* (TCP/UDP stream) labels — none in use on this server.
- Multiple NPM instances, multi-host sync.

## 10. Validated facts from the live prototype (2026-07-16)

- Restarting npm-docker-sync full-rescans on startup and recreated 2 genuinely lost proxy hosts.
- All proxy hosts forward to host-IP:port, so container IP churn is not a factor.
- NPM tokens authenticate with the same NPM_EMAIL/NPM_PASSWORD values npm-docker-sync uses;
  read them from env as-is, never through shell interpolation (the password breaks shell quoting).
- The grace-recheck loop produced zero false positives against 30 labeled containers.
