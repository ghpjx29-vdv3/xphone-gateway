"""Build small event messages for Muse; role documents stay behind the connector."""

import hashlib
import json
from pathlib import Path


PROMPT_DIR = Path(__file__).parent / "prompts"


def tool_data(mcp_call, name: str, args: dict) -> dict:
    return json.loads(mcp_call(name, args)["content"][0]["text"])


class MuseContext:
    def version(self) -> str:
        return hashlib.sha256((PROMPT_DIR / "agent-turn.md").read_bytes()).hexdigest()

    def prompt(self, record: dict, pack_id: str, playthrough_id: str, batch_id: str,
               memory: dict, first_turn: bool, rejected_batch: str | None = None,
               rejected_reason: str | None = None) -> str:
        event = record["decrypted"]
        route = record["route"]
        payload = event.get("payload") or {}
        user_input = {key: payload[key] for key in ("text", "requestKind", "platform", "action", "item", "location")
                      if key in payload}
        current = {
            "packId": pack_id, "playthroughId": playthrough_id,
            "eventId": record["id"], "batchId": batch_id,
            "role": route["role"], "targetApp": route.get("targetApp"),
            "consumer": route["consumer"], "type": event.get("type"),
            "appId": event.get("appId"), "surfaceId": event.get("surfaceId"),
            "userInput": user_input, "surfaceSummary": event.get("surfaceSummary"),
            "sharedMemory": memory,
        }
        if payload.get("recentHistory"):
            current["recentChatHistory"] = payload["recentHistory"]
        if event.get("recentControlMessages"):
            current["recentControlMessages"] = event["recentControlMessages"]
        if rejected_batch:
            current["previousRejectedBatchId"] = rejected_batch
        if rejected_reason:
            current["previousRejectedReason"] = rejected_reason
        if route.get("targetApp") == "messages":
            target = next((actor for actor in event.get("actors", [])
                           if actor.get("actorId") == payload.get("actorId")
                           or actor.get("threadId") == payload.get("conversationId")), None)
            if target:
                current["targetCharacter"] = target["name"]
        if route["role"] == "os-agent":
            current["windowMemory"] = event.get("windowMemory")
            current["actors"] = [actor["name"] for actor in event.get("actors", [])]
            current["notes"] = [note["title"] for note in event.get("notes", [])]

        parts = []
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        parts.append("当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        parts.append((PROMPT_DIR / "tool-hook.md").read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)
