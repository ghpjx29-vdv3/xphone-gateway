#!/usr/bin/env python3
"""Wake the Muse personal agent for XPhone maintenance jobs on the VM."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path


BASE = Path.home() / "xphone"
GATEWAY = BASE / "gateway"
CONTENT = BASE / "content"
PROMPTS = Path(__file__).parent / "prompts"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def cli(*args: str) -> dict:
    env = os.environ.copy()
    proxy_file = GATEWAY / "proxy.env"
    if proxy_file.exists():
        for line in proxy_file.read_text(encoding="utf-8").splitlines():
            if line and not line.startswith("#"):
                name, value = line.split("=", 1)
                env.setdefault(name, value)
    proxy = env.get("wss_proxy") or env.get("https_proxy") or env.get("HTTPS_PROXY")
    if proxy:
        env["wss_proxy"] = proxy
    binary = env.get("XPHONE_MUSE_CLI") or str(Path.home() / ".local/bin/muse-cli")
    completed = subprocess.run([binary, *args], capture_output=True, text=True,
                               timeout=360, check=True, env=env)
    return json.loads(completed.stdout)


def wake(session_key: str, prompt: str) -> None:
    path = GATEWAY / "maintenance-sessions.json"
    sessions = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if session_key not in sessions:
        created = cli("session-start", "--title", f"XPhone 体验管理 {session_key}")
        sessions[session_key] = created.get("session_id") or created["id"]
        write_json(path, sessions)
    sent = cli("send", prompt, "--thread", sessions[session_key], "--wait", "300")
    if sent.get("sent") is not True:
        raise RuntimeError("Muse 未确认维护任务送达")


def prompt_for(job: str, scope: dict, now: datetime) -> str:
    pack_id, playthrough_id = scope["packId"], scope["playthroughId"]
    pack = CONTENT / pack_id
    memory = pack / playthrough_id / "memory"
    manifest = json.loads((pack / ".manifest.json").read_text(encoding="utf-8"))
    expiry = now + (timedelta(hours=1) if job == "experience" else timedelta(days=1))
    values = {
        "packId": pack_id, "playthroughId": playthrough_id,
        "ledgerPath": str(GATEWAY / "muse-memory.json"),
        "manifestPath": str(pack / ".manifest.json"),
        "docsDirectory": str(pack / "docs"),
        "summaryPath": str(memory / "summary.md"),
        "pinsPath": str(memory / "pins.md"),
        "healthPath": str(memory / "health.md"),
        "inspirationPath": str(memory / "inspiration.md"),
        "proposalPath": str(memory / "compaction-proposal.json"),
        "inspirationCity": manifest.get("inspirationCity") or "",
        "expiresAt": expiry.isoformat(),
    }
    template = (PROMPTS / f"maintenance-{job}.md").read_text(encoding="utf-8")
    return template + "\n\n本轮云电脑文件路径与范围：\n" + json.dumps(values, ensure_ascii=False, indent=2)


def apply_compaction(scope: dict, proposal_path: Path) -> int:
    if not proposal_path.exists():
        return 0
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    scope_key = f"{scope['packId']}:{scope['playthroughId']}"
    if proposal["scope"] != scope_key:
        raise ValueError("记忆整理范围与当前游玩不一致")
    memory_path = GATEWAY / "muse-memory.json"
    lock_path = memory_path.with_suffix(".lock")
    with lock_path.open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        try:
            data = json.loads(memory_path.read_text(encoding="utf-8"))
            current = data[scope_key]
            ledger = {(item["eventId"], item.get("step")): item for item in current["ledger"]}
            before = []
            for change in proposal.get("ledger", []):
                item = ledger[(change["eventId"], change.get("step"))]
                if item["summary"] != change["summary"]:
                    before.append(item.copy())
                    item["summary"] = change["summary"]
            surface_changed = 0
            for surface_id, summary in proposal.get("surfaces", {}).items():
                if current["surfaces"][surface_id] != summary:
                    current["surfaces"][surface_id] = summary
                    surface_changed += 1
            if not before and not surface_changed:
                return 0
            archive = GATEWAY / "memory-archive"
            archive.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            shutil.copy2(memory_path, archive / f"before-compaction-{stamp}.json")
            with (archive / f"{stamp}.jsonl").open("a", encoding="utf-8") as stream:
                for item in before:
                    stream.write(json.dumps({"scope": scope_key, "entry": item}, ensure_ascii=False) + "\n")
            write_json(memory_path, data)
            log = CONTENT / scope["packId"] / scope["playthroughId"] / "memory" / "compaction-log.jsonl"
            with log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": stamp, "ledgerChanged": len(before),
                                         "surfacesChanged": surface_changed,
                                         "charactersBefore": sum(len(item["summary"]) for item in before),
                                         "charactersAfter": sum(len(ledger[(item["eventId"], item.get("step"))]["summary"]) for item in before)},
                                        ensure_ascii=False) + "\n")
            return len(before)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("job", choices=("experience", "inspiration", "memory"))
    args = parser.parse_args()
    scope_file = GATEWAY / "scope.json"
    if not scope_file.exists() or (GATEWAY / "PAUSED").exists():
        return 0
    scope = json.loads(scope_file.read_text(encoding="utf-8")).get("scope")
    if not scope:
        return 0
    with (GATEWAY / "maintenance.lock").open("a+b") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        now = datetime.now(timezone.utc)
        proposal = CONTENT / scope["packId"] / scope["playthroughId"] / "memory" / "compaction-proposal.json"
        if args.job == "memory":
            proposal.unlink(missing_ok=True)
        wake(f"{scope['packId']}:{scope['playthroughId']}", prompt_for(args.job, scope, now))
        if args.job == "memory":
            apply_compaction(scope, proposal)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
