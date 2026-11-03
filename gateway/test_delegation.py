"""The Worker passes only a local task path; Muse reads the document on the VM."""

import hashlib
import json
import tempfile
import unittest
import base64
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
            task_path.write_text(f"目标: messages / 晚舟 DM\n任务: {secret_task}\n", encoding="utf-8")
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
            with self.assertRaisesRegex(ValueError, "可交办列表.*messages / 晚舟"):
                delegation.accept(str(task_path), "pack", "play")
            task_path.write_text(f"目标: messages / 晚舟\n任务: {secret_task}\n", encoding="utf-8")
            ack = delegation.accept(str(task_path), "pack", "play")
            self.assertEqual(ack["taskPath"], str(task_path))
            self.assertEqual(ack["targetApp"], "messages")
            self.assertIn(f"打开任务文档：{task_path}", sent[0])
            self.assertNotIn(secret_task, sent[0])
            self.assertNotIn(ack["batchId"], sent[0])
            action_path = task_path.with_suffix(".actions.json")
            action_path.write_text(json.dumps([{"type": "messages.append", "text": "今晚换班。"}]), encoding="utf-8")
            prepared = delegation.prepare_actions(str(task_path), "pack", "play", b"a" * 32)
            envelope = prepared["envelope"]
            self.assertEqual(envelope["header"]["batchId"], ack["batchId"])
            self.assertEqual(envelope["version"], 2)
            decode = lambda value: base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            plaintext = AESGCM(b"a" * 32).decrypt(decode(envelope["nonce"]),
                                                   decode(envelope["ciphertext"]), decode(envelope["aad"]))
            self.assertEqual(json.loads(plaintext)["actions"][0]["text"], "今晚换班。")
            self.assertNotIn("batchId", json.loads(plaintext))
            tampered = json.loads(decode(envelope["aad"]))
            tampered["header"]["targetApp"] = "notes"
            with self.assertRaises(Exception):
                AESGCM(b"a" * 32).decrypt(decode(envelope["nonce"]), decode(envelope["ciphertext"]),
                                           json.dumps(tampered, ensure_ascii=False, separators=(",", ":")).encode())
            self.assertIn("messages / 晚舟", dispatcher.context.prompt(record, "pack", "play", "batch-1", {}, False))
            with self.assertRaisesRegex(RuntimeError, "已交办"):
                delegation.accept(str(task_path), "pack", "play")
            self.assertEqual([name for name, _ in calls].count("runtime.read_document"), 1)

            delegation.poll(mcp, "pack", "play")
            self.assertIn("已完成", dispatcher.memory.view("pack", "play", {
                "role": "subagent", "surfaceId": "messages.dm.wan-zhou"})["surface"])
            self.assertTrue((task_root / "log.jsonl").exists())

    def test_journal_event_file_seals_frame_and_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            inbox = base / "inbox"
            inbox.mkdir()
            dispatcher = MuseDispatch(str(inbox), str(base / "state.json"))
            event_id = "11111111-1111-4111-8111-111111111111"
            record = {"id": event_id, "packId": "pack", "playthroughId": "play",
                      "route": {"role": "os-agent", "windowId": "window"},
                      "decrypted": {"type": "journal.main_record.requested", "tickId": "tick-1",
                                    "payload": {"recordId": "record-1", "streamId": "stream-1"}}}
            (inbox / f"{event_id}.json").write_text(json.dumps(record), encoding="utf-8")
            from muse_delivery import batch_id
            batch = batch_id("pack", "play", event_id, 0)
            dispatcher.state["events"][event_id] = {"batchId": batch, "step": 0}
            root = base / "content" / "pack" / "play" / "tasks" / "events" / event_id
            root.mkdir(parents=True)
            task = root / f"{event_id}.md"
            task.write_text("任务: 写日记", encoding="utf-8")
            task.with_suffix(".actions.json").write_text(
                json.dumps([{"type": "journal.appendMainRecord", "markdown": "# 今夜"}]), encoding="utf-8")
            prepared = Delegation(dispatcher, base / "content").prepare_actions(
                str(task), "pack", "play", b"a" * 32)
            self.assertEqual(prepared["envelope"]["header"]["origin"], "event")
            self.assertEqual(prepared["envelope"]["header"]["batchId"], batch)
            self.assertEqual(prepared["frame"]["phase"], "start")


if __name__ == "__main__":
    unittest.main()
