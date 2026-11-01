#!/usr/bin/env bash
# XPhone Gateway watchdog.
# The VM can be replaced at any time (wiping /etc, killing processes).
# This script re-installs the systemd unit from the persistent template
# under ~/xphone/gateway/ and restarts the gateway if it is not running.
# Intended to run from a runtime-managed cron every 10 minutes.
set -uo pipefail

GW_DIR="$HOME/xphone/gateway"
UNIT_SRC="$GW_DIR/xphone-gateway.service"
UNIT_DST="/etc/systemd/system/xphone-gateway.service"
LOG="$GW_DIR/watchdog.log"
HEALTH="$GW_DIR/health.json"
STALE_AFTER_SECONDS=180
PAUSED="$GW_DIR/PAUSED"

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
[ -f "$PAUSED" ] && exit 0

# 1. A live process must also be pulling the retained inbox successfully.
if pgrep -f "[g]ateway.py" > /dev/null 2>&1 && [ -f "$HEALTH" ]; then
  last_pull=$(stat -c %Y "$HEALTH")
  if [ $(( $(date +%s) - last_pull )) -lt "$STALE_AFTER_SECONDS" ]; then
    exit 0
  fi
fi

log "watchdog: gateway missing or inbox pull stale, recovering"

# 2. Unit file present? (lost on VM replacement)
if [ ! -f "$UNIT_DST" ]; then
  if [ -f "$UNIT_SRC" ]; then
    cp "$UNIT_SRC" "$UNIT_DST" || { log "watchdog: FAILED to copy unit file"; exit 1; }
    log "watchdog: reinstalled systemd unit from template"
  else
    log "watchdog: FAILED, unit template missing at $UNIT_SRC"
    exit 1
  fi
  systemctl daemon-reload || log "watchdog: daemon-reload failed"
fi

# 3. Ensure enabled + running
systemctl enable xphone-gateway.service > /dev/null 2>&1 || true
if systemctl restart xphone-gateway.service 2>&1; then
  sleep 8
  if pgrep -f "[g]ateway.py" > /dev/null 2>&1; then
    log "watchdog: gateway restarted OK"
    exit 0
  fi
fi

log "watchdog: FAILED to restart gateway"
exit 1
