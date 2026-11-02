"""Send retained XPhone inbox events to dedicated Muse side chats."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path


log = logging.getLogger("xphone-gateway")
RETRY_SECONDS = 120
MAX_BACKOFF_SECONDS = 600
EVENT_LEASE_SECONDS = 90


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
        try:
            result = subprocess.run(
                [env.get("XPHONE_MUSE_CLI", "muse-cli"), *args],
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
        decrypted = record["decrypted"]
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

    def dispatch(self, items: list, mcp_call, playthrough_id: str) -> None:
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
            event_id = item["envelope"]["id"]
            path = self.inbox / f"{event_id}.json"
            if not path.exists():
                log.warning("muse event missing local record: event=%s", event_id)
                return
            record = json.loads(path.read_text())
            if record["playthroughId"] != playthrough_id:
                return
            if item["envelope"]["kind"] == "event":
                response = mcp_call("xphone.get_event_status", {"eventId": event_id})
                status = json.loads(response["content"][0]["text"])
                if status["status"] == "completed":
                    mcp_call("xphone.ack_inbox", {"cursor": item["cursor"]})
                    path.unlink()
                    self.state["events"].pop(event_id, None)
                    self.save()
                    log.info("muse event completed: event=%s cursor=%s", event_id, item["cursor"])
                    continue
                if status["status"] == "running" and status["updatedAt"] + EVENT_LEASE_SECONDS * 1000 > time.time() * 1000:
                    return

            attempt = self.state["events"].get(event_id, {"failures": 0, "nextAt": 0})
            now = time.time()
            if now < attempt["nextAt"]:
                return
            conversation_id = self.conversation_id(record)
            try:
                session_id = self.session_id(conversation_id)
                instruction = ("Process the XPhone event. Read it through the existing XPhone MCP, decrypt locally, "
                               "write the reply through XPhone MCP, then finish and acknowledge it. "
                               + json.dumps({"conversation_id": conversation_id, "event_id": event_id}, ensure_ascii=False))
                result = self.cli("send", instruction, "--thread", session_id, "--wait", "0")
                if result.get("sent") is not True:
                    raise MuseCliError("send")
            except MuseCliError as error:
                failures = attempt["failures"] + 1
                delay = min(30 * 2 ** min(failures - 1, 5), MAX_BACKOFF_SECONDS)
                self.state["events"][event_id] = {"failures": failures, "nextAt": now + delay,
                                                   "playthroughId": playthrough_id}
                self.save()
                log.warning("muse wake failed: event=%s operation=%s reason=%s code=%s retry=%ss",
                            event_id, error.operation, error.reason, error.returncode, delay)
                return
            self.state["events"][event_id] = {"failures": 0, "nextAt": now + RETRY_SECONDS,
                                               "playthroughId": playthrough_id}
            self.save()
            log.info("muse wake sent: event=%s conversation=%s", event_id, conversation_id)
            return
