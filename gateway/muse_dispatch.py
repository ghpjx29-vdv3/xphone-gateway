"""Send retained XPhone inbox events to dedicated Muse side chats, collect replies, and submit actions via MCP."""

import json
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path


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

    def build_prompt(self, record: dict, pack_id: str, playthrough_id: str) -> str:
        decrypted = record.get("decrypted") or {}
        event_type = decrypted.get("type", "")
        payload = decrypted.get("payload") or {}
        route = record.get("route") or {}
        role = route.get("role", "os-agent")
        lines = [
            f"收到 XPhone 客户端事件 (角色: {role}, 类型: {event_type}):",
        ]
        text = payload.get("text") or decrypted.get("text") or payload.get("content")
        if text:
            lines.append(f"用户/系统内容: {text}")
        lines.append(f"详细负载: {json.dumps(payload, ensure_ascii=False)}")
        lines.append(
            "\n"
            "\n你正在作为 XPhone 系统的伴侣 Agent 操纵用户的虚拟手机。你有直接调用 MCP 的能力。"
            "\n\n当用户请求执行手机系统动作时（如创建角色、发社交动态、私信、地图定位等），"
            "你通过 custom.xphone-agent-worker 连接器直接提交，不要只在回复里用文字声称“已完成”。"
            "\n\n操作步骤："
            "\n1. 用 muse.connector_search 找到 custom.xphone-agent-worker（如果还没加载）。"
            "\n2. 构造 action 列表。支持的类型（16 种）："
            '\n   os-agent (contacts):'
            '\n   - {"type": "characters.create", "actorId": "英文ID", "name": "名", "description": "人设"}'
            '\n   - {"type": "characters.update", "actorId": "已有ID", "name": "名", "description": "人设"}'
            '\n   os-agent (photos):'
            '\n   - {"type": "photos.import", "photoId": "唯一ID", "imageUrl": "图片URL"}'
            '\n   os-agent (maps):'
            '\n   - {"type": "map.replace_catalogue", "catalogue": "{city,center,zones,places}"}'
            '\n   os-agent (messages):'
            '\n   - {"type": "agent.reply", "messageId": "唯一ID", "text": "回复内容"}'
            '\n   os-agent (notes):'
            '\n   - {"type": "notes.create",...} / {"type": "notes.update",...}'
            '\n   os-agent (world):'
            '\n   - {"type": "world.applyStatePatch",...}'
            '\n   messages subagent:'
            '\n   - {"type": "messages.append", "threadId": "线程ID", "from": "发送者", "text": "内容"}'
            '\n   maps subagent:'
            '\n   - {"type": "map.set_actor_position", "actorId": "ID", "placeId": "地点ID", "lat": 31.23, "lng": 121.47}'
            '\n   - {"type": "map.place_item", "itemId": "ID", "placeId": "地点ID", "name": "名"}'
            '\n   - {"type": "map.remove_item", "itemId": "ID"}'
            '\n   - {"type": "map.applyevent",...} / {"type": "map.set_place_image",...}'
            '\n   socialmedia subagent:'
            '\n   - {"type": "socialmedia.applyBatch", "actions": [{"platform": "x", "operation": "appendPost", "postId": "ID", "authorName": "名", "text": "正文"}]}'
            "\n3. 加密并提交："
            "\n   - 从 ~/.config/xphone/content.key 读取 32 字节密钥（base64url）。"
            "\n   - 用 Python + cryptography 库 AES-256-GCM 加密："
            f"\n     packId={pack_id}, playthroughId={playthrough_id}"
            "\n     plaintext = JSON({\"batchId\": <uuid>, \"actions\": [...]})"
            "\n     aad = JSON({\"v\":1,\"packId\":...,\"playthroughId\":...,\"direction\":\"client\",\"kind\":\"action_batch\",\"id\":<batchId>})"
            "\n     nonce = 12 字节随机；ciphertext = AESGCM(key).encrypt(nonce, plaintext, aad)"
            "\n     envelope = {\"id\": batchId, \"kind\":\"action_batch\", \"version\":1,"
            " \"nonce\": b64u(nonce), \"ciphertext\": b64u(ct), \"aad\": b64u(aad)}（b64u = base64url 去掉末尾 =）"
            "\n   - 写 /tmp/xphone_args.json：{\"envelope\": <envelope>}"
            "\n   - 执行：python3 ~/workspace/custom-connectors/custom.xphone-agent-worker/bin/client.py"
            " call-tool --name xphone.submit_action_batch --arguments-file /tmp/xphone_args.json"
            "\n4. 回复用户自然语言总结执行结果，在回复里输出 JSON 代码块。"
            "\n\n如果仅是普通闲聊无需变更手机数据，直接回复自然语言即可，无需调 MCP。"
        )
        return "\n".join(lines)

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
                prompt = self.build_prompt(record, event_pack_id, playthrough_id)
                result = self.cli("send", prompt, "--thread", session_id, "--wait", str(MUSE_WAIT_SECONDS))
                if result.get("sent") is not True:
                    raise MuseCliError("send", reason="not-sent")
                reply = result.get("reply") or {}
                reply_text = reply.get("text")
                if not reply_text:
                    raise MuseCliError("send", reason="no-reply")

                # Side chat submits actions directly via connector; gateway only manages lease.
                mcp_call("xphone.finish_event", {"eventId": event_id, "leaseId": lease_id, "status": "completed"})
                mcp_call("xphone.ack_inbox", {"cursor": cursor})
                path.unlink(missing_ok=True)
                self.state["events"].pop(event_id, None)
                self.save()
                log.info("muse event finished and acked: event=%s cursor=%s (actions via connector)",
                         event_id, cursor)
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
