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

state=$(hook_state_get)
read -r last_file last_wake < <(python3 -c 'import json,sys; s=json.load(sys.stdin); print(s.get("lastFile", "-"), s.get("lastWake", 0))' <<< "$state")
now=$(date +%s)
if [ "$file" = "$last_file" ] && [ $((now - last_wake)) -lt "$RETRY_SECONDS" ]; then
  silent "xphone inbox awaiting worker"
  exit 0
fi

payload=$(python3 -c 'import json,sys; print(json.dumps({"files": [sys.argv[1]]}))' "$file")
log "xphone inbox wake" '{"new":1}'
wake "xphone-inbox-events" "$payload"
hook_state_set "$(python3 -c 'import json,sys; print(json.dumps({"lastFile": sys.argv[1], "lastWake": int(sys.argv[2])}))' "$file" "$now")"
