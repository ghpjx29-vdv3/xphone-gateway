"""Muse delivery completion and role context contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muse_context import MuseContext, PROMPT_DIR
from muse_delivery import batch_id
from muse_dispatch import MuseDispatch


def result(value):
    return {"content": [{"text": json.dumps(value)}]}


class MuseDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.dispatcher = MuseDispatch(str(self.inbox), str(Path(self.tmp.name) / "state.json"))
        self.dispatcher.context.cache.base = Path(self.tmp.name) / "content"
        prompts = self.dispatcher.context.cache.root("pack") / "prompts"
        prompts.mkdir(parents=True)
        for source, target in (("tool-hook.md", "tool-reminder.md"),
                               ("delegation-inject.md", "delegation-inject.md")):
            (prompts / target).write_text((PROMPT_DIR / source).read_text(encoding="utf-8"), encoding="utf-8")
        self.event_id = "event-1"
        self.item = {"cursor": 1, "envelope": {"id": self.event_id, "kind": "event"}}
        self.record = {"id": self.event_id, "packId": "pack", "playthroughId": "play",
                       "route": {"role": "subagent", "roleId": "messages:alice", "consumer": "messages.alice",
                                 "targetApp": "messages", "surfaceId": "messages.dm.alice"},
                       "decrypted": {"type": "messages.user_sent", "payload": {"actorId": "alice", "text": "hi"}}}
        (self.inbox / f"{self.event_id}.json").write_text(json.dumps(self.record), encoding="utf-8")
        self.calls = []
        self.status = "unknown"

    def mcp(self, name, args):
        self.calls.append((name, args))
        if name == "xphone.get_event_status":
            return result({"status": "accepted"})
        if name == "xphone.start_event":
            return result({"status": "running", "claimed": True, "leaseId": "lease"})
        if name == "xphone.get_delivery":
            return result({"status": self.status})
        return result({"ok": True})

    def test_applied_batch_completes_without_structured_reply(self):
        batch = batch_id("pack", "play", self.event_id)
        self.dispatcher.context.prompt = lambda *_, **__: "prompt"

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": "sidechat"}
            if args[0] == "send":
                self.assertIn("--thread", args)
                self.status = "applied"
                return {"sent": True, "reply": {"text": "角色已回复。"}}
            raise AssertionError(args)

        self.dispatcher.cli = cli
        self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        self.assertEqual([name for name, _ in self.calls if name == "xphone.finish_event"], ["xphone.finish_event"])
        self.assertEqual([args["batchId"] for name, args in self.calls if name == "xphone.get_delivery"], [batch, batch])
        self.assertFalse((self.inbox / "event-1.json").exists())
        view = self.dispatcher.memory.view("pack", "play", self.record["route"])
        self.assertEqual(view["surface"], "角色已回复。")
        self.assertEqual(view["ledger"][0]["source"], "reply_text")

    def test_unknown_delivery_keeps_event_and_backoff(self):
        self.dispatcher.context.prompt = lambda *_, **__: "prompt"
        self.dispatcher.cli = lambda *args: ({"session_id": "sidechat"} if args[0] == "session-start"
                                      else {"sent": True, "reply": {"text": "done"}})
        with patch("muse_dispatch.DELIVERY_WAIT_SECONDS", 0):
            self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        self.assertTrue((self.inbox / "event-1.json").exists())
        self.assertFalse(any(name == "xphone.finish_event" for name, _ in self.calls))
        self.assertEqual(self.dispatcher.state["events"][self.event_id]["failures"], 1)

    def test_rejected_batch_advances_to_new_deterministic_step(self):
        self.status = "rejected"
        self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        attempt = self.dispatcher.state["events"][self.event_id]
        self.assertEqual(attempt["step"], 1)
        self.assertEqual(attempt["rejectedBatchId"], batch_id("pack", "play", self.event_id))
        self.assertFalse(any(name == "xphone.finish_event" for name, _ in self.calls))
        attempt["nextAt"] = 0
        self.status = "applied"
        self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        batches = [args["batchId"] for name, args in self.calls if name == "xphone.get_delivery"]
        self.assertEqual(batches[-1], batch_id("pack", "play", self.event_id, 1))
        self.assertTrue(any(name == "xphone.finish_event" for name, _ in self.calls))

    def test_lifecycle_finishes_without_muse(self):
        self.record["decrypted"]["type"] = "app.foreground"
        (self.inbox / "event-1.json").write_text(json.dumps(self.record), encoding="utf-8")
        self.dispatcher.cli = lambda *_: self.fail("Muse should not start")
        self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        self.assertTrue(any(name == "xphone.finish_event" for name, _ in self.calls))

    def test_subagent_followup_keeps_event_and_hook_without_role_prompt(self):
        context = MuseContext()
        context.cache.base = self.dispatcher.context.cache.base
        first = context.prompt(self.record, "pack", "play", "batch", {}, True)
        followup = context.prompt(self.record, "pack", "play", "batch", {}, False)
        self.assertIn("connector_search", first)
        self.assertIn("当前事件与共享上下文", followup)
        self.assertIn("readableDocuments", followup)
        self.assertIn("xphone.submit_action_batch", followup)
        self.assertNotIn("## P0 必读", followup)
        self.assertNotIn("threadId", followup)
        self.assertLess(len(followup), len(first))

    def test_journal_orchestration_finishes_after_delegation_ack(self):
        journal_event_id = "journal-event-1"
        item = {"cursor": 2, "envelope": {"id": journal_event_id, "kind": "event"}}
        record = {
            "id": journal_event_id, "packId": "pack", "playthroughId": "play",
            "route": {"role": "os-agent", "roleId": "osAgent:main", "consumer": "os",
                      "targetApp": "g5_dashboard", "surfaceId": "journal.main_record"},
            "decrypted": {"type": "journal.main_record.requested", "payload": {},
                          "actors": [{"name": "晚舟", "actorId": "wan_zhou:role"}]}
        }
        (self.inbox / f"{journal_event_id}.json").write_text(json.dumps(record), encoding="utf-8")

        sent_prompts = []
        batch_0 = batch_id("pack", "play", journal_event_id, 0)
        batch_1 = batch_id("pack", "play", journal_event_id, 1)

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": "os-session"}
            if args[0] == "send":
                sent_prompts.append(args[1])
                if len(sent_prompts) == 2:
                    self.dispatcher.state["events"][journal_event_id]["delegations"] = ["/task.md"]
                return {"sent": True, "reply": {"text": "正文完成" if len(sent_prompts) == 1 else "晚舟发了私信"}}
            if args[0] == "session-delete":
                return {"ok": True}
            raise AssertionError(args)

        self.dispatcher.cli = cli

        status_map = {batch_0: "unknown", batch_1: "unknown"}

        def mcp(name, args):
            self.calls.append((name, args))
            if name == "xphone.get_event_status":
                return result({"status": "accepted"})
            if name == "xphone.start_event":
                return result({"status": "running", "claimed": True, "leaseId": "lease"})
            if name == "xphone.get_delivery":
                bid = args["batchId"]
                # Once send is invoked, mark batch as applied
                if len(sent_prompts) >= 1 and bid == batch_0:
                    status_map[batch_0] = "applied"
                return result({"status": status_map.get(bid, "unknown")})
            return result({"ok": True})

        self.dispatcher.dispatch([item], mcp, "play", pack_id="pack")
        self.assertEqual(len(sent_prompts), 2)
        self.assertIn("Phase 1", sent_prompts[0])
        self.assertIn("Phase 2", sent_prompts[1])
        finish_calls = [args for name, args in self.calls if name == "xphone.finish_event"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
