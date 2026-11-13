#!/usr/bin/env bash
# XPhone Gateway watchdog (deploy.py edition).
# The VM can be replaced at any time (wiping /usr/local, killing processes).
# This script recovers the NEW gateway (transport + muse, deployed from the
# source tree by deploy.py) when it is not running. Intended to run from a
# runtime-managed cron every 10 minutes.
#
# - It does NOT touch the old systemd gateway (xphone-gateway.service).
#   ~/xphone/gateway/PAUSED exists to keep that old unit from resurrecting
#   and is intentionally ignored here.
# - It deploys the current on-disk checkout as-is; it never pulls new code.
#   Deploying a new commit is a deliberate step, done by hand.
# - To pause recovery, disable the cron job; there is no PAUSED-style flag
#   for the new gateway.
set -uo pipefail

REPO="$HOME/workspace/xphone-gateway-upstream"
DEPLOY="$REPO/gateway/deploy.py"
VENV_PY="$HOME/.config/xphone/venv/bin/python"
GW_DIR="$HOME/xphone/gateway"
LOG="$GW_DIR/watchdog.log"
LOCK="$GW_DIR/watchdog.lock"
MUSE_LOG="$HOME/.config/xphone/setup/muse.log"
GO_TARBALL="https://go.dev/dl/go1.23.4.linux-amd64.tar.gz"
GO_BIN="/usr/local/go/bin/go"
MAX_DEPLOY_ATTEMPTS=4
WS_WAIT_SECONDS=120

log() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }

# One watchdog at a time; a still-running previous run means recovery is
# already in progress, so exit quietly.
exec 9>"$LOCK"
flock -n 9 || exit 0

# 1. Gateway already running? Both processes must be present.
if pgrep -f "[t]unnel_runtime.py run" >/dev/null 2>&1 \
  && pgrep -f "[d]eploy.py _muse" >/dev/null 2>&1; then
  exit 0
fi

log "watchdog: gateway missing, recovering via deploy.py"

# 2. Go is wiped on VM replacement; reinstall if the binary is gone.
if [ ! -x "$GO_BIN" ]; then
  if curl -fsSL -o /tmp/go1.23.4.linux-amd64.tar.gz "$GO_TARBALL" \
    && tar -C /usr/local -xzf /tmp/go1.23.4.linux-amd64.tar.gz; then
    log "watchdog: reinstalled Go 1.23.4"
  else
    log "watchdog: FAILED to reinstall Go"
    exit 1
  fi
fi
export PATH="/usr/local/go/bin:$PATH"

# 3. Deploy. wait_for_relay's health check is flaky (IncompleteRead /
# RemoteDisconnected are not caught by deploy.py), so retry a few times
# with a pause between attempts.
muse_lines_before=0
[ -f "$MUSE_LOG" ] && muse_lines_before=$(wc -l < "$MUSE_LOG")
attempt=0
deploy_ok=0
while [ "$attempt" -lt "$MAX_DEPLOY_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  if "$VENV_PY" "$DEPLOY" --provider muse >>"$LOG" 2>&1; then
    deploy_ok=1
    break
  fi
  log "watchdog: deploy attempt $attempt failed, retrying in 30s"
  sleep 30
done
if [ "$deploy_ok" -ne 1 ]; then
  log "watchdog: FAILED to deploy after $MAX_DEPLOY_ATTEMPTS attempts"
  exit 1
fi

# 4. Verify: both processes up, and muse.log gained a "websocket connected"
# line after the deploy (a fresh connect, not a stale line from before).
# (muse.log is append-only, but reset the baseline if it ever shrinks.)
deadline=$(( $(date +%s) + WS_WAIT_SECONDS ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if [ -f "$MUSE_LOG" ]; then
    cur_lines=$(wc -l < "$MUSE_LOG")
    if [ "$cur_lines" -lt "$muse_lines_before" ]; then
      muse_lines_before=$cur_lines
    fi
    if pgrep -f "[t]unnel_runtime.py run" >/dev/null 2>&1 \
      && pgrep -f "[d]eploy.py _muse" >/dev/null 2>&1 \
      && tail -n +"$((muse_lines_before + 1))" "$MUSE_LOG" 2>/dev/null | grep -q "websocket connected"; then
      log "watchdog: gateway recovered OK"
      exit 0
    fi
  fi
  sleep 10
done

log "watchdog: FAILED verification after deploy (processes or WS connect missing)"
exit 1
