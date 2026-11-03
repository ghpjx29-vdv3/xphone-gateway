"""Materialize one published pack on the Muse VM for local agent reading."""

import hashlib
import json
import os
from pathlib import Path

def tool_data(mcp_call, name: str, args: dict) -> dict:
    return json.loads(mcp_call(name, args)["content"][0]["text"])


class ContentCache:
    def __init__(self, base: str | None = None):
        self.base = Path(base or Path.home() / "xphone" / "content")
        self.private = Path.home() / "xphone" / "gateway" / "private"

    def root(self, pack_id: str) -> Path:
        return self.base / pack_id

    def document_path(self, pack_id: str, reference_id: str) -> Path:
        name = (f"characters/{reference_id.removeprefix('characters.')}"
                if reference_id.startswith("characters.") else
                "world" if reference_id == "world.overview" else reference_id)
        return self.root(pack_id) / "docs" / f"{name}.md"

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, path)

    def sync(self, mcp_call, pack_id: str, playthrough_id: str) -> dict:
        manifest = tool_data(mcp_call, "runtime.get_pack_manifest", {})
        if manifest["packId"] != pack_id:
            raise RuntimeError("pack manifest scope mismatch")
        root = self.root(pack_id)
        (root / "docs").mkdir(parents=True, exist_ok=True)
        (root / "prompts").mkdir(parents=True, exist_ok=True)
        (root / playthrough_id / "tasks" / "delegations").mkdir(parents=True, exist_ok=True)
        memory = root / playthrough_id / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        for name in ("summary.md", "pins.md"):
            (memory / name).touch(exist_ok=True)
        self.private.mkdir(parents=True, exist_ok=True)
        self.private.chmod(0o700)
        local_path = root / ".manifest.json"
        previous = json.loads(local_path.read_text(encoding="utf-8")) if local_path.exists() else {}
        old_hashes = {item["id"]: item["sha256"] for item in previous.get("documents", [])}
        old_assets = {item["path"]: item["sha256"] for item in previous.get("runtimeAssets", [])}
        for item in manifest["documents"]:
            path = self.document_path(pack_id, item["id"])
            if old_hashes.get(item["id"]) == item["sha256"] and path.exists():
                continue
            content = tool_data(mcp_call, "runtime.read_document", {"referenceId": item["id"]})["content"]
            if hashlib.sha256(content.encode()).hexdigest() != item["sha256"]:
                raise RuntimeError(f"document hash mismatch: {item['id']}")
            self._write(path, content)
        for item in manifest["runtimeAssets"]:
            source = item["path"]
            if source.startswith("content/config/agent-prompts/"):
                path = root / "prompts" / source.split("agent-prompts/", 1)[1]
            else:
                path = self.private / pack_id / source
                path.parent.mkdir(parents=True, exist_ok=True)
                path.parent.chmod(0o700)
            if old_assets.get(source) == item["sha256"] and path.exists():
                continue
            content = tool_data(mcp_call, "runtime.read_asset", {"path": source})["content"]
            if hashlib.sha256(content.encode()).hexdigest() != item["sha256"]:
                raise RuntimeError(f"asset hash mismatch: {source}")
            self._write(path, content)
            if not source.startswith("content/config/agent-prompts/"):
                path.chmod(0o600)
        for app in ("notes", "world", "characters", "photos"):
            path = root / "prompts" / "subagents" / f"{app}.md"
            if not path.exists():
                self._write(path, f"你是 XPhone {app} subagent。只执行本轮派给 {app} 的任务，先读本轮允许的本地故事文档，按连接器 action schema 提交动作；使用指定 batchId，完成后回复含 taskPath 的简短结果。\n")
        templates = Path(__file__).parent / "prompts"
        published = {item["path"] for item in manifest["runtimeAssets"]}
        for source, target in (("tool-hook.md", "tool-reminder.md"),
                               ("delegation-inject.md", "delegation-inject.md")):
            if f"content/config/agent-prompts/{target}" not in published:
                self._write(root / "prompts" / target,
                            (templates / source).read_text(encoding="utf-8"))
        actions = tool_data(mcp_call, "xphone.list_app_actions",
                            {"packId": pack_id, "playthroughId": playthrough_id})
        self._write(root / "prompts" / "app-actions.json",
                    json.dumps(actions, ensure_ascii=False, indent=2))
        self._write(local_path, json.dumps(manifest, ensure_ascii=False, indent=2))
        return manifest

    def allowed_paths(self, pack_id: str, route: dict, granted_ids: list[str]) -> list[str]:
        manifest_path = self.root(pack_id) / ".manifest.json"
        if not manifest_path.exists():
            return []
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        consumer = route.get("consumer", "")
        consumers = {consumer}
        if consumer.startswith("messages."):
            consumers.add("messages")
            suffix = consumer.removeprefix("messages.").replace("_", "-").split(":")[0]
            consumers.add(f"messages.{suffix}")
            consumers.add(f"characters.{suffix}")
        granted = set(granted_ids)
        return [str(self.document_path(pack_id, item["id"]))
                for item in manifest["documents"]
                if item["id"] in granted and consumers.intersection(item.get("consumer") or [])]

    def role_prompt(self, pack_id: str, route: dict) -> str | None:
        role = "os/osagent" if route.get("role") == "os-agent" else f"subagents/{route.get('targetApp')}"
        path = self.root(pack_id) / "prompts" / f"{role}.md"
        return str(path) if path.exists() else None
