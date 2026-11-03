"""Build small event messages for Muse; role documents stay behind the connector."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from content_cache import ContentCache


PROMPT_DIR = Path(__file__).parent / "prompts"


def tool_data(mcp_call, name: str, args: dict) -> dict:
    return json.loads(mcp_call(name, args)["content"][0]["text"])


class MuseContext:
    def __init__(self):
        self.cache = ContentCache()

    def version(self) -> str:
        return hashlib.sha256((PROMPT_DIR / "agent-turn.md").read_bytes()).hexdigest()

    def local_paths(self, record: dict, route: dict) -> dict:
        pack_id, playthrough_id = record["packId"], record["playthroughId"]
        root = self.cache.root(pack_id)
        memory = root / playthrough_id / "memory"
        return {
            "rolePrompt": self.cache.role_prompt(pack_id, route),
            "readableDocuments": self.cache.allowed_paths(
                pack_id, route, record["decrypted"].get("grantedReferenceIds") or []),
            "summary": str(memory / "summary.md"),
            "pins": str(memory / "pins.md"),
            "taskPendingDirectory": str(root / playthrough_id / "tasks" / "pending"),
        }

    def timed_note(self, record: dict, name: str) -> str:
        path = self.cache.root(record["packId"]) / record["playthroughId"] / "memory" / name
        if not path.exists():
            return ""
        content = path.read_text(encoding="utf-8")
        first = content.splitlines()[0] if content else ""
        if not first.startswith("expiresAt: "):
            return ""
        expiry = datetime.fromisoformat(first[11:].strip().replace("Z", "+00:00"))
        return content if expiry > datetime.now(timezone.utc) else ""

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
            "localContent": self.local_paths(record, route),
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
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        if route["role"] == "os-agent":
            inspiration = self.timed_note(record, "inspiration.md")
            if inspiration:
                parts.append("[现实素材，仅作虚构灵感]\n" + inspiration)
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
            "localContent": self.local_paths(record, record["route"]),
        }
        if journal_summary:
            current["journalSummary"] = journal_summary[:500]
        parts = [
            "【故事正文已落盘，进入续写后角色与世界联动阶段 (Phase 2: Post-Journal Orchestration)】",
            f"故事正文已保存。当前角色列表：{json.dumps(actors, ensure_ascii=False)}",
            "请根据最新剧情进展规划联动。每项 App 动作各写一份本地任务文件，再用 `xphone.delegate_task` 只提交文件名。不得由 OS 直接提交私聊、地图、社媒、笔记或世界动作。",
            "如无需联动：直接回复“无需联动”。",
            "当前上下文与批次：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")),
            (PROMPT_DIR / "tool-hook.md").read_text(encoding="utf-8").strip(),
        ]
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        inspiration = self.timed_note(record, "inspiration.md")
        if inspiration:
            parts.append("[现实素材，仅作虚构灵感]\n" + inspiration)
        return "\n\n".join(parts)

    def delegated_prompt(self, record: dict, route: dict, batch: str, step: int,
                         task_file: str, task: str, memory: dict, first_turn: bool) -> str:
        delegated = {**record, "route": route}
        current = {
            "packId": record["packId"], "playthroughId": record["playthroughId"],
            "parentEventId": record["id"], "eventId": record["id"],
            "step": step, "batchId": batch, "taskFile": task_file,
            "role": "subagent", "targetApp": route["targetApp"],
            "surfaceId": route["surfaceId"], "consumer": route["consumer"],
            "localContent": self.local_paths(delegated, route), "sharedMemory": memory,
        }
        parts = []
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        parts.extend(["OS 派工任务。只执行目标 App／页面，不替 OS 规划其他 App。提交加密 action batch 时，在批次内容写入 parentEventId、step、targetApp、surfaceId，值照抄下方上下文，客户端用它们关联父事件。",
                      "任务正文：" + task,
                      "当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")),
                      (PROMPT_DIR / "tool-hook.md").read_text(encoding="utf-8").strip()])
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        return "\n\n".join(parts)
