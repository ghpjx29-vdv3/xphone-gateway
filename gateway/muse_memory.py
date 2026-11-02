"""Shared semantic memory for one XPhone playthrough."""

import json
import os
from pathlib import Path


class MuseMemory:
    def __init__(self, path: str):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def view(self, pack_id: str, playthrough_id: str, route: dict) -> dict:
        scope = self.data.get(f"{pack_id}:{playthrough_id}", {})
        surface = route.get("surfaceId")
        if route.get("role") == "os-agent":
            return {"surfaces": dict(list(scope.get("surfaces", {}).items())[-12:]),
                    "ledger": scope.get("ledger", [])[-12:]}
        return {"surface": scope.get("surfaces", {}).get(surface),
                "ledger": [item for item in scope.get("ledger", []) if item.get("surfaceId") == surface][-5:]}

    def commit(self, pack_id: str, playthrough_id: str, event_id: str, route: dict,
               batch_ids: list[str], summary: str, source: str) -> None:
        scope = self.data.setdefault(f"{pack_id}:{playthrough_id}", {"surfaces": {}, "ledger": []})
        if any(item["eventId"] == event_id for item in scope["ledger"]):
            return
        surface = route.get("surfaceId")
        if surface and summary:
            scope["surfaces"][surface] = summary[:500]
        scope["ledger"].append({"eventId": event_id, "roleId": route.get("roleId"),
                                "surfaceId": surface, "batchIds": batch_ids,
                                "summary": summary[:1000], "source": source})
        scope["ledger"] = scope["ledger"][-100:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)
