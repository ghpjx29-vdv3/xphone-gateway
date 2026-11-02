#!/usr/bin/env bash
# Wake one retained event at a time in server cursor order. An unfinished file
# is retried after its lease can expire, including after a worker crash.
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

INBOX="$HOME/xphone/inbox"
PAUSED="$HOME/xphone/gateway/PAUSED"
RETRY_SECONDS=120
mkdir -p "$INBOX"
[ -f "$PAUSED" ] && { silent "xphone inbox paused"; exit 0; }

file=$(python3 - "$INBOX" <<'PY'
import json
import pathlib
import sys

paths = list(pathlib.Path(sys.argv[1]).glob('*.json'))
if paths:
    print(min(paths, key=lambda path: json.loads(path.read_text(encoding='utf-8'))['cursor']).name)
PY
)

if [ -z "$file" ]; then
  silent "no xphone inbox files"
  exit 0
fi

# The file may have been deleted by a worker between glob and wake.
# Check existence before doing anything else.
if [ ! -f "$INBOX/$file" ]; then
  silent "xphone inbox file already gone"
  exit 0
fi

now=$(date +%s)
state=$(hook_state_get)

# Deduplicate: skip if this file was woken within RETRY_SECONDS.
# State format on disk is {"seen": [...]} (plain filename list, no timestamps),
# maintained by the hook runtime. We track wake timestamps in a separate
# "wake_ts" dict so we don't clobber the runtime's format.
should_skip=$(python3 - "$now" "$RETRY_SECONDS" "$file" <<'PY'
import json
import sys

now = int(sys.argv[1])
retry = int(sys.argv[2])
file = sys.argv[3]
try:
    state = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    state = {}

wake_ts = state.get("wake_ts", {})
if not isinstance(wake_ts, dict):
    wake_ts = {}
last = wake_ts.get(file, 0)
try:
    last = int(last)
except (TypeError, ValueError):
    last = 0

# Prune entries older than retry window to keep state small.
wake_ts = {k: v for k, v in wake_ts.items() if isinstance(v, int) and now - v < retry}

if last and now - last < retry:
    print("skip")
else:
    print("go")
PY
<<< "$state")

if [ "$should_skip" = "skip" ]; then
  silent "xphone inbox awaiting worker"
  exit 0
fi

# Re-check existence right before wake (worker may have deleted it).
if [ ! -f "$INBOX/$file" ]; then
  silent "xphone inbox file already gone"
  exit 0
fi

payload=$(python3 -c 'import json,sys; print(json.dumps({"files": [sys.argv[1]]}))' "$file")
log "xphone inbox wake" '{"new":1}'
wake "xphone-inbox-events" "$payload"

# Record wake timestamp, preserving the runtime's existing state keys.
new_state=$(python3 - "$now" "$file" <<'PY'
import json
import sys

now = int(sys.argv[1])
file = sys.argv[2]
try:
    state = json.loads(sys.stdin.read() or "{}")
except json.JSONDecodeError:
    state = {}
if not isinstance(state, dict):
    state = {}

wake_ts = state.get("wake_ts", {})
if not isinstance(wake_ts, dict):
    wake_ts = {}
# Prune old entries.
wake_ts = {k: v for k, v in wake_ts.items() if isinstance(v, int) and now - v < 120}
wake_ts[file] = now
state["wake_ts"] = wake_ts
# Keep legacy keys for compatibility with older readers.
state["lastFile"] = file
state["lastWake"] = now
print(json.dumps(state))
PY
<<< "$state")
hook_state_set "$new_state"
