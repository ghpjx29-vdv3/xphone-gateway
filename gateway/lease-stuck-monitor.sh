#!/usr/bin/env bash
# XPhone lease-stuck monitor.
#
# Detects worker crashes that leave events unprocessed: the gateway writes
# each event to ~/xphone/inbox/<event-id>.json and the worker deletes it
# after handling. If the oldest unprocessed file is older than STUCK_AFTER
# minutes, the worker likely crashed mid-processing (or a lease got stuck
# in `running` server-side), which blocks sequential ack of later events.
#
# Alert-only: this script never auto-recovers. Finishing someone else's
# lease automatically risks duplicate submissions. On alert, follow the
# manual recovery flow (start_event -> finish -> ack in cursor order).
#
# Intended to run from a runtime-managed cron every 30 minutes.
# Respects ~/xphone/gateway/PAUSED (user manual maintenance switch).
set -uo pipefail

GW_DIR="$HOME/xphone/gateway"
INBOX="$HOME/xphone/inbox"
PAUSED="$GW_DIR/PAUSED"
STUCK_AFTER_MIN="${1:-30}"

# User paused maintenance: skip.
[ -f "$PAUSED" ] && exit 0

# Gateway itself not running is the watchdog's job, not ours.
pgrep -f "[g]ateway.py" > /dev/null 2>&1 || exit 0

[ -d "$INBOX" ] || exit 0

now=$(date +%s)
stuck=()
for f in "$INBOX"/*.json; do
  [ -e "$f" ] || continue
  mtime=$(stat -c %Y "$f")
  age_min=$(( (now - mtime) / 60 ))
  if [ "$age_min" -ge "$STUCK_AFTER_MIN" ]; then
    stuck+=("$(basename "$f") (${age_min}min)")
  fi
done

if [ "${#stuck[@]}" -gt 0 ]; then
  echo "STUCK: ${stuck[*]}"
  exit 1
fi

exit 0
