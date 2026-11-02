"""Build role-sized Muse inputs from the current client event and Worker tools."""

import json
import re
from pathlib import Path


PROMPTS = {
    "os-agent": "content/config/agent-prompts/os/osagent.md",
    "messages": "content/config/agent-prompts/subagents/messages.md",
    "socialmedia": "content/config/agent-prompts/subagents/socialmedia.md",
    "maps": "content/config/agent-prompts/subagents/maps.md",
}


def tool_data(mcp_call, name: str, args: dict) -> dict:
    return json.loads(mcp_call(name, args)["content"][0]["text"])


class MuseContext:
    def __init__(self):
        self.assets: dict[tuple[str, str], str] = {}
        self.paths: dict[str, set[str]] = {}
        self.versions: dict[str, str] = {}

    def asset(self, pack_id: str, path: str, mcp_call) -> str:
        if path not in self.paths[pack_id]:
            return ""
        key = (pack_id, path)
        if key not in self.assets:
            self.assets[key] = tool_data(mcp_call, "runtime.read_asset", {"path": path})["content"]
        return self.assets[key]

    def expand(self, pack_id: str, content: str, mcp_call) -> str:
        def include(match):
            path = "content/config/agent-prompts/" + match.group(1)
            return self.asset(pack_id, path, mcp_call)
        return re.sub(r"\{\{include:([^}]+)\}\}", include, content)

    def prompt(self, record: dict, pack_id: str, playthrough_id: str, batch_id: str,
               memory: dict, mcp_call, rejected_batch: str | None = None) -> str:
        event = record["decrypted"]
        route = record["route"]
        role = route["role"]
        app = route.get("targetApp", "os-agent")
        index = tool_data(mcp_call, "runtime.list_assets", {})
        if self.versions.get(pack_id) != index["version"]:
            self.assets = {key: value for key, value in self.assets.items() if key[0] != pack_id}
            self.versions[pack_id] = index["version"]
        self.paths[pack_id] = set(index["paths"])
        references = tool_data(mcp_call, "story.get_context", {"eventId": record["id"],
                                                                   "consumer": route["consumer"]}).get("references", [])
        base_prompt = self.expand(pack_id, self.asset(pack_id, PROMPTS[app], mcp_call), mcp_call)
        base_prompt = re.sub(r"After the tool call, reply exactly:\s*\{\}", "", base_prompt)
        base_prompt = base_prompt.replace("submit_app_actions", "xphone.submit_action_batch")
        base_prompt = base_prompt.replace("{{runtimePolicyInstructions}}",
                                          json.dumps(event.get("runtimePolicy") or {}, ensure_ascii=False))
        base_prompt = base_prompt.replace("{{knownInformation}}", "\n".join(
            f"- {ref['title']}: {ref['summary']}" for ref in references) or "- none")
        base_prompt = base_prompt.replace("{{unknownInformation}}", "- none")
        actions = tool_data(mcp_call, "xphone.list_app_actions",
                            {"packId": pack_id, "playthroughId": playthrough_id})["actions"]
        permitted = [action for action in actions if action["role"] == role or
                     (role == "subagent" and action["role"] == f"{app} subagent") or
                     (role == "os-agent" and action["role"].startswith("os-agent"))]
        payload = event.get("payload") or {}
        current = {
            "eventId": record["id"], "batchId": batch_id, "consumer": route["consumer"],
            "type": event.get("type"), "appId": event.get("appId"),
            "surfaceId": event.get("surfaceId"), "windowId": event.get("windowId"),
            "userInput": {key: payload[key] for key in ("text", "messageId", "actorId", "postId", "requestKind",
                                                       "platform", "action", "placeId", "item", "location", "noteId") if key in payload},
            "surfaceSummary": event.get("surfaceSummary"), "sharedMemory": memory,
            "recentLedger": event.get("recentLedger", []),
        }
        if rejected_batch:
            current["previousRejectedBatchId"] = rejected_batch
        if role == "os-agent":
            current.update({"windowMemory": event.get("windowMemory"), "actors": event.get("actors", []),
                            "recentControlMessages": event.get("recentControlMessages", []),
                            "notes": event.get("notes", [])})
        elif app == "messages":
            current["actors"] = [actor for actor in event.get("actors", [])
                                 if actor.get("actorId") == payload.get("actorId")]
        instructions = (Path(__file__).parent / "prompts" / "agent-turn.md").read_text(encoding="utf-8")
        return "\n\n".join([base_prompt, instructions,
                           "当前事件与共享上下文：" + json.dumps(current, ensure_ascii=False),
                           "可见文档索引：" + json.dumps(references, ensure_ascii=False),
                           "本角色 action schema：" + json.dumps(permitted, ensure_ascii=False)])
