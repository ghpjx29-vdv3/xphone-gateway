"""Shared semantic memory for one XPhone playthrough."""

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

_thread_lock = threading.RLock()


@contextmanager
def memory_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _thread_lock, lock_path.open("a+b") as lock_file:
        if os.name != "nt":
            import fcntl
            fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name != "nt":
                fcntl.flock(lock_file, fcntl.LOCK_UN)


SURFACE_NAMES = {
    "agent": "个人代理", "os": "个人代理", "journal": "故事正文", "messages": "私聊",
    "maps": "地图", "socialmedia": "社交动态", "notes": "备忘录",
    "world": "世界", "characters": "人物", "photos": "相册", "system": "系统",
}


def surface_label(raw: str | None) -> str:
    if not raw:
        return "系统"
    parts = raw.split(".")
    base = parts[0]
    name = SURFACE_NAMES.get(base, base)
    if base == "messages" and len(parts) >= 3 and parts[1] == "dm":
        actor = parts[2].split(":")[0]
        return f"{name}（{actor}）"
    return name


class MuseMemory:
    def __init__(self, path: str):
        self.path = Path(path)

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def view(self, pack_id: str, playthrough_id: str, route: dict) -> dict:
        scope = self.read().get(f"{pack_id}:{playthrough_id}", {})
        surface = route.get("surfaceId")
        raw_ledger = scope.get("ledger", [])
        seen = set()
        clean_ledger = []
        for item in reversed(raw_ledger):
            summary = str(item.get("summary") or "").strip()
            raw_surface = item.get("surfaceId") or "system"
            label = surface_label(raw_surface)
            key = (label, summary)
            if not any(character.isalnum() for character in summary) or key in seen:
                continue
            seen.add(key)
            clean_ledger.append({"surfaceId": label, "summary": summary,
                                 "source": item.get("source"), "status": item.get("status", "applied"),
                                 "createdAt": item.get("createdAt"),
                                 "_rawSurface": raw_surface})
        clean_ledger.reverse()
        raw_surfaces = {name: summary for name, summary in scope.get("surfaces", {}).items()
                        if any(character.isalnum() for character in str(summary))}
        surfaces = {surface_label(name): summary for name, summary in raw_surfaces.items()}
        if route.get("role") == "os-agent":
            return {"surfaces": dict(list(surfaces.items())[-6:]),
                    "ledger": [{k: v for k, v in entry.items() if not k.startswith("_")}
                               for entry in clean_ledger[-6:]]}
        target_summary = raw_surfaces.get(surface) or surfaces.get(surface_label(surface))
        subagent_ledger = [{k: v for k, v in entry.items() if not k.startswith("_")}
                           for entry in clean_ledger
                           if entry.get("_rawSurface") == surface or entry.get("surfaceId") == surface_label(surface)][-5:]
        return {"surface": target_summary,
                "ledger": subagent_ledger}

    def commit(self, pack_id: str, playthrough_id: str, event_id: str, route: dict,
               batch_ids: list[str], summary: str, source: str,
               step: int | None = None, status: str = "applied") -> None:
        with memory_lock(self.path):
            data = self.read()
            scope = data.setdefault(f"{pack_id}:{playthrough_id}", {"surfaces": {}, "ledger": []})
            if any(item["eventId"] == event_id and item.get("step") == step for item in scope["ledger"]):
                return
            surface = route.get("surfaceId")
            if status == "applied" and surface and summary:
                scope["surfaces"][surface] = summary[:500]
            scope["ledger"].append({"eventId": event_id, "step": step, "roleId": route.get("roleId"),
                                    "surfaceId": surface, "batchIds": batch_ids,
                                    "summary": summary[:1000], "source": source, "status": status,
                                    "createdAt": datetime.now(timezone.utc).isoformat()})
            expired = scope["ledger"][:-100]
            if expired:
                archive = self.path.parent / "memory-archive" / f"{datetime.now(timezone.utc):%Y-%m}.jsonl"
                archive.parent.mkdir(parents=True, exist_ok=True)
                with archive.open("a", encoding="utf-8") as stream:
                    for item in expired:
                        stream.write(json.dumps({"scope": f"{pack_id}:{playthrough_id}", "entry": item}, ensure_ascii=False) + "\n")
                scope["ledger"] = scope["ledger"][-100:]
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
