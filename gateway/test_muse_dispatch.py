"""Muse delivery completion and role context contracts."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from muse_context import MuseContext
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
        first = context.prompt(self.record, "pack", "play", "batch", {}, True)
        followup = context.prompt(self.record, "pack", "play", "batch", {}, False)
        self.assertIn("connector_search", first)
        self.assertIn("当前事件与共享上下文", followup)
        self.assertIn("story.get_context", followup)
        self.assertIn("xphone.submit_action_batch", followup)
        self.assertNotIn("## P0 必读", followup)
        self.assertNotIn("threadId", followup)
        self.assertLess(len(followup), len(first) * 0.3)


if __name__ == "__main__":
    unittest.main()
