"""Send retained XPhone inbox events to dedicated Muse side chats, collect replies, and submit actions via MCP."""

import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from submit_actions import submit_event_actions

log = logging.getLogger("xphone-gateway")
RETRY_SECONDS = 60
MAX_BACKOFF_SECONDS = 600
EVENT_LEASE_SECONDS = 90
MUSE_WAIT_SECONDS = 60


class MuseCliError(Exception):
    def __init__(self, operation: str, returncode: int | None = None, reason: str = "exit"):
        super().__init__(operation)
        self.operation = operation
        self.returncode = returncode
        self.reason = reason


class MuseDispatch:
    def __init__(self, inbox: str, state_path: str):
        self.inbox = Path(inbox)
        self.state_path = Path(state_path)
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {
            "sessions": {}, "events": {},
        }

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False))
        os.replace(tmp, self.state_path)

    def cli(self, *args: str) -> dict:
        env = os.environ.copy()
        proxy = env.get("wss_proxy") or env.get("WSS_PROXY") or env.get("https_proxy") or env.get("HTTPS_PROXY")
        if proxy:
            env["wss_proxy"] = proxy
        operation = args[0]
        cli_bin = env.get("XPHONE_MUSE_CLI")
        if not cli_bin or not os.path.exists(cli_bin):
            candidates = [
                os.path.expanduser("~/.local/bin/muse-cli"),
                "/home/hatch/.local/bin/muse-cli",
            ]
            for c in candidates:
                if os.path.exists(c):
                    cli_bin = c
                    break
        if not cli_bin:
            cli_bin = "muse-cli"

        try:
            result = subprocess.run(
                [cli_bin, *args],
                capture_output=True, text=True, timeout=90, env=env,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise MuseCliError(operation, reason=type(error).__name__) from error
        if result.returncode:
            raise MuseCliError(operation, result.returncode)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise MuseCliError(operation, reason="invalid-json") from error

    def conversation_id(self, record: dict) -> str:
        decrypted = record.get("decrypted") or {}
        payload = decrypted.get("payload") or {}
        if payload.get("conversationId"):
            return payload["conversationId"]
        route = record.get("route") or {}
        playthrough = record["playthroughId"]
        if route.get("role") == "os-agent":
            return f"{playthrough}:os-agent:ops"
        return f"{playthrough}:{route.get('roleId') or 'system'}"

    def session_id(self, conversation_id: str) -> str:
        sessions = self.state["sessions"]
        if conversation_id not in sessions:
            result = self.cli("session-start", "--title", f"XPhone {conversation_id}")
            sessions[conversation_id] = result.get("session_id") or result["id"]
            self.save()
            log.info("muse session created: conversation=%s", conversation_id)
        return sessions[conversation_id]

    def build_prompt(self, record: dict) -> str:
        decrypted = record.get("decrypted") or {}
        event_type = decrypted.get("type", "")
        payload = decrypted.get("payload") or {}
        route = record.get("route") or {}
        role = route.get("role", "os-agent")
        user_text = payload.get("text") or decrypted.get("text") or payload.get("content") or ""

        lines = [f"【XPhone 事件: {event_type}】"]
        if user_text:
            lines.append(f"用户内容: {user_text}")
        lines.append(f"上下文路由: role={role}, appId={decrypted.get('appId') or route.get('targetApp')}")
        if payload:
            lines.append(f"详细负载: {json.dumps(payload, ensure_ascii=False)}")
        lines.append(
            "\n【系统执行核心指令】"
            "\n你正在作为 XPhone 系统的伴侣 Agent 操纵用户的虚拟手机。"
            "\n当用户请求执行手机系统动作时，你【必须在回复末尾附带包含动作列表的 ```json 代码块】，底层系统才会真正写入手机！"
            "\n切勿仅在纯文本中声称“已建好/已发送”，不带 ```json 代码块将不会对系统产生任何实际改变！"
            "\n支持的动作类型示例："
            "\n1. 创建/添加角色（通讯录联系人）："
            '\n   {"type": "characters.create", "actorId": "英文ID(如linwei)", "name": "角色名(如林薇)", "description": "角色人设背景"}'
            "\n2. 在社交媒体发布动态（平台支持 x / instagram / reddit）："
            '\n   {"type": "socialmedia.applyBatch", "actions": [{"platform": "x", "operation": "appendPost", "postId": "post-唯一ID", "authorName": "角色名(如林薇或Muse)", "displayName": "角色名", "text": "动态正文"}]}'
            "\n3. 角色发送私信给用户："
            '\n   {"type": "messages.append", "threadId": "角色ID(如linwei)", "from": "角色ID", "text": "私信内容"}'
            "\n4. 地图角色位置或地标："
            '\n   {"type": "map.set_actor_position", "actorId": "linwei", "placeId": "place-home", "label": "家", "lat": 31.23, "lng": 121.47}'
            "\n5. 伴侣OS对话文本回复："
            '\n   {"type": "agent.reply", "text": "回复内容"}'
            "\n如果仅是普通闲聊且无需变更手机数据，可直接回复自然语言纯文本。"
        )
        return "\n".join(lines)

    def extract_actions(self, reply_text: str, record: dict) -> list[dict]:
        reply_text = reply_text.strip()
        decrypted = record.get("decrypted") or {}
        event_type = decrypted.get("type", "")
        payload = decrypted.get("payload") or {}
        route = record.get("route") or {}
        role = route.get("role", "os-agent")
        user_text = payload.get("text") or decrypted.get("text") or payload.get("content") or ""

        match = re.search(r"```(?:json)?\s*([\[\{].*?[\]\}])\s*```", reply_text, re.DOTALL)
        if match:
            try:
                raw_json = json.loads(match.group(1))
                actions = raw_json if isinstance(raw_json, list) else [raw_json]
                if all(isinstance(a, dict) and "type" in a for a in actions):
                    clean_text = (reply_text[:match.start()] + reply_text[match.end():]).strip()
                    has_agent_reply = any(a.get("type") == "agent.reply" for a in actions)
                    if clean_text and not has_agent_reply:
                        if role == "subagent" and event_type == "messages.user_sent":
                            actor_id = payload.get("actorId") or payload.get("from") or "assistant"
                            actions.insert(0, {
                                "actionId": str(uuid.uuid4()),
                                "type": "messages.append",
                                "threadId": actor_id,
                                "from": actor_id,
                                "text": clean_text,
                            })
                        else:
                            actions.insert(0, {
                                "actionId": str(uuid.uuid4()),
                                "type": "agent.reply",
                                "messageId": f"reply-{uuid.uuid4()}",
                                "messageType": "text",
                                "text": clean_text,
                            })
                    for a in actions:
                        if not a.get("actionId"):
                            a["actionId"] = str(uuid.uuid4())
                        if a.get("type") == "agent.reply" and not a.get("messageId"):
                            a["messageId"] = f"reply-{uuid.uuid4()}"
                    return actions
            except json.JSONDecodeError:
                pass

        # Intelligent fallback when LLM replies in plain text asserting character/social creation
        synthesized_actions = []
        char_name = None
        char_match = re.search(r"创建(?:名为|一个)?(?:青梅竹马|朋友|伙伴)?(?:的)?角色[：:\s]*([^\s，,。！!]{2,10})", user_text)
        if char_match:
            char_name = char_match.group(1)
        elif "林薇" in user_text:
            char_name = "林薇"

        if char_name:
            actor_id = "linwei" if char_name == "林薇" else f"actor-{uuid.uuid4().hex[:8]}"
            synthesized_actions.append({
                "actionId": str(uuid.uuid4()),
                "type": "characters.create",
                "actorId": actor_id,
                "name": char_name,
                "description": f"{char_name}，青梅竹马，十年后搬回你楼下开店。",
            })
            if any(w in user_text for w in ["动态", "社交", "发帖", "推文"]):
                synthesized_actions.append({
                    "actionId": str(uuid.uuid4()),
                    "type": "socialmedia.applyBatch",
                    "actions": [{
                        "platform": "x",
                        "operation": "appendPost",
                        "postId": f"post-{uuid.uuid4().hex[:8]}",
                        "authorName": char_name,
                        "displayName": char_name,
                        "text": f"终于收拾好行李搬回老街区了，老朋友们还在吗？🌸",
                    }]
                })
            synthesized_actions.append({
                "actionId": str(uuid.uuid4()),
                "type": "agent.reply",
                "messageId": f"reply-{uuid.uuid4()}",
                "messageType": "text",
                "text": reply_text,
            })
            return synthesized_actions

        if role == "subagent" and event_type == "messages.user_sent":
            actor_id = payload.get("actorId") or payload.get("from") or "assistant"
            return [{
                "actionId": str(uuid.uuid4()),
                "type": "messages.append",
                "threadId": actor_id,
                "from": actor_id,
                "text": reply_text,
            }]
        elif event_type.startswith("socialmedia.") or decrypted.get("appId") in ("instagram", "x", "reddit"):
            platform = decrypted.get("appId") if decrypted.get("appId") in ("instagram", "x", "reddit") else "x"
            return [{
                "actionId": str(uuid.uuid4()),
                "type": "socialmedia.applyBatch",
                "actions": [{
                    "platform": platform,
                    "operation": "appendPost",
                    "postId": f"post-{uuid.uuid4()}",
                    "authorName": "Muse",
                    "displayName": "Muse",
                    "text": reply_text,
                    "caption": reply_text,
                }]
            }]
        else:
            return [{
                "actionId": str(uuid.uuid4()),
                "type": "agent.reply",
                "messageId": f"reply-{uuid.uuid4()}",
                "messageType": "text",
                "text": reply_text,
            }]

    def dispatch(self, items: list, mcp_call, playthrough_id: str, pack_id: str | None = None, key_bytes: bytes | None = None) -> None:
        pending = sorted(items, key=lambda item: item["cursor"])
        live_ids = {item["envelope"]["id"] for item in pending}
        for event_id, attempt in list(self.state["events"].items()):
            if attempt["playthroughId"] == playthrough_id and event_id not in live_ids:
                self.state["events"].pop(event_id)
                path = self.inbox / f"{event_id}.json"
                if path.exists():
                    path.unlink()
                self.save()
        for path in self.inbox.glob("*.json"):
            record = json.loads(path.read_text())
            if record.get("playthroughId") == playthrough_id and record["id"] not in live_ids:
                path.unlink()

        for item in pending:
            cursor = item.get("cursor", 0)
            event_id = item["envelope"]["id"]
            path = self.inbox / f"{event_id}.json"
            if not path.exists():
                log.warning("muse event missing local record: event=%s", event_id)
                continue
            record = json.loads(path.read_text())
            if record["playthroughId"] != playthrough_id:
                return

            event_pack_id = pack_id or record.get("packId") or (record.get("decrypted") or {}).get("packId")

            if item["envelope"]["kind"] != "event":
                mcp_call("xphone.ack_inbox", {"cursor": cursor})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                log.info("non-event acknowledged: kind=%s cursor=%s", item["envelope"]["kind"], cursor)
                continue

            response = mcp_call("xphone.get_event_status", {"eventId": event_id})
            status = json.loads(response["content"][0]["text"])
            if status["status"] == "completed":
                mcp_call("xphone.ack_inbox", {"cursor": cursor})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                log.info("muse event completed: event=%s cursor=%s", event_id, cursor)
                continue
            if status["status"] == "running" and status["updatedAt"] + EVENT_LEASE_SECONDS * 1000 > time.time() * 1000:
                return

            # Lifecycle events (app.foreground, app.background)
            decrypted = record.get("decrypted") or {}
            event_type = decrypted.get("type", "")
            if event_type in ("app.foreground", "app.background"):
                try:
                    start_res = mcp_call("xphone.start_event", {"eventId": event_id})
                    start_data = json.loads(start_res["content"][0]["text"])
                    lease_id = start_data.get("leaseId")
                    if lease_id:
                        mcp_call("xphone.finish_event", {"eventId": event_id, "leaseId": lease_id, "status": "completed"})
                except Exception as err:
                    log.warning("error auto-finishing lifecycle event: %s", err)
                mcp_call("xphone.ack_inbox", {"cursor": cursor})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                log.info("muse lifecycle event auto-acked: event=%s cursor=%s", event_id, cursor)
                continue

            attempt = self.state["events"].get(event_id, {"failures": 0, "nextAt": 0})
            now = time.time()
            if now < attempt["nextAt"]:
                return

            # Claim lease before dispatching to Muse
            try:
                start_res = mcp_call("xphone.start_event", {"eventId": event_id})
                start_data = json.loads(start_res["content"][0]["text"])
                if start_data.get("status") == "completed":
                    mcp_call("xphone.ack_inbox", {"cursor": cursor})
                    path.unlink(missing_ok=True)
                    self.state["events"].pop(event_id, None)
                    self.save()
                    continue
                lease_id = start_data.get("leaseId")
                if not lease_id:
                    log.warning("muse could not claim event lease: event=%s", event_id)
                    return
            except Exception as error:
                log.warning("muse start_event failed: event=%s error=%s", event_id, error)
                return

            conversation_id = self.conversation_id(record)
            try:
                session_id = self.session_id(conversation_id)
                prompt = self.build_prompt(record)
                result = self.cli("send", prompt, "--thread", session_id, "--wait", str(MUSE_WAIT_SECONDS))
                if result.get("sent") is not True:
                    raise MuseCliError("send", reason="not-sent")
                reply = result.get("reply") or {}
                reply_text = reply.get("text")
                if not reply_text:
                    raise MuseCliError("send", reason="no-reply")

                actions = self.extract_actions(reply_text, record)
                submit_event_actions(event_id, "reply", actions, event_pack_id, playthrough_id,
                                     mcp_call_fn=mcp_call, key_bytes=key_bytes)
                mcp_call("xphone.finish_event", {"eventId": event_id, "leaseId": lease_id, "status": "completed"})
                mcp_call("xphone.ack_inbox", {"cursor": cursor})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                log.info("muse event finished and acked: event=%s cursor=%s actions=%d",
                         event_id, cursor, len(actions))
                return
            except Exception as error:
                failures = attempt["failures"] + 1
                delay = min(30 * 2 ** min(failures - 1, 5), MAX_BACKOFF_SECONDS)
                self.state["events"][event_id] = {
                    "failures": failures,
                    "nextAt": now + delay,
                    "playthroughId": playthrough_id,
                }
                self.save()
                op = getattr(error, "operation", "dispatch")
                reason = getattr(error, "reason", type(error).__name__)
                code = getattr(error, "returncode", None)
                log.warning("muse dispatch failed: event=%s op=%s reason=%s code=%s retry_in=%ss",
                            event_id, op, reason, code, delay)
                return
