"""Muse delivery completion and role context contracts."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from muse_context import MuseContext, PROMPT_DIR
from muse_delivery import batch_id
from muse_dispatch import MuseDispatch, LeaseRenewal


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

    def test_lease_renewal_retries_transient_failures(self):
        calls = []

        def renew(name, args):
            calls.append((name, args))
            if len(calls) < 3:
                raise ConnectionError('temporary outage')

        renewal = LeaseRenewal(renew, 'event-1', 'lease', None)
        with patch('muse_dispatch.time.monotonic', side_effect=[0, 30, 30, 35, 35, 40, 40]), patch.object(
            renewal.stop_signal, 'wait', side_effect=[False, False, False, True]
        ):
            renewal.run()
        self.assertFalse(renewal.lost)
        self.assertEqual(len(calls), 3)

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

    def test_os_reply_applied_keeps_parent_leased_until_delegation_applies(self):
        self.record["route"] = {"role": "os-agent", "roleId": "osAgent:main", "consumer": "os",
                                "surfaceId": "agent.reply"}
        (self.inbox / f"{self.event_id}.json").write_text(json.dumps(self.record), encoding="utf-8")
        self.status = "applied"
        event_status = {"status": "accepted", "delegationStatus": "pending"}

        def mcp(name, args):
            if name == "xphone.get_event_status":
                return result(event_status)
            return self.mcp(name, args)

        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertFalse(any(name in ("xphone.finish_event", "xphone.ack_inbox") for name, _ in self.calls))
        self.dispatcher.state["events"][self.event_id]["nextAt"] = 0
        event_status.update(status="running", updatedAt=int(time.time() * 1000))
        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertFalse(any(name == "xphone.finish_event" for name, _ in self.calls))
        event_status["delegationStatus"] = "applied"
        self.dispatcher.state["events"][self.event_id]["nextAt"] = 0
        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        names = [name for name, _ in self.calls]
        self.assertEqual(names.count("xphone.start_event"), 1)
        self.assertEqual(names.count("xphone.renew_event"), 2)
        self.assertEqual(names[-2:], ["xphone.finish_event", "xphone.ack_inbox"])

    def test_applied_delegation_does_not_close_a_user_turn_without_its_reply(self):
        self.record["route"] = {"role": "os-agent", "roleId": "osAgent:main", "consumer": "os",
                                "surfaceId": "agent.reply"}
        self.record["decrypted"]["type"] = "os.user_message"
        (self.inbox / f"{self.event_id}.json").write_text(json.dumps(self.record), encoding="utf-8")
        attempt = {"museStarted": True, "museSent": True, "failures": 0, "nextAt": 0}
        self.dispatcher.state["events"][self.event_id] = attempt
        event_status = {"status": "accepted", "delegationStatus": "applied"}

        def mcp(name, args):
            if name == "xphone.get_event_status":
                return result(event_status)
            return self.mcp(name, args)

        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertFalse(any(name in ("xphone.finish_event", "xphone.ack_inbox") for name, _ in self.calls))
        self.assertTrue(attempt["waitingDelegations"])
        self.status = "applied"
        attempt["nextAt"] = 0
        event_status.update(status="running", updatedAt=int(time.time() * 1000))
        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertEqual([name for name, _ in self.calls][-2:], ["xphone.finish_event", "xphone.ack_inbox"])

    def test_unknown_delivery_keeps_event_and_backoff(self):
        self.dispatcher.context.prompt = lambda *_, **__: "prompt"
        sent = []

        def cli(*args):
            if args[0] == 'send':
                sent.append(args)
                return {"sent": True, "reply": {"text": "done"}}
            return {"session_id": "sidechat"}

        self.dispatcher.cli = cli
        with patch("muse_dispatch.DELIVERY_WAIT_SECONDS", 0):
            self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
            self.dispatcher.state['events'][self.event_id]['nextAt'] = 0
            self.dispatcher.dispatch([self.item], self.mcp, "play", pack_id="pack")
        self.assertTrue((self.inbox / "event-1.json").exists())
        self.assertFalse(any(name == "xphone.finish_event" for name, _ in self.calls))
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.dispatcher.state["events"][self.event_id]["failures"], 2)

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
        self.assertIn("【本轮最新任务｜先处理这里】", followup)
        self.assertIn("可读故事资料", followup)
        self.assertIn("xphone.submit_task_actions", followup)
        self.assertNotIn("## 本轮开始时确认", followup)
        self.assertNotIn("threadId", followup)
        self.assertLess(len(followup), len(first))

    def test_os_prompt_separates_latest_message_from_old_chat_and_noisy_memory(self):
        context = self.dispatcher.context
        record = {**self.record,
                  "route": {"role": "os-agent", "roleId": "osAgent:main", "surfaceId": "agent.control"},
                  "decrypted": {"type": "os.user_message", "appId": "agent",
                                "payload": {"messageId": "new", "text": "这次最新的问题"},
                                "recentControlMessages": [
                                    {"id": "new", "role": "user", "text": "这次最新的问题", "timestamp": 3},
                                    {"id": "older-2", "role": "assistant", "text": "上一轮回答", "timestamp": 2},
                                    {"id": "older-1", "role": "user", "text": "更早的问题", "timestamp": 1},
                                ]}}
        memory = self.dispatcher.memory
        memory.commit("pack", "play", "old-1", record["route"], ["batch-1"], "。", "reply_text")
        memory.commit("pack", "play", "old-2", record["route"], ["batch-2"], "晚舟去了码头。", "reply_text")
        memory.commit("pack", "play", "old-3", record["route"], ["batch-3"], "晚舟去了码头。", "reply_text")
        prompt = context.prompt(record, "pack", "play", "batch", memory.view("pack", "play", record["route"]), False)
        self.assertEqual(prompt.count("这次最新的问题"), 1)
        self.assertLess(prompt.index("这次最新的问题"), prompt.index("【之前的对话"))
        self.assertLess(prompt.index("更早的问题"), prompt.index("上一轮回答"))
        self.assertEqual(prompt.count("晚舟去了码头。"), 1)
        self.assertNotIn("个人代理：。", prompt)
        self.assertNotIn('"packId"', prompt)

    def test_journal_orchestration_finishes_when_async_delegation_applies(self):
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
        child_batch = batch_id("pack", "play", journal_event_id, 2)

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": "os-session"}
            if args[0] == "send":
                sent_prompts.append(args[1])
                if len(sent_prompts) == 2:
                    self.dispatcher.state["events"][journal_event_id]["delegations"] = ["/task.md"]
                    self.dispatcher.state.setdefault("delegations", {})["/task.md"] = {"batchId": child_batch}
                return {"sent": True, "reply": {"text": "正文完成" if len(sent_prompts) == 1 else "晚舟发了私信"}}
            if args[0] == "session-delete":
                return {"ok": True}
            raise AssertionError(args)

        self.dispatcher.cli = cli

        status_map = {batch_0: "unknown", batch_1: "unknown", child_batch: "queued"}
        event_status = {"status": "accepted"}

        def mcp(name, args):
            self.calls.append((name, args))
            if name == "xphone.get_event_status":
                return result(event_status)
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
        self.assertFalse(any(name == "xphone.finish_event" for name, _ in self.calls))
        self.assertEqual(self.dispatcher.state["events"][journal_event_id]["failures"], 0)
        status_map[child_batch] = "applied"
        self.dispatcher.on_receipt(child_batch)
        event_status.update({"status": "running", "updatedAt": int(time.time() * 1000)})
        self.dispatcher.dispatch([item], mcp, "play", pack_id="pack")
        self.assertEqual(len(sent_prompts), 2)
        finish_calls = [args for name, args in self.calls if name == "xphone.finish_event"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0]["status"], "completed")

    def test_inflight_journal_recovers_after_all_delegations_applied(self):
        self.record["route"] = {"role": "os-agent", "roleId": "osAgent:main", "consumer": "os",
                                "surfaceId": "journal.main_record"}
        self.record["decrypted"]["type"] = "journal.main_record.requested"
        (self.inbox / f"{self.event_id}.json").write_text(json.dumps(self.record), encoding="utf-8")
        first = batch_id("pack", "play", self.event_id, 0)
        child = batch_id("pack", "play", self.event_id, 2)
        self.dispatcher.state["events"][self.event_id] = {
            "step": 1, "batchId": batch_id("pack", "play", self.event_id, 1),
            "delegations": ["/task.md"], "failures": 1, "nextAt": 0, "replyText": "正文完成"}
        self.dispatcher.state["delegations"] = {"/task.md": {"batchId": child, "status": "applied"}}
        self.dispatcher.cli = lambda *_: self.fail("已完成派工不应再次唤起 Muse")

        def mcp(name, args):
            self.calls.append((name, args))
            if name == "xphone.get_delivery":
                return result({"status": "applied" if args["batchId"] in (first, child) else "unknown"})
            if name == "xphone.get_event_status":
                return result({"status": "running", "updatedAt": int(time.time() * 1000) - 100_000})
            if name == "xphone.start_event":
                return result({"claimed": True, "leaseId": "new-lease"})
            return result({"ok": True})

        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertEqual([name for name, _ in self.calls].count("xphone.finish_event"), 1)
        self.assertFalse((self.inbox / f"{self.event_id}.json").exists())


    def test_journal_retry_reuses_applied_first_batch_without_generating_duplicate_phase1(self):
        self.record["route"] = {"role": "os-agent", "roleId": "osAgent:main", "consumer": "os",
                                "surfaceId": "journal.main_record"}
        self.record["decrypted"]["type"] = "journal.main_record.requested"
        (self.inbox / f"{self.event_id}.json").write_text(json.dumps(self.record), encoding="utf-8")
        first_batch = batch_id("pack", "play", self.event_id, 0)
        sent_prompts = []

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": "os-session"}
            if args[0] == "send":
                sent_prompts.append(args[1])
                return {"sent": True, "reply": {"text": "无需联动"}}
            if args[0] == "session-delete":
                return {"ok": True}
            raise AssertionError(args)

        self.dispatcher.cli = cli

        def mcp(name, args):
            self.calls.append((name, args))
            if name == "xphone.get_event_status":
                return result({"status": "running", "updatedAt": int(time.time() * 1000) - 100_000})
            if name == "xphone.start_event":
                return result({"claimed": True, "leaseId": "lease-1"})
            if name == "xphone.get_delivery":
                return result({"status": "applied" if args["batchId"] == first_batch else "unknown"})
            return result({"ok": True})

        # Even with step=0 in state (or empty state), because first_batch is already applied,
        # it must directly enter Phase 2 and NOT prompt Phase 1 again
        self.dispatcher.dispatch([self.item], mcp, "play", pack_id="pack")
        self.assertEqual(len(sent_prompts), 1)
        self.assertIn("Phase 2", sent_prompts[0])
        self.assertNotIn("Phase 1", sent_prompts[0])
        finish_calls = [args for name, args in self.calls if name == "xphone.finish_event"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0]["status"], "completed")

    def test_memory_view_maps_raw_surfaces_to_human_readable(self):
        memory = self.dispatcher.memory
        route = {"role": "os-agent", "surfaceId": "agent.control"}
        memory.commit("pack", "play", "ev-1", {"roleId": "r1", "surfaceId": "messages.dm.wan_zhou:role"},
                      ["b1"], "与晚舟完成了私信沟通", "reply_text")
        memory.commit("pack", "play", "ev-2", {"roleId": "r2", "surfaceId": "notes.quick"},
                      ["b2"], "记录了码头时间", "action")
        memory.commit("pack", "play", "ev-3", {"roleId": "r3", "surfaceId": "journal.main_record"},
                      ["b3"], "夜幕下的码头故事", "action")
        view = memory.view("pack", "play", route)
        surfaces = view["surfaces"]
        self.assertIn("私聊（wan_zhou）", surfaces)
        self.assertIn("备忘录", surfaces)
        self.assertIn("故事正文", surfaces)
        self.assertNotIn("messages.dm.wan_zhou:role", surfaces)
        ledger_surfaces = [item["surfaceId"] for item in view["ledger"]]
        self.assertIn("私聊（wan_zhou）", ledger_surfaces)
        self.assertIn("备忘录", ledger_surfaces)
        self.assertIn("故事正文", ledger_surfaces)
        self.assertNotIn("messages.dm.wan_zhou:role", ledger_surfaces)


if __name__ == "__main__":
    unittest.main()
