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
            if event.get("type") == "journal.main_record.requested":
                current["stage"] = "journal_direct_write"

        parts = []
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        if event.get("type") == "journal.main_record.requested":
            parts.append("【故事续写直接写作阶段 (Phase 1)】\n请调用 `xphone.submit_journal_stream_frame` 流式输出草稿，并在最后调用 `xphone.submit_action_batch` 独占提交 `journal.appendMainRecord`（batchId 必须为此轮上下文中的 batchId）。此阶段请勿提交其他角色私聊或动作；正文落盘后将进入 Phase 2 角色联动编排。")
        parts.append("当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        parts.append((PROMPT_DIR / "tool-hook.md").read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def post_journal_prompt(self, record: dict, pack_id: str, playthrough_id: str,
                            batch_id: str, memory: dict, journal_summary: str = "") -> str:
        event = record["decrypted"]
        actors = [actor["name"] for actor in event.get("actors", [])]
        current = {
            "packId": pack_id, "playthroughId": playthrough_id,
            "eventId": record["id"], "batchId": batch_id,
            "role": "os-agent", "stage": "post_journal_orchestration",
            "type": "journal.post_orchestration",
            "actors": actors,
            "notes": [note["title"] for note in event.get("notes", [])],
            "sharedMemory": memory,
        }
        if journal_summary:
            current["journalSummary"] = journal_summary[:500]
        parts = [
            "【故事正文已落盘，进入续写后角色与世界联动阶段 (Phase 2: Post-Journal Orchestration)】",
            f"故事正文已保存。当前角色列表：{json.dumps(actors, ensure_ascii=False)}",
            "请根据最新剧情进展，规划后续联动并执行派工：",
            "1. 私聊联动：若需角色向玩家发私信，请调用 `xphone.submit_action_batch`（关联 batchId），提交 `messages.append`，并必须注明 `sender` 为角色显示名（例如：{\"type\": \"messages.append\", \"sender\": \"晚舟\", \"text\": \"...\"}）。",
            "2. 社交/地图/世界：如需发动态、调位置或改变量，可提交 `socialmedia.applyBatch`、`map.set_actor_position`、`world.applyStatePatch` 等。",
            "3. 如无需联动：直接回复说明“无需联动”，不要调用 `xphone.submit_action_batch`。",
            "当前上下文与批次：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")),
            (PROMPT_DIR / "tool-hook.md").read_text(encoding="utf-8").strip(),
        ]
        return "\n\n".join(parts)
