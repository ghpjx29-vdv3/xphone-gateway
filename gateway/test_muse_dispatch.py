"""Muse wake ordering and retry behavior for retained Worker events."""

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

    def item(self, cursor, conversation):
        event_id = f"event-{cursor}"
        (self.inbox / f"{event_id}.json").write_text(json.dumps({
            "id": event_id, "playthroughId": "play",
            "decrypted": {"payload": {"conversationId": conversation, "text": "PRIVATE_MESSAGE"}},
            "route": {"role": "os-agent", "roleId": "os"},
        }))
        return {"cursor": cursor, "envelope": {"id": event_id, "kind": "event"}}

    def test_one_event_at_a_time_and_deduplicated_wake(self):
        items = [self.item(1, "first"), self.item(2, "second")]
        statuses = {"event-1": "accepted", "event-2": "accepted"}
        sent = []
        acked = []

        def mcp(name, args):
            if name == "xphone.get_event_status":
                value = {"status": statuses[args["eventId"]]}
            else:
                acked.append(args["cursor"])
                value = {"ok": True}
            return {"content": [{"text": json.dumps(value)}]}

        def cli(*args):
            if args[0] == "session-start":
                return {"session_id": f"session-{len(sent) + 1}"}
            sent.append(args)
            return {"sent": True}

        self.dispatcher.cli = cli
        with patch("muse_dispatch.time.time", return_value=1000):
            self.dispatcher.dispatch(items, mcp, "play")
            self.dispatcher = MuseDispatch(str(self.inbox), str(Path(self.tmp.name) / "state.json"))
            self.dispatcher.cli = cli
            self.dispatcher.dispatch(items, mcp, "play")
            self.assertEqual(len(sent), 1)
            statuses["event-1"] = "completed"
            self.dispatcher.dispatch(items, mcp, "play")
        self.assertEqual(acked, [1])
        self.assertEqual(len(sent), 2)
        self.assertIn('"event_id": "event-1"', sent[0][1])
        self.assertIn('"event_id": "event-2"', sent[1][1])
        self.assertNotIn("PRIVATE_MESSAGE", sent[0][1])
        self.assertFalse((self.inbox / "event-1.json").exists())

    def test_vm_failure_uses_backoff_without_losing_event(self):
        items = [self.item(1, "first")]
        calls = []

        def cli(*args):
            calls.append(args)
            raise MuseCliError(args[0], 1)

        self.dispatcher.cli = cli
        mcp = lambda _name, _args: {"content": [{"text": '{"status":"accepted"}'}]}
        with patch("muse_dispatch.time.time", return_value=1000):
            self.dispatcher.dispatch(items, mcp, "play")
            self.dispatcher.dispatch(items, mcp, "play")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.dispatcher.state["events"]["event-1"]["nextAt"], 1030)
        with patch("muse_dispatch.time.time", return_value=1031):
            self.dispatcher.dispatch(items, mcp, "play")
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
