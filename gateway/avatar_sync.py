#!/usr/bin/env python3
"""
Agent avatar sync: push Muse's avatar (image + state videos) to the XPhone
Worker so clients can load it as the personal agent's avatar.

Flow:
  1. Read local avatar assets from ~/workspace/avatars/ (via avatar.get metadata).
  2. Base64-encode image + videos.
  3. Call MCP tool `xphone.set_agent_profile` (Worker-side, to be implemented)
     with name "Muse" and the avatar payload.
  4. Worker persists to durable storage (R2) and serves to clients.

Run on gateway startup and whenever the avatar changes:
    ~/xphone/gateway/.venv/bin/python ~/xphone/gateway/avatar_sync.py

Requires the MCP client at ~/workspace/custom-connectors/custom.xphone-agent-worker/bin/client.py
with Secure Vault token configured.
"""
import base64
import json
import os
import subprocess
import sys

HOME = os.environ.get("HOME", "/home/hatch")
AVATAR_DIR = os.path.join(HOME, "workspace/avatars")
MCP_CLIENT = os.path.join(
    HOME, "workspace/custom-connectors/custom.xphone-agent-worker/bin/client.py"
)

# Maps XPhone agent states to filename keywords in the avatar dir.
# Current naming: {stem}-{state}-attempt-{hash}.mp4 (idle has no state part).
STATE_KEYWORDS = {
    "idle": ["attempt-44dd26864269"],
    "working": ["working"],
    "making_something": ["making_something"],
    "milestone_level_up": ["milestone_level_up"],
}

AGENT_NAME = "Muse"


def find_latest_avatar():
    """Find the most recent avatar image in the avatars dir."""
    candidates = []
    for f in os.listdir(AVATAR_DIR):
        if not f.endswith(".webp"):
            continue
        if f.startswith("."):
            continue
        # Skip derivative frames (dance frames etc.); want the base avatar.
        if "dance-frame" in f or "frame-" in f:
            continue
        p = os.path.join(AVATAR_DIR, f)
        candidates.append((os.path.getmtime(p), p))
    if not candidates:
        raise FileNotFoundError(f"No avatar webp found in {AVATAR_DIR}")
    candidates.sort(reverse=True)
    return candidates[0][1]


def b64_file(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def build_payload(image_path):
    """Build the xphone.set_agent_profile payload (flat base64 strings)."""
    stem = os.path.splitext(os.path.basename(image_path))[0]
    videos = {}
    for state, keywords in STATE_KEYWORDS.items():
        for f in sorted(os.listdir(AVATAR_DIR)):
            if not f.endswith(".mp4"):
                continue
            if not f.startswith(stem):
                continue
            if all(kw in f for kw in keywords):
                videos[state] = b64_file(os.path.join(AVATAR_DIR, f))
                break

    return {
        "name": AGENT_NAME,
        "avatarImage": b64_file(image_path),
        "videos": videos,
    }


def call_mcp(tool, arguments):
    args_file = "/tmp/avatar_sync_args.json"
    with open(args_file, "w") as f:
        json.dump(arguments, f)
    try:
        out = subprocess.run(
            [sys.executable, MCP_CLIENT, "call-tool",
             "--name", tool, "--arguments-file", args_file],
            capture_output=True, text=True, timeout=120,
        )
        return out.stdout.strip()
    finally:
        if os.path.exists(args_file):
            os.remove(args_file)


def main():
    image_path = find_latest_avatar()
    print(f"Avatar image: {image_path}", file=sys.stderr)
    payload = build_payload(image_path)
    print(f"States: {list(payload['videos'].keys())}", file=sys.stderr)
    total_kb = (len(payload["avatarImage"])
                + sum(len(v) for v in payload["videos"].values())) * 3 // 4 // 1024
    print(f"Payload ~{total_kb} KB", file=sys.stderr)

    # TODO: Worker must implement `xphone.set_agent_profile`.
    # Until then this will return "Request rejected" / unknown tool.
    result = call_mcp("xphone.set_agent_profile", payload)
    print(result)


if __name__ == "__main__":
    main()
