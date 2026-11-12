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
EVENT_NAMES = {
    "os.user_message": "用户给个人代理发消息",
    "messages.user_sent": "用户发来私信",
    "journal.main_record.requested": "用户请求续写故事",
}
STAGE_NAMES = {
    "journal_direct_write": "先写本轮故事正文",
    "post_journal_orchestration": "正文已保存，安排后续联动",
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
            event_id = record["id"]
            paths["eventTaskPath"] = str(root / playthrough_id / "tasks" / "events" / event_id / f"{event_id}.md")
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

    @staticmethod
    def add_detail(lines: list[str], label: str, value, depth: int = 0) -> None:
        if value is None or value == "" or value == [] or value == {}:
            return
        prefix = "  " * depth
        if isinstance(value, dict):
            lines.append(f"{prefix}{label}：")
            for key, item in value.items():
                MuseContext.add_detail(lines, str(key), item, depth + 1)
        elif isinstance(value, list):
            lines.append(f"{prefix}{label}：")
            for item in value:
                if isinstance(item, (dict, list)):
                    MuseContext.add_detail(lines, "-", item, depth + 1)
                else:
                    lines.append(f"{prefix}  - {item}")
        else:
            lines.append(f"{prefix}{label}：{value}")

    def readable_context(self, record: dict, current: dict) -> str:
        event = record["decrypted"]
        user_input = current.get("userInput") or {}
        latest = user_input.get("text") or user_input.get("prompt") or current.get("userPrompt")
        lines = ["【本轮最新任务｜先处理这里】",
                 f"故事：{record['packId']}",
                 f"来自：{'用户与个人代理的对话' if event.get('type') == 'os.user_message' else current.get('surface') or '故事事件'}",
                 f"事件：{EVENT_NAMES.get(event.get('type'), event.get('type') or '交办任务')}"]
        if current.get("stage"):
            lines.append(f"当前阶段：{STAGE_NAMES.get(current['stage'], current['stage'])}")
        if latest:
            lines.append("用户这次说：")
            lines.extend(f"  {line}" for line in str(latest).splitlines())
        if current.get("taskPath"):
            lines.append(f"这次要执行的任务文档：{current['taskPath']}")
        for key, label in (("requestKind", "请求类型"), ("platform", "平台"),
                           ("action", "操作"), ("item", "对象"), ("location", "位置")):
            self.add_detail(lines, label, user_input.get(key))
        if current.get("previousRejectedReason"):
            lines.append(f"上一次提交未通过：{current['previousRejectedReason']}")

        history = current.get("recentControlMessages") or current.get("recentChatHistory") or []
        if history:
            lines.extend(["", "【之前的对话｜从旧到新，只作背景】"])
            for number, item in enumerate(sorted(history, key=lambda entry: entry.get("timestamp") or 0), 1):
                speaker = "用户" if item.get("role") == "user" else "代理或角色"
                lines.append(f"{number}. {speaker}：{item.get('text') or item.get('content') or '[图片]'}")

        memory = current.get("sharedMemory") or {}
        ledger_summaries = {str(item.get("summary") or "").strip() for item in memory.get("ledger", [])}
        seen = set()
        facts = []
        for surface, summary in memory.get("surfaces", {}).items():
            if summary and summary not in ledger_summaries and summary not in seen:
                seen.add(summary)
                facts.append(f"- {SURFACE_NAMES.get(surface.split('.')[0], surface)}：{summary}")
        for item in memory.get("ledger", []):
            summary = str(item.get("summary") or "").strip()
            if summary and summary not in seen:
                seen.add(summary)
                surface = str(item.get("surfaceId") or "system").split(".")[0]
                time_label = str(item.get("createdAt") or "")[:16].replace("T", " ")
                status = "被拒收" if item.get("status") == "rejected" else "已应用"
                facts.append(f"- {time_label + ' ' if time_label else ''}{SURFACE_NAMES.get(surface, surface)}（{status}）：{summary}")
        if memory.get("surface") and memory["surface"] not in seen:
            facts.append(f"- 当前页面：{memory['surface']}")
        if facts:
            lines.extend(["", "【历史执行记录｜从旧到新，只作背景】", *facts[-6:]])

        for key, label in (("surfaceSummary", "当前页面摘要"), ("windowMemory", "当前剧情记忆"),
                           ("journalSummary", "刚写完的故事正文摘要"),
                           ("targetCharacter", "这次对话的角色"), ("actors", "人物"), ("notes", "备忘录")):
            if current.get(key):
                lines.append("")
                self.add_detail(lines, label, current[key])

        local = current.get("localContent") or {}
        lines.extend(["", "【本轮可用的本地文件】"])
        for key, label in (("rolePrompt", "角色说明"), ("readableDocuments", "可读故事资料"),
                           ("summary", "共享摘要"), ("pins", "重点事实"),
                           ("actionCatalog", "动作清单"), ("eventTaskPath", "本轮动作任务文件"),
                           ("taskDirectory", "交办任务目录"), ("delegationTargets", "允许交办的目标")):
            self.add_detail(lines, label, local.get(key))
        return "\n".join(lines)

    def prompt(self, record: dict, pack_id: str, playthrough_id: str, batch_id: str,
               memory: dict, first_turn: bool, rejected_batch: str | None = None,
               rejected_reason: str | None = None) -> str:
        event = record["decrypted"]
        route = record["route"]
        payload = event.get("payload") or {}
        user_input = {key: payload[key] for key in ("prompt", "text", "requestKind", "platform", "action", "item", "location")
                      if key in payload}
        current = {
            "packId": pack_id,
            "role": route["role"], "type": event.get("type"),
            "surface": SURFACE_NAMES.get(event.get("appId"), event.get("appId")),
            "userInput": user_input, "surfaceSummary": event.get("surfaceSummary"),
            "sharedMemory": memory,
            "localContent": self.local_paths(record, route),
        }
        if payload.get("recentHistory"):
            current["recentChatHistory"] = [
                item for item in payload["recentHistory"] if item.get("id") != payload.get("messageId")]
        if event.get("recentControlMessages"):
            current["recentControlMessages"] = [
                item for item in event["recentControlMessages"] if item.get("id") != payload.get("messageId")]
        if rejected_reason:
            current["previousRejectedReason"] = rejected_reason
        if route.get("targetApp") == "messages":
            target = next((actor for actor in event.get("actors", [])
                           if actor.get("actorId") == payload.get("actorId")
                           or actor.get("threadId") == payload.get("conversationId")), None)
            if target:
                current["targetCharacter"] = target["name"]
        if route["role"] == "os-agent":
            event_task = Path(current["localContent"]["eventTaskPath"])
            event_task.parent.mkdir(parents=True, exist_ok=True)
            event_task.write_text("任务: 按当前事件生成个人代理回复或日记正文动作。\n", encoding="utf-8")
            current["windowMemory"] = event.get("windowMemory")
            current["actors"] = [actor["name"] for actor in event.get("actors", [])]
            current["notes"] = [note["title"] for note in event.get("notes", [])]
            if event.get("type") == "journal.main_record.requested":
                current["stage"] = "journal_direct_write"

        parts = [self.readable_context(record, current)]
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        if event.get("type") == "journal.main_record.requested":
            parts.append("【故事续写直接写作阶段 (Phase 1)】\n在下方“本轮动作任务文件”同名的 .actions.json 文件中写入只含一个 journal.appendMainRecord 的动作数组，字段为 type 与 markdown。调用 xphone.submit_task_actions，只填任务文档的完整路径。网关自动发送单帧草稿并提交正文；正文落盘后进入下一阶段。")
        if route["role"] == "os-agent":
            parts.append(f"需要其他 App 执行具体操作时，在云电脑目录 {current['localContent']['taskDirectory']} 写一份 <uuid4>.md 任务文档。“目标:”只能从下方“允许交办的目标”复制完整值；调用 xphone.delegate_task 时只填文档绝对路径。返回任务编号表示已受理，可用 xphone.get_delegation 查看进度。收到 applied 后才能告知用户操作已完成。")
            parts.append("个人代理自身回复：在下方“本轮动作任务文件”同名的 .actions.json 文件写纯语义动作数组，调用 xphone.submit_task_actions，只填任务文档完整路径。")
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
            "packId": pack_id,
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
            self.readable_context(record, current),
        ]
        if user_prompt:
            parts.append(f"【用户对本轮故事的联动与交办指示】\n{user_prompt}")
        parts.extend([
            f"故事正文已保存。当前角色列表：{json.dumps(actors, ensure_ascii=False)}",
            f"请根据最新剧情进展与用户的交办指示规划联动。每项 App 动作都在云电脑目录 {current['localContent']['taskDirectory']} 写一份 <uuid4>.md 任务文档。“目标:”只能从下方“允许交办的目标”复制完整值，再用 xphone.delegate_task 只提交文档绝对路径。不得由个人代理直接提交私聊、地图、社媒、笔记或世界动作。",
            "如无需联动：直接回复“无需联动”。",
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
            "taskPath": task_path,
            "role": "subagent",
            "localContent": self.local_paths(delegated, route),
            "sharedMemory": memory,
        }
        parts = [self.readable_context(record, current)]
        if first_turn:
            parts.append((PROMPT_DIR / "agent-turn.md").read_text(encoding="utf-8").strip())
        local = current["localContent"]
        documents = "\n".join(local["readableDocuments"]) or "本轮没有已解锁的故事文档"
        parts.extend([self.template(record, "delegation-inject.md"),
                      f"现在先用云电脑文件读取能力打开任务文档：{task_path}\n再读角色说明：{local['rolePrompt']}\n动作清单：{local['actionCatalog']}\n可读故事文档：\n{documents}",
                      self.template(record, "tool-reminder.md")])
        health = self.timed_note(record, "health.md")
        if health:
            parts.append("[架构健康提醒]\n" + health)
        return "\n\n".join(parts)
