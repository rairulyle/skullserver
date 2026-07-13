#!/bin/bash
# thrnz PORT_SCRIPT hook: push the PIA-forwarded port into qBittorrent.
# Runs inside the wireguard container, which shares qBittorrent's network
# namespace, so qBittorrent is reachable on localhost with WebUI auth bypassed.
# $1 = the PIA-forwarded port.
set -uo pipefail
port="$1"
qbt="http://localhost:8085"
log() { echo "$(date '+%F %T') [sync-port] $*"; }

if curl -fsS --retry 15 --retry-delay 4 --retry-all-errors --max-time 20 \
     --data "json={\"listen_port\":${port}}" \
     "${qbt}/api/v2/app/setPreferences" >/dev/null; then
  log "qBittorrent listen_port set to ${port}"
else
  log "ERROR: failed to set qBittorrent listen_port to ${port}"
  exit 1
fi
