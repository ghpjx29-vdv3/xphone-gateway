"""Crash boundaries for retained Worker inbox delivery."""

import asyncio
import json
import os
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import gateway as gateway_module
import call_mcp


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
        self.scope_patch = patch.object(
            gateway_module, "SCOPE_PATH", os.path.join(self.tmp.name, "scope.json"))
        self.corrupted_path = os.path.join(self.tmp.name, "corrupted-events.jsonl")
        self.corrupted_patch = patch.object(
            gateway_module, "CORRUPTED_PATH", self.corrupted_path)
        self.inbox_patch.start()
        self.health_patch.start()
        self.scope_patch.start()
        self.corrupted_patch.start()
        self.addCleanup(self.inbox_patch.stop)
        self.addCleanup(self.health_patch.stop)
        self.addCleanup(self.scope_patch.stop)
        self.addCleanup(self.corrupted_patch.stop)
        self.gateway = object.__new__(gateway_module.Gateway)
        self.gateway.decrypt_envelope = lambda envelope: {"value": envelope["id"]}
        self.gateway._dispatch_locks = {}
        self.gateway._dispatch_lock_guard = threading.Lock()
        self.gateway._dispatch_cancel = []
        self.gateway._dispatch_pending = set()
        self.gateway._corrupted_events = set()
        self.gateway._delegation_poll_lock = threading.Lock()
        self.gateway.delegation = None
        self.gateway.muse_dispatch = None
        self.gateway.scope = None

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

    def test_muse_provider_dispatches_after_inbox_persist(self):
        item = event(1)
        self.gateway.scope = {"playthroughId": "play", "packId": "pack"}
        self.gateway.session_token = "token"
        self.gateway.mcp_url = "https://worker.example/mcp"
        self.gateway.mcp_call = lambda _name, args: {
            "content": [{"text": json.dumps({"items": [item] if args["cursor"] == 0 else []})}]
        }
        dispatched = []
        done = threading.Event()

        def dispatch(items, _mcp_call, playthrough_id, **kwargs):
            self.assertTrue(os.path.exists(os.path.join(self.inbox, "event-1.json")))
            dispatched.append((items, playthrough_id))
            done.set()

        self.gateway.muse_dispatch = SimpleNamespace(dispatch=dispatch, session_key=lambda record: ("role-1", False))
        with patch.object(gateway_module, "PROVIDER", "muse"):
            self.gateway.pull_inbox()
        self.assertTrue(done.wait(2))
        self.assertEqual(dispatched, [([item], "play")])

    def test_different_roles_dispatch_concurrently(self):
        items = [event(1), event(2)]
        for index, item in enumerate(items):
            item['route'] = {'role': 'subagent', 'roleId': f'role-{index}'}
        self.gateway.scope = {'playthroughId': 'play', 'packId': 'pack'}
        self.gateway.session_token = 'token'
        self.gateway.mcp_url = 'https://worker.example/mcp'
        self.gateway.mcp_call = lambda _name, args: {
            'content': [{'text': json.dumps({'items': items if args['cursor'] == 0 else []})}]
        }
        barrier = threading.Barrier(2)
        finished = []

        def dispatch(role_items, *_args, **_kwargs):
            barrier.wait(timeout=2)
            finished.append(role_items[0]['envelope']['id'])

        self.gateway.muse_dispatch = SimpleNamespace(
            dispatch=dispatch, session_key=lambda record: (record['route']['roleId'], False))
        with patch.object(gateway_module, 'PROVIDER', 'muse'):
            self.gateway.pull_inbox()
        deadline = time.time() + 2
        while len(finished) < 2 and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(set(finished), {'event-1', 'event-2'})

    def test_bad_first_envelope_does_not_skip_later_events(self):
        self.gateway.decrypt_envelope = lambda envelope: (
            (_ for _ in ()).throw(ValueError("bad ciphertext"))
            if envelope["id"] == "event-1" else {"value": "ok"}
        )
        self.gateway.mcp_call = lambda name, args: {
            "content": [{"text": json.dumps({"items": [event(1), event(2)]})}]
        }
        self.gateway.pull_inbox()
        self.assertFalse(os.path.exists(os.path.join(self.inbox, "event-1.json")))
        self.assertTrue(os.path.exists(os.path.join(self.inbox, "event-2.json")))
        self.assertTrue(os.path.exists(gateway_module.HEALTH_PATH))
        self.assertTrue(os.path.exists(self.corrupted_path))
        with open(self.corrupted_path, encoding="utf-8") as f:
            records = [json.loads(line) for line in f]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["eventId"], "event-1")
        self.assertEqual(records[0]["cursor"], 1)
        self.assertIn("bad ciphertext", records[0]["error"])

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
        with patch.object(gateway_module, "read_key", return_value=b"a" * 32), patch.object(
            gateway_module, "WSClient", return_value=socket), patch.object(
            gateway_module, "PING_INTERVAL", 0.01
        ), patch.object(gateway_module, "active_scope", return_value={"packId": "pack"}) as scope_check, patch.object(
            gateway_module, "session", return_value={"token": "token", "mcpUrl": "https://worker.example/mcp",
                                                    "scope": {"packId": "pack"}, "expiresAt": 9999999999999}
        ):
            with self.assertRaises(gateway_module.WSClientError):
                asyncio.run(self.gateway.run_once())
        self.assertEqual(socket.sent, ["ping"])
        self.assertTrue(socket.closed)
        self.assertEqual(scope_check.call_count, 2)

    def test_call_mcp_tool_invocation_and_session_caching(self):
        fake_sess = {
            "token": "tok123",
            "mcpUrl": "https://worker.dev/mcp",
            "scope": {"packId": "p1", "playthroughId": "pt1"},
            "expiresAt": int((time.time() + 3600) * 1000),
        }
        called = []

        def mock_mcp(name, args, token, url):
            called.append((name, args, token, url))
            return {"content": [{"type": "text", "text": "{\"claimed\": true}"}]}

        session_cache = os.path.join(self.tmp.name, "session.json")
        scope_file = os.path.join(self.tmp.name, "scope.json")
        with open(scope_file, "w") as f:
            json.dump({"scope": {"packId": "p1", "playthroughId": "pt1"}}, f)

        with patch.object(call_mcp, "SESSION_CACHE_PATH", session_cache), \
             patch.object(call_mcp, "SCOPE_PATH", scope_file), \
             patch.object(call_mcp, "session", return_value=fake_sess), \
             patch.object(call_mcp, "account_mcp_call", side_effect=mock_mcp):
            s1 = call_mcp.get_session()
            self.assertEqual(s1["token"], "tok123")
            self.assertTrue(os.path.exists(session_cache))

            # Second call should read from cache without calling session()
            with patch.object(call_mcp, "session", side_effect=RuntimeError("should not call")):
                s2 = call_mcp.get_session()
                self.assertEqual(s2["token"], "tok123")

            # Calling CLI main with --name and --arguments
            with patch("sys.argv", ["call_mcp.py", "call-tool", "--name", "xphone.start_event", "--arguments", "{\"eventId\":\"e1\"}"]):
                call_mcp.main()

            self.assertEqual(len(called), 1)
            self.assertEqual(called[0][0], "xphone.start_event")
            self.assertEqual(called[0][1], {"eventId": "e1"})
            self.assertEqual(called[0][2], "tok123")


if __name__ == "__main__":
    unittest.main()
