"""Build small event messages for Muse; role documents stay behind the connector."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from content_cache import ContentCache
from delegation import target_options


PROMPT_DIR = Path(__file__).parent / "prompts"
SURFACE_NAMES = {
    "agent": "个人代理", "journal": "日记", "messages": "私信",
    "maps": "地图", "socialmedia": "社交动态", "notes": "备忘录",
    "world": "世界", "characters": "人物", "photos": "相册", "system": "系统",
}


def tool_data(mcp_call, name: str, args: dict) -> dict:
    return json.loads(mcp_call(name, args)["content"][0]["text"])


class MuseContext:
    def __init__(self):
        self.cache = ContentCache()

    def version(self) -> str:
        return hashlib.sha256((PROMPT_DIR / "agent-turn.md").read_bytes()).hexdigest()

    def template(self, record: dict, name: str) -> str:
        path = self.cache.root(record["packId"]) / "prompts" / name
        return path.read_text(encoding="utf-8").strip()

    def local_paths(self, record: dict, route: dict) -> dict:
        pack_id, playthrough_id = record["packId"], record["playthroughId"]
        root = self.cache.root(pack_id)
        memory = root / playthrough_id / "memory"
        paths = {
            "rolePrompt": self.cache.role_prompt(pack_id, route),
            "actionCatalog": str(root / "prompts" / "app-actions.json"),
            "toolReminder": str(root / "prompts" / "tool-reminder.md"),
            "delegationInject": str(root / "prompts" / "delegation-inject.md"),
            "readableDocuments": self.cache.allowed_paths(
                pack_id, route, record["decrypted"].get("grantedReferenceIds") or []),
            "summary": str(memory / "summary.md"),
            "pins": str(memory / "pins.md"),
            "taskDirectory": str(root / playthrough_id / "tasks" / "delegations" / record["id"]),
        }
        if route["role"] == "os-agent":
            paths["delegationTargets"] = target_options(record)
        return paths

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
        user_input = {key: payload[key] for key in ("prompt", "text", "requestKind", "platform", "action", "item", "location")
                      if key in payload}
        current = {
            "packId": pack_id, "batchId": batch_id,
            "role": route["role"], "type": event.get("type"),
            "surface": SURFACE_NAMES.get(event.get("appId"), event.get("appId")),
            "userInput": user_input, "surfaceSummary": event.get("surfaceSummary"),
            "sharedMemory": memory,
            "localContent": self.local_paths(record, route),
        }
        if payload.get("recentHistory"):
            current["recentChatHistory"] = [
                {key: value for key, value in item.items() if key not in {"id", "timestamp"}}
                for item in payload["recentHistory"]]
        if event.get("recentControlMessages"):
            current["recentControlMessages"] = [
                {key: value for key, value in item.items() if key not in {"id", "timestamp"}}
                for item in event["recentControlMessages"]]
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
        if route["role"] == "os-agent":
            parts.append(f"需要其他 App 执行具体操作时，在云电脑目录 {current['localContent']['taskDirectory']} 写一份 <uuid4>.md 任务文档。“目标:”只能从 localContent.delegationTargets 复制完整值；调用 xphone.delegate_task 时只填文档绝对路径。若工具报目标不在列表，按返回的可用目标修正文档后重试。成功回执只表示 subagent 已接收，执行结果下轮从 ledger 读取。")
        parts.append("当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")))
        parts.append(self.template(record, "tool-reminder.md"))
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
        payload = event.get("payload") or {}
        user_prompt = payload.get("prompt") or payload.get("text") or ""
        actors = [actor["name"] for actor in event.get("actors", [])]
        current = {
            "packId": pack_id, "batchId": batch_id,
            "role": "os-agent", "stage": "post_journal_orchestration",
            "type": "journal.post_orchestration",
            "userPrompt": user_prompt,
            "actors": actors,
            "notes": [note["title"] for note in event.get("notes", [])],
            "sharedMemory": memory,
            "localContent": self.local_paths(record, record["route"]),
        }
        if journal_summary:
            current["journalSummary"] = journal_summary[:500]
        parts = [
            "【故事正文已落盘，进入续写后角色与世界联动阶段 (Phase 2: Post-Journal Orchestration)】",
        ]
        if user_prompt:
            parts.append(f"【用户对本轮故事的联动与交办指示】\n{user_prompt}")
        parts.extend([
            f"故事正文已保存。当前角色列表：{json.dumps(actors, ensure_ascii=False)}",
            f"请根据最新剧情进展与用户的交办指示规划联动。每项 App 动作都在云电脑目录 {current['localContent']['taskDirectory']} 写一份 <uuid4>.md 任务文档。“目标:”只能从 localContent.delegationTargets 复制完整值，再用 xphone.delegate_task 只提交文档绝对路径。若工具报目标不在列表，按返回的可用目标修正文档后重试。不得由 OS 直接提交私聊、地图、社媒、笔记或世界动作。",
            "如无需联动：直接回复“无需联动”。",
            "当前上下文与批次：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")),
            self.template(record, "tool-reminder.md"),
        ])
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        inspiration = self.timed_note(record, "inspiration.md")
        if inspiration:
            parts.append("[现实素材，仅作虚构灵感]\n" + inspiration)
        return "\n\n".join(parts)

    def delegated_prompt(self, record: dict, route: dict, batch: str, step: int,
                         task_path: str, memory: dict, first_turn: bool) -> str:
        delegated = {**record, "route": route}
        current = {
            "packId": record["packId"],
            "batchId": batch,
            "parentEventId": record["id"],
            "step": step,
            "targetApp": route["targetApp"],
            "surfaceId": route["surfaceId"],
            "taskPath": task_path,
            "role": "subagent",
            "localContent": self.local_paths(delegated, route),
            "sharedMemory": memory,
        }
        parts = []
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        local = current["localContent"]
        documents = "\n".join(local["readableDocuments"]) or "本轮没有已解锁的故事文档"
        parts.extend([self.template(record, "delegation-inject.md"),
                      f"现在先用云电脑文件读取能力打开任务文档：{task_path}\n再读角色说明：{local['rolePrompt']}\n动作清单：{local['actionCatalog']}\n可读故事文档：\n{documents}",
                      "当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False, separators=(",", ":")),
                      self.template(record, "tool-reminder.md")])
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        return "\n\n".join(parts)
