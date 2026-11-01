"""Crash boundaries for retained Worker inbox delivery."""

import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import gateway as gateway_module
import submit_actions


def event(cursor):
    return {
        "cursor": cursor,
        "direction": "agent",
        "envelope": {
            "id": f"event-{cursor}",
            "kind": "event",
            "aad": "e30",  # {}
        },
    }


class RetainedInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.inbox = os.path.join(self.tmp.name, "inbox")
        os.mkdir(self.inbox)
        self.inbox_patch = patch.object(gateway_module, "INBOX", self.inbox)
        self.health_patch = patch.object(
            gateway_module, "HEALTH_PATH", os.path.join(self.tmp.name, "health.json"))
        self.inbox_patch.start()
        self.health_patch.start()
        self.addCleanup(self.inbox_patch.stop)
        self.addCleanup(self.health_patch.stop)
        self.gateway = object.__new__(gateway_module.Gateway)
        self.gateway.decrypt_envelope = lambda envelope: {"value": envelope["id"]}

    def test_deleted_unacked_file_is_restored_on_next_pull(self):
        calls = []

        def mcp_call(name, args):
            calls.append(args["cursor"])
            items = [event(1)] if args["cursor"] == 0 else []
            return {"content": [{"text": json.dumps({"items": items})}]}

        self.gateway.mcp_call = mcp_call
        path = os.path.join(self.inbox, "event-1.json")
        self.gateway.pull_inbox()
        self.assertTrue(os.path.exists(path))
        os.remove(path)  # A worker crashed after deleting before server ack.
        self.gateway.pull_inbox()
        self.assertTrue(os.path.exists(path))
        self.assertEqual(calls, [0, 1, 0, 1])

    def test_bad_first_envelope_does_not_skip_later_events(self):
        self.gateway.decrypt_envelope = lambda envelope: (
            (_ for _ in ()).throw(ValueError("bad ciphertext"))
            if envelope["id"] == "event-1" else {"value": "ok"}
        )
        self.gateway.mcp_call = lambda name, args: {
            "content": [{"text": json.dumps({"items": [event(1), event(2)]})}]
        }
        with self.assertRaises(ValueError):
            self.gateway.pull_inbox()
        self.assertFalse(os.path.exists(os.path.join(self.inbox, "event-2.json")))
        self.assertFalse(os.path.exists(gateway_module.HEALTH_PATH))

    def test_missing_pong_reconnects(self):
        class SilentSocket:
            sent = []
            closed = False

            async def connect(self):
                pass

            async def recv_text(self):
                await asyncio.sleep(1)

            async def send_text(self, value):
                self.sent.append(value)

            async def close(self):
                self.closed = True

        socket = SilentSocket()
        self.gateway._stop = asyncio.Event()
        self.gateway.mcp_call = lambda name, args: {
            "content": [{"text": json.dumps({"ticket": "ticket", "connection": "connection"})}]
        }
        self.gateway.pull_inbox = lambda: None
        with patch.object(gateway_module, "WSClient", return_value=socket), patch.object(
            gateway_module, "PING_INTERVAL", 0.01
        ):
            with self.assertRaises(gateway_module.WSClientError):
                asyncio.run(self.gateway.run_once())
        self.assertEqual(socket.sent, ["ping"])
        self.assertTrue(socket.closed)

    def test_replayed_action_uses_same_ids_without_resubmission(self):
        delivery = {"status": "unknown"}
        submitted = []

        def submit(actions, pack_id, playthrough_id, batch_id):
            submitted.append((batch_id, actions[0]["actionId"]))
            return batch_id

        def mcp(name, args):
            return {"content": [{"text": json.dumps(delivery)}]}

        with patch.object(submit_actions, "mcp_call", side_effect=mcp), patch.object(
            submit_actions, "submit_batch", side_effect=submit
        ):
            first = submit_actions.submit_event_actions("event-1", "reply", [{}], "pack", "playthrough")
            retry = submit_actions.submit_event_actions("event-1", "reply", [{}], "pack", "playthrough")
            delivery["status"] = "queued"
            second = submit_actions.submit_event_actions("event-1", "reply", [{}], "pack", "playthrough")
        self.assertEqual(first, retry)
        self.assertEqual(first, second)
        self.assertEqual(len(submitted), 2)
        self.assertEqual(submitted[0], submitted[1])
        self.assertEqual(submitted[0][0], first)


if __name__ == "__main__":
    unittest.main()
