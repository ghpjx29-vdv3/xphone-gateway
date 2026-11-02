"""Muse wake ordering, prompt building, reply submission, and lifecycle handling for retained Worker events."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from muse_dispatch import MuseCliError, MuseDispatch


class MuseDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = Path(self.tmp.name) / "inbox"
        self.inbox.mkdir()
        self.dispatcher = MuseDispatch(str(self.inbox), str(Path(self.tmp.name) / "state.json"))
        # 32-byte test key
        self.test_key = b"12345678901234567890123456789012"

    def item(self, cursor, conversation, event_type="os.user_message", text="PRIVATE_MESSAGE"):
        event_id = f"event-{cursor}"
        (self.inbox / f"{event_id}.json").write_text(json.dumps({
            "id": event_id, "playthroughId": "play", "packId": "blank-pack",
            "decrypted": {"type": event_type, "payload": {"conversationId": conversation, "text": text}},
            "route": {"role": "os-agent", "roleId": "os"},
        }))
        return {"cursor": cursor, "envelope": {"id": event_id, "kind": "event"}}

    def test_one_event_at_a_time_and_end_to_end_submit(self):
        # New architecture: side chat submits via connector directly;
        # gateway only manages lease (start/finish/ack), no submit_action_batch.
        items = [self.item(1, "first", text="hello muse"), self.item(2, "second", text="second msg")]
        statuses = {"event-1": "accepted", "event-2": "accepted"}
        sent = []
        mcp_calls = []

        def mcp(name, args):
            mcp_calls.append((name, args))
            if name == "xphone.get_event_status":
                return {"content": [{"text": json.dumps({"status": statuses[args["eventId"]]})}]}
            elif name == "xphone.start_event":
                return {"content": [{"text": json.dumps({"status": "running", "leaseId": f"lease-{args['eventId']}"})}]}
            elif name == "xphone.finish_event":
                statuses[args["eventId"]] = "completed"
                return {"content": [{"text": json.dumps({"ok": True})}]}
            elif name == "xphone.ack_inbox":
                return {"content": [{"text": json.dumps({"ok": True})}]}
            return {"content": [{"text": "{}"}]}

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": f"session-{len(sent) + 1}"}
            sent.append(args)
            return {"sent": True, "reply": {"text": "收到你的测试消息！"}}

        self.dispatcher.cli = cli
        with patch("muse_dispatch.time.time", return_value=1000):
            self.dispatcher.dispatch(items, mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
            self.assertEqual(len(sent), 1)
            # Check prompt contains the message text
            self.assertIn("hello muse", sent[0][1])
            self.assertEqual(sent[0][4], "--wait")
            self.assertEqual(sent[0][5], "60")
            # Verify event-1 finished and removed
            self.assertFalse((self.inbox / "event-1.json").exists())
            self.assertTrue((self.inbox / "event-2.json").exists())
            # Gateway must NOT submit via MCP; side chat does it via connector
            called_names = [c[0] for c in mcp_calls]
            self.assertNotIn("xphone.submit_action_batch", called_names)

            # Now dispatch again; event-2 should be processed
            self.dispatcher.dispatch(items, mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
            self.assertEqual(len(sent), 2)
            self.assertIn("second msg", sent[1][1])
            self.assertFalse((self.inbox / "event-2.json").exists())

    def test_lifecycle_event_auto_finishes_and_acks_without_ai(self):
        item = self.item(1, "sys", event_type="app.foreground", text="")
        sent = []
        mcp_calls = []

        def mcp(name, args):
            mcp_calls.append((name, args))
            if name == "xphone.get_event_status":
                return {"content": [{"text": json.dumps({"status": "accepted"})}]}
            elif name == "xphone.start_event":
                return {"content": [{"text": json.dumps({"status": "running", "leaseId": "lease-fg"})}]}
            elif name == "xphone.finish_event":
                return {"content": [{"text": json.dumps({"ok": True})}]}
            elif name == "xphone.ack_inbox":
                return {"content": [{"text": json.dumps({"ok": True})}]}
            return {"content": [{"text": "{}"}]}

        def cli(*args):
            sent.append(args)
            return {"sent": True}

        self.dispatcher.cli = cli
        self.dispatcher.dispatch([item], mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
        # Should not call cli at all!
        self.assertEqual(len(sent), 0)
        # Should call start_event, finish_event, ack_inbox
        called_names = [call[0] for call in mcp_calls]
        self.assertIn("xphone.start_event", called_names)
        self.assertIn("xphone.finish_event", called_names)
        self.assertIn("xphone.ack_inbox", called_names)
        self.assertFalse((self.inbox / "event-1.json").exists())

    def test_vm_failure_uses_backoff_without_losing_event(self):
        items = [self.item(1, "first")]
        calls = []

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": "s1"}
            calls.append(args)
            raise MuseCliError(args[0], 1)

        self.dispatcher.cli = cli
        mcp = lambda name, _args: {"content": [{"text": '{"status":"accepted","leaseId":"l1"}'}]}
        with patch("muse_dispatch.time.time", return_value=1000):
            self.dispatcher.dispatch(items, mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
            self.dispatcher.dispatch(items, mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.dispatcher.state["events"]["event-1"]["nextAt"], 1030)
        with patch("muse_dispatch.time.time", return_value=1031):
            self.dispatcher.dispatch(items, mcp, "play", pack_id="blank-pack", key_bytes=self.test_key)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.dispatcher.state["events"]["event-1"]["nextAt"], 1091)

    def test_cli_routes_wss_through_https_proxy(self):
        with patch.dict(os.environ, {"https_proxy": "http://proxy:8080", "wss_proxy": ""}, clear=True), patch(
            "muse_dispatch.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout='{"sent":true}')
        ) as run:
            self.dispatcher.cli("status")
        self.assertEqual(run.call_args.kwargs["env"]["wss_proxy"], "http://proxy:8080")


if __name__ == "__main__":
    unittest.main()
