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


class MuseMemory:
    def __init__(self, path: str):
        self.path = Path(path)

    def read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def view(self, pack_id: str, playthrough_id: str, route: dict) -> dict:
        scope = self.read().get(f"{pack_id}:{playthrough_id}", {})
        surface = route.get("surfaceId")
        raw_ledger = scope.get("ledger", [])
        clean_ledger = [
            {"surfaceId": item.get("surfaceId") or "system", "summary": item["summary"],
             "source": item.get("source"), "status": item.get("status", "applied")}
            for item in raw_ledger if item.get("summary") and item["summary"].strip()
        ]
        if route.get("role") == "os-agent":
            return {"surfaces": dict(list(scope.get("surfaces", {}).items())[-12:]),
                    "ledger": clean_ledger[-12:]}
        return {"surface": scope.get("surfaces", {}).get(surface),
                "ledger": [item for item in clean_ledger if item.get("surfaceId") == surface][-5:]}

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
