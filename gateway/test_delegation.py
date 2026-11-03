"""The Worker passes only a local task path; Muse reads the document on the VM."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from content_cache import ContentCache
from delegation import Delegation
from muse_dispatch import MuseDispatch


def result(value):
    return {"content": [{"text": json.dumps(value, ensure_ascii=False)}]}


class DelegationTests(unittest.TestCase):
    def test_local_document_delegation_and_memory_result(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            cache = ContentCache(str(base / "content"))
            cache.private = base / "private"
            world = "# 世界观\n码头在城西。"
            manifest = {"packId": "pack", "version": "1", "documents": [
                {"id": "world.overview", "sha256": hashlib.sha256(world.encode()).hexdigest(),
                 "consumer": ["os", "messages"]}], "runtimeAssets": []}
            calls = []

            def mcp(name, args):
                calls.append((name, args))
                if name == "runtime.get_pack_manifest":
                    return result(manifest)
                if name == "runtime.read_document":
                    return result({"content": world})
                if name == "xphone.list_app_actions":
                    return result({"actions": [{"type": "messages.append"}]})
                if name == "xphone.get_delivery":
                    return result({"status": "applied"})
                raise AssertionError(name)

            cache.sync(mcp, "pack", "play")
            self.assertEqual((cache.root("pack") / "docs" / "world.md").read_text(encoding="utf-8"), world)
            self.assertTrue((cache.root("pack") / "prompts" / "tool-reminder.md").exists())
            self.assertTrue((cache.root("pack") / "play" / "memory" / "pins.md").exists())

            inbox = base / "inbox"
            inbox.mkdir()
            dispatcher = MuseDispatch(str(inbox), str(base / "state.json"))
            dispatcher.context.cache = cache
            record = {"id": "event-1", "packId": "pack", "playthroughId": "play",
                      "route": {"role": "os-agent", "roleId": "osAgent:window", "windowId": "window",
                                "consumer": "os", "surfaceId": "agent.control"},
                      "decrypted": {"type": "os.user_message", "actors": [
                          {"actorId": "wan-zhou", "name": "晚舟"}],
                          "grantedReferenceIds": ["world.overview"]}}
            (inbox / "event-1.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
            task_root = cache.root("pack") / "play" / "tasks" / "delegations"
            task_path = task_root / "event-1" / "123e4567-e89b-42d3-a456-426614174000.md"
            task_path.parent.mkdir()
            secret_task = "让晚舟告诉用户码头今晚换班。"
            task_path.write_text(f"父事件: event-1\n目标: messages / 晚舟私信\n任务: {secret_task}\n", encoding="utf-8")
            sent = []

            def cli(*args):
                if args[0] == "session-start":
                    return {"session_id": "messages-session"}
                if args[0] == "send":
                    sent.append(args[1])
                    return {"sent": True}
                if args[0] == "history":
                    return {"messages": [{"role": "assistant", "text": f"已完成 {task_path}"}]}
                raise AssertionError(args)

            dispatcher.cli = cli
            delegation = Delegation(dispatcher, cache.base)
            ack = delegation.accept(str(task_path), "pack", "play")
            self.assertEqual(ack["taskPath"], str(task_path))
            self.assertEqual(ack["targetApp"], "messages")
            self.assertIn(f"打开任务文档：{task_path}", sent[0])
            self.assertNotIn(secret_task, sent[0])
            self.assertEqual([name for name, _ in calls].count("runtime.read_document"), 1)

            delegation.poll(mcp, "pack", "play")
            self.assertIn("已完成", dispatcher.memory.view("pack", "play", {
                "role": "subagent", "surfaceId": "messages.dm.wan-zhou"})["surface"])
            self.assertTrue((task_root / "log.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
