"""Route local OS task files to Muse side chats and track applied child batches."""

import json
import logging
import re
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from muse_delivery import batch_id, delivery_status
from submit_actions import seal


log = logging.getLogger("xphone-gateway")
TASK_NAME = re.compile(r"^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}\.md$")
APP_TARGETS = {
    "maps / 地图": ("maps", "maps.active", "maps"),
    "socialmedia / 社交动态": ("socialmedia", "socialmedia.feed", "socialmedia"),
    "notes / 备忘录": ("notes", "notes.active", "os"),
    "world / 世界": ("world", "world.active", "os"),
    "characters / 人物": ("characters", "characters.active", "os"),
    "photos / 相册": ("photos", "photos.active", "os"),
}


def target_options(parent: dict) -> list[str]:
    return [*(f"messages / {actor['name']}" for actor in parent["decrypted"].get("actors") or []),
            *APP_TARGETS]


def route_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "_", value.strip() or "none")[:120]


class Delegation:
    def __init__(self, dispatcher, content_root: Path):
        self.dispatcher = dispatcher
        self.content_root = content_root
        self.dispatcher.state.setdefault("delegations", {})

    def task_root(self, pack_id: str, playthrough_id: str) -> Path:
        return self.content_root / pack_id / playthrough_id / "tasks" / "delegations"

    @staticmethod
    def parse(content: str) -> tuple[str, str, str]:
        lines = content.splitlines()
        if len(lines) < 2 or not lines[0].startswith("目标: ") or not lines[1].startswith("任务: "):
            raise ValueError("任务文件须以“目标:”“任务:”两行开始")
        target = lines[0][4:].strip()
        task = "\n".join([lines[1][4:].strip(), *lines[2:]]).strip()
        app, separator, page = target.partition(" / ")
        if not separator or not app or not page or not task:
            raise ValueError("任务文件须填写目标和任务；“目标:”请从本轮 localContent.delegationTargets 逐字复制完整值，修改后重新调用 xphone.delegate_task")
        return app, page, task

    @staticmethod
    def route(parent: dict, app: str, page: str) -> dict:
        window = parent["route"]["windowId"]
        target = f"{app} / {page}"
        options = target_options(parent)
        if target not in options:
            raise ValueError(f"目标“{target}”不在本轮可交办列表中。请把任务文档的“目标:”改为以下完整值之一，再调用 xphone.delegate_task：{'、'.join(options)}")
        if app == "messages":
            actor_id = next(actor["actorId"] for actor in parent["decrypted"]["actors"]
                            if actor["name"] == page)
            surface, consumer = f"messages.dm.{actor_id}", f"messages.{actor_id}"
            prefix = "storyMessagesToolsV1"
        else:
            _, surface, consumer = APP_TARGETS[target]
            prefix = "storyPromptV1"
        role_id = f"subagent:{prefix}:{app}:{route_part(window)}:{route_part(surface)}"
        return {"role": "subagent", "roleId": role_id, "targetApp": app,
                "surfaceId": surface, "consumer": consumer, "windowId": window}

    def log_state(self, root: Path, task_path: str, state: str, event_id: str, step: int) -> None:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "log.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                     "taskPath": task_path, "state": state,
                                     "eventId": event_id, "step": step}, ensure_ascii=False) + "\n")

    def prepare(self, job_id: str, task_path: str, pack_id: str, playthrough_id: str) -> dict:
        root = self.task_root(pack_id, playthrough_id)
        if not Path(task_path).is_absolute():
            raise ValueError("taskPath 必须是任务文档的绝对路径")
        path = Path(task_path).resolve()
        if str(path) != task_path or not TASK_NAME.fullmatch(path.name) or path.parent.parent != root.resolve():
            raise ValueError("taskPath 必须指向当前游玩的 delegations/{eventId}/{uuid4}.md")
        key = str(path)
        existing = self.dispatcher.state["delegations"].get(key)
        if existing:
            existing["jobId"] = job_id
            self.dispatcher.save()
            return self.association(existing)
        if not path.exists():
            raise FileNotFoundError(f"找不到任务文件：{task_path}")
        parent_id = path.parent.name
        app, page, _ = self.parse(path.read_text(encoding="utf-8"))
        parent_path = self.dispatcher.inbox / f"{parent_id}.json"
        if not parent_path.exists():
            raise ValueError("父事件不在运行中")
        parent = json.loads(parent_path.read_text(encoding="utf-8"))
        if parent["packId"] != pack_id or parent["playthroughId"] != playthrough_id or parent["route"]["role"] != "os-agent":
            raise ValueError("父事件账号范围或角色不匹配")
        route = self.route(parent, app, page)
        with self.dispatcher.state_lock:
            for previous in self.dispatcher.state["delegations"].values():
                if (not previous.get("jobId") and previous["eventId"] == parent_id
                        and previous["route"]["surfaceId"] == route["surfaceId"]
                        and "rejected" in (previous["status"], previous.get("deliveryStatus"))):
                    previous["superseded"] = True
            attempt = self.dispatcher.state["events"].setdefault(parent_id, {"failures": 0, "nextAt": 0})
            step = attempt.get("nextDelegateStep", 1)
            attempt["nextDelegateStep"] = step + 1
            session_key, _ = self.dispatcher.session_key({**parent, "route": route})
            record = {"status": "prepared", "jobId": job_id, "taskPath": task_path, "eventId": parent_id,
                      "step": step, "batchId": batch_id(pack_id, playthrough_id, parent_id, step),
                      "route": route, "sessionKey": session_key,
                      "packId": pack_id, "playthroughId": playthrough_id}
            self.dispatcher.state["delegations"][key] = record
            attempt.setdefault("delegations", []).append(task_path)
            self.dispatcher.save()
        return self.association(record)

    @staticmethod
    def association(record: dict) -> dict:
        route = record["route"]
        return {"batchId": record["batchId"], "step": record["step"],
                "targetApp": route["targetApp"], "surfaceId": route["surfaceId"]}

    def launch(self, job_id: str, task_path: str, deadline_at: int) -> None:
        record = self.dispatcher.state["delegations"].get(task_path)
        if not record or record.get("jobId") != job_id:
            raise RuntimeError("找不到已准备的派工")
        session_key = record["sessionKey"]
        with self.dispatcher.session_lock(session_key):
            if record["status"] == "failed":
                raise RuntimeError(record["error"])
            if record["status"] != "prepared":
                return
            if time.time() * 1000 >= deadline_at:
                raise RuntimeError("派工已超过等待提交的期限")
            parent_id = record["eventId"]
            pack_id, playthrough_id = record["packId"], record["playthroughId"]
            route, batch, step = record["route"], record["batchId"], record["step"]
            record.update(status="launching", sentAt=time.time())
            self.dispatcher.save()
            try:
                parent = json.loads((self.dispatcher.inbox / f"{parent_id}.json").read_text(encoding="utf-8"))
                session_id = self.dispatcher.session_id(session_key)
                memory = self.dispatcher.memory.view(pack_id, playthrough_id, route)
                prompt = self.dispatcher.context.delegated_prompt(
                    parent, route, batch, step, task_path, memory,
                    self.dispatcher.state["promptVersions"].get(session_key) != self.dispatcher.context.version())
                result = self.dispatcher.cli("send", prompt, "--thread", session_id, "--wait", "60")
                if result.get("sent") is not True:
                    raise RuntimeError("Muse 未确认任务送达")
            except Exception as error:
                if getattr(error, "reason", None) == "TimeoutExpired":
                    log.warning("Muse send outcome unknown: job=%s", job_id)
                    return
                if record["status"] == "launching":
                    record.update(status="failed", error=str(error))
                    self.dispatcher.save()
                raise
            if record["status"] == "launching":
                record["status"] = "sent"
            self.dispatcher.state["promptVersions"][session_key] = self.dispatcher.context.version()
            self.dispatcher.save()
            self.log_state(self.task_root(pack_id, playthrough_id), task_path, record["status"], parent_id, step)

    def prepare_actions(self, task_path: str, pack_id: str, playthrough_id: str,
                        key_bytes: bytes) -> dict:
        path = Path(task_path).resolve()
        root = self.content_root / pack_id / playthrough_id / "tasks"
        delegated = path.parent.parent == (root / "delegations").resolve() and TASK_NAME.fullmatch(path.name)
        event = path.parent.parent == (root / "events").resolve() and path.name == f"{path.parent.name}.md"
        if not delegated and not event:
            raise ValueError("taskPath 必须指向当前游玩的任务文档")
        if delegated:
            record = self.dispatcher.state["delegations"].get(str(path))
            if not record or record["status"] not in ("launching", "sent"):
                raise ValueError("没有运行中的派工；请核对原任务文档路径")
            batch = record["batchId"]
            event_id, step, route = record["eventId"], record["step"], record["route"]
            origin = "delegation"
        else:
            event_id = path.parent.name
            attempt = self.dispatcher.state["events"].get(event_id)
            parent_path = self.dispatcher.inbox / f"{event_id}.json"
            if not attempt or not parent_path.exists():
                raise ValueError("没有运行中的事件")
            parent = json.loads(parent_path.read_text(encoding="utf-8"))
            if parent["packId"] != pack_id or parent["playthroughId"] != playthrough_id:
                raise ValueError("事件不属于当前游玩")
            step = attempt.get("step", 0)
            batch = attempt.get("batchId") or batch_id(pack_id, playthrough_id, event_id, step)
            route = parent.get("route") or {}
            origin = "event"
        action_path = path.with_suffix(".actions.json")
        if not action_path.exists():
            raise FileNotFoundError(f"找不到动作文件：{action_path}；请先写入纯语义动作数组")
        actions = json.loads(action_path.read_text(encoding="utf-8"))
        if not isinstance(actions, list) or not actions:
            raise ValueError("动作文件必须是非空 JSON 动作数组；不要填写 batchId 等路由字段")
        for action in actions:
            if action.get("type") != "journal.appendMainRecord":
                continue
            options = action.get("options")
            if not isinstance(options, list) or len(options) != 3 or any(
                not isinstance(option, dict)
                or type(option.get("index")) is not int or option["index"] != index
                or not isinstance(option.get("text"), str) or not option["text"].strip()
                for index, option in enumerate(options, 1)
            ):
                raise ValueError("journal.appendMainRecord 必须提供 options：按顺序填写 index 为 1、2、3 的三个选项，text 不能为空。请修改动作文件后重新提交。")
        header = {"batchId": batch, "parentEventId": event_id, "step": step,
                  "targetApp": route.get("targetApp") or ("journal" if event and parent["decrypted"].get("type") == "journal.main_record.requested" else "agent"),
                  "surfaceId": route.get("surfaceId") or ("journal.main_record" if event and parent["decrypted"].get("type") == "journal.main_record.requested" else "agent.reply"),
                  "origin": origin}
        payload = {"actions": actions}
        frame = None
        if event and len(actions) == 1 and actions[0].get("type") == "journal.appendMainRecord":
            source = parent["decrypted"]
            details = source.get("payload") or {}
            tick_id = source["tickId"]
            stream_id = details["streamId"]
            record_id = details["recordId"]
            actions[0]["recordId"] = record_id
            payload["tickId"] = tick_id
            metadata = {"tickId": tick_id, "streamId": stream_id, "seq": 0, "phase": "start"}
            frame = seal(key_bytes, pack_id, playthrough_id, "journal_frame", f"{batch}.frame",
                         {**metadata, "recordId": record_id, "offset": 0, "text": actions[0]["markdown"]},
                         metadata=metadata)
        envelope = seal(key_bytes, pack_id, playthrough_id, "action_batch", batch,
                        payload, header=header)
        return {"envelope": envelope, "frame": frame}

    def poll(self, mcp_call, pack_id: str, playthrough_id: str) -> None:
        for record in list(self.dispatcher.state["delegations"].values()):
            if record["packId"] != pack_id or record["playthroughId"] != playthrough_id or record["status"] not in ("launching", "sent", "failed"):
                continue
            status = delivery_status(mcp_call, record["batchId"])
            if status not in ("applied", "rejected"):
                continue
            task_path = record["taskPath"]
            root = self.task_root(pack_id, playthrough_id)
            summary = ""
            if status == "applied":
                record.setdefault("appliedAt", time.time())
                session_id = self.dispatcher.state["sessions"].get(record["sessionKey"])
                if session_id:
                    history = self.dispatcher.cli("history", "--thread", session_id, "--limit", "10")
                    messages = history if isinstance(history, list) else history.get("messages", [])
                    summary = next((str(item.get("text", ""))[:500] for item in reversed(messages)
                                    if task_path in str(item.get("text", "")) and
                                    "assistant" in str(item.get("role", "")).lower()), "")
                if not summary and time.time() - record["appliedAt"] < 120:
                    self.dispatcher.save()
                    continue
                if not summary:
                    summary = "客户端已应用派工动作；旁聊未提供文字摘要。"
            else:
                reason = self.dispatcher.rejection_reason(record["batchId"])
                summary = f"派工动作被客户端拒收：{reason or '未提供原因'}"
            self.dispatcher.memory.commit(pack_id, playthrough_id, record["eventId"],
                                          record["route"], [record["batchId"]], summary,
                                          "reply_text" if summary else "delivery_only",
                                          step=record["step"], status=status)
            record["status"] = status
            self.dispatcher.save()
            self.log_state(root, task_path, status, record["eventId"], record["step"])
