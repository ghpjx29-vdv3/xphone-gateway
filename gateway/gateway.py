#!/usr/bin/env python3
"""XPhone Gateway: follow the current account pack with an outbound WebSocket.

Loop:
  1. Exchange the account token for a short-lived current-pack session.
  2. Mint a one-time ticket via MCP xphone.open_event_stream.
  3. Connect to the agent WebSocket with subprotocol xphone-agent-ticket.<ticket>.
  4. On envelope notifications, pull events via MCP xphone.read_inbox.
  5. Decrypt each envelope with the active playthrough's AES-256-GCM key.
  6. Write one JSON file per event to ~/xphone/inbox/<event-id>.json.
  7. The selected provider handles the retained event; Muse uses muse-cli.

Secrets: the active playthrough's content key is read from 0600 scoped storage
into memory. The account token is read by account_api from Secure Vault or a
0600 local file; pack session tokens stay in memory. Nothing secret is
logged: logs carry event ids, cursors and routes only, never key material,
tickets or decrypted content.
"""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from wsclient import WSClient, WSClientError
from account_api import active_scope, session, mcp_call as account_mcp_call
from key_transfer import respond_to_pending
from key_store import read_key
from muse_dispatch import MuseDispatch
from content_cache import ContentCache
from delegation import Delegation

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "xphone")
INBOX = os.path.join(BASE, "inbox")
HEALTH_PATH = os.path.join(BASE, "gateway", "health.json")
SCOPE_PATH = os.path.join(BASE, "gateway", "scope.json")
CORRUPTED_PATH = os.path.join(BASE, "gateway", "corrupted-events.jsonl")
USER_AGENT = "XPhone-Gateway/1.0"
PROVIDER = os.environ.get("XPHONE_PROVIDER", "worker")

PING_INTERVAL = 30
POLL_INTERVAL = 5
INBOX_RETRY_INTERVAL = 5
RECONNECT_BACKOFF = [5, 10, 30, 60]

log = logging.getLogger("xphone-gateway")


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Gateway:
    def __init__(self) -> None:
        self.key = None
        self.aesgcm = None
        os.makedirs(INBOX, exist_ok=True)
        self.muse_dispatch = (MuseDispatch(INBOX, os.path.join(BASE, "gateway", "muse-dispatch.json"))
                              if PROVIDER == "muse" else None)
        self.content_cache = ContentCache()
        self.delegation = Delegation(self.muse_dispatch, self.content_cache.base) if self.muse_dispatch else None
        self._delegation_poll_lock = threading.Lock()
        self._dispatch_locks: dict[str, threading.Lock] = {}
        self._dispatch_lock_guard = threading.Lock()
        self._dispatch_cancel: list[threading.Event] = []
        self._dispatch_pending: set[str] = set()
        self._corrupted_events: set[str] = set()
        self._stop = asyncio.Event()

    def _record_corrupted(self, event_id: str, cursor: int, error: Exception, envelope: dict) -> None:
        if event_id in self._corrupted_events:
            return
        self._corrupted_events.add(event_id)
        os.makedirs(os.path.dirname(CORRUPTED_PATH), exist_ok=True)
        record = {
            "eventId": event_id,
            "cursor": cursor,
            "error": str(error) or type(error).__name__,
            "errorType": type(error).__name__,
            "timestamp": int(time.time() * 1000),
            "kind": envelope.get("kind"),
        }
        with open(CORRUPTED_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _record_health(self) -> None:
        directory = os.path.dirname(HEALTH_PATH)
        os.makedirs(directory, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory,
                                                prefix="health.", suffix=".tmp", delete=False)
        try:
            with temporary as f:
                json.dump({"lastHealthyAt": int(time.time())}, f)
            os.replace(temporary.name, HEALTH_PATH)
        finally:
            Path(temporary.name).unlink(missing_ok=True)

    def _record_scope(self, scope: dict | None, mcp_url: str | None) -> None:
        # Lets hook workers (and operators) see the current active pack
        # without minting a session themselves. Atomic write like health.
        os.makedirs(os.path.dirname(SCOPE_PATH), exist_ok=True)
        record: dict = {"scope": None, "updatedAt": int(time.time())}
        if scope:
            record["scope"] = {
                "packId": scope.get("packId"),
                "playthroughId": scope.get("playthroughId"),
                "xphoneSha256": scope.get("xphoneSha256"),
                "mcpUrl": mcp_url,
            }
        tmp = SCOPE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(tmp, SCOPE_PATH)

    def mcp_call(self, name: str, args: dict):
        return account_mcp_call(name, args, self.session_token, self.mcp_url)

    def decrypt_envelope(self, envelope: dict) -> dict:
        nonce = b64u_decode(envelope["nonce"])
        ct = b64u_decode(envelope["ciphertext"])
        aad = b64u_decode(envelope["aad"])
        pt = self.aesgcm.decrypt(nonce, ct, aad)
        return json.loads(pt.decode("utf-8"))

    def handle_items(self, items: list) -> None:
        for item in items:
            cursor = item.get("cursor", 0)
            envelope = item["envelope"]
            event_id = envelope["id"]
            dest = os.path.join(INBOX, f"{event_id}.json")
            if os.path.exists(dest):
                continue
            try:
                decrypted = self.decrypt_envelope(envelope)
            except Exception as error:
                log.error("decrypt blocked: event=%s cursor=%s error=%s (skipping and recording)",
                          event_id, cursor, type(error).__name__)
                self._record_corrupted(event_id, cursor, error, envelope)
                continue
            aad = json.loads(b64u_decode(envelope["aad"]).decode("utf-8"))
            record = {
                "id": event_id,
                "kind": envelope["kind"],
                "cursor": cursor,
                "direction": item.get("direction"),
                "route": item.get("route") or aad.get("route") or {},
                "playthroughId": decrypted.get("playthroughId")
                or aad.get("playthroughId"),
                "packId": decrypted.get("packId") or aad.get("packId"),
                "decrypted": decrypted,
                "queuedAt": item.get("queuedAt"),
                "receivedAt": int(time.time() * 1000),
            }
            tmp = dest + ".tmp"
            with open(tmp, "w") as f:
                json.dump(record, f, ensure_ascii=False)
            os.replace(tmp, dest)
            if envelope["kind"] == "receipt" and self.muse_dispatch:
                self.muse_dispatch.on_receipt(decrypted["batchId"])
            log.info("inbox write: event=%s cursor=%s route=%s",
                     event_id, cursor,
                     (record["route"] or {}).get("role"))
            self.trigger_worker(record)

    def trigger_worker(self, record: dict) -> None:
        if PROVIDER == "muse":
            return
        decrypted = record.get("decrypted") or {}
        event_type = decrypted.get("type", "")
        if event_type in ("app.foreground", "app.background"):
            return

        worker_py = os.environ.get("XPHONE_WORKER_SCRIPT") or os.path.join(BASE, "gateway", "inbox_worker.py")
        if not os.path.exists(worker_py):
            hatch_py = "/home/hatch/xphone/gateway/inbox_worker.py"
            if os.path.exists(hatch_py):
                worker_py = hatch_py
            else:
                return

        worker_bin = os.environ.get("XPHONE_WORKER_PYTHON") or os.path.join(BASE, "gateway", ".venv", "bin", "python")
        if not os.path.exists(worker_bin):
            hatch_bin = "/home/hatch/xphone/gateway/.venv/bin/python"
            if os.path.exists(hatch_bin):
                worker_bin = hatch_bin
            else:
                worker_bin = sys.executable

        try:
            import subprocess
            subprocess.Popen(
                [worker_bin, worker_py],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("worker triggered (Scheme 1): %s %s", worker_bin, worker_py)
        except Exception as error:
            log.error("failed to trigger worker: %s", error)

    def pull_inbox(self) -> None:
        # The Worker removes only acked entries. Re-scan retained entries on
        # every pull so a deleted local file can be recreated after a crash.
        cursor = 0
        retained = []
        while True:
            res = self.mcp_call("xphone.read_inbox",
                                {"cursor": cursor, "limit": 20})
            text = res["content"][0]["text"]
            items = json.loads(text).get("items", [])
            if not items:
                break
            retained.extend(items)
            self.handle_items(items)
            self._record_health()
            new_cursor = max(i.get("cursor", cursor) for i in items)
            if new_cursor <= cursor:
                break
            cursor = new_cursor
        if self.delegation and self.scope and self._delegation_poll_lock.acquire(blocking=False):
            token, url, scope = self.session_token, self.mcp_url, self.scope.copy()

            def poll_delegations() -> None:
                try:
                    self.delegation.poll(lambda name, args: account_mcp_call(name, args, token, url),
                                         scope["packId"], scope["playthroughId"])
                except Exception:
                    log.error("delegation tracking failed\n%s", traceback.format_exc())
                finally:
                    self._delegation_poll_lock.release()

            threading.Thread(target=poll_delegations, daemon=True).start()
        if PROVIDER == "muse" and self.muse_dispatch and self.scope and retained:
            token, url, scope = self.session_token, self.mcp_url, self.scope.copy()
            grouped: dict[str, list] = {}
            for item in retained:
                path = os.path.join(INBOX, f"{item['envelope']['id']}.json")
                try:
                    with open(path, encoding="utf-8") as f:
                        record = json.load(f)
                except FileNotFoundError:
                    continue
                key = (self.muse_dispatch.session_key(record)[0]
                       if item["envelope"]["kind"] == "event" else f"receipt:{record['id']}")
                grouped.setdefault(key, []).append(item)
            for key, items in grouped.items():
                with self._dispatch_lock_guard:
                    lock = self._dispatch_locks.setdefault(key, threading.Lock())
                    if not lock.acquire(blocking=False):
                        self._dispatch_pending.add(key)
                        continue
                    cancel = threading.Event()
                    self._dispatch_cancel.append(cancel)
                    self._dispatch_pending.discard(key)

                def dispatch_retained(role_key=key, role_items=items, role_lock=lock, role_cancel=cancel) -> None:
                    try:
                        self.muse_dispatch.dispatch(
                            role_items,
                            lambda name, args: account_mcp_call(name, args, token, url),
                            scope["playthroughId"], pack_id=scope["packId"], cancel=role_cancel,
                        )
                    except Exception as error:
                        log.exception("muse dispatch deferred: error=%s", type(error).__name__)
                        with self._dispatch_lock_guard:
                            if not role_cancel.is_set():
                                self._dispatch_pending.add(role_key)
                    finally:
                        with self._dispatch_lock_guard:
                            role_lock.release()
                            if role_cancel in self._dispatch_cancel:
                                self._dispatch_cancel.remove(role_cancel)

                threading.Thread(target=dispatch_retained, daemon=True).start()
        self._record_health()

    def take_pending_dispatch(self) -> bool:
        with self._dispatch_lock_guard:
            available = {key for key in self._dispatch_pending if not self._dispatch_locks[key].locked()}
            self._dispatch_pending.difference_update(available)
        return bool(available)

    async def run_once(self) -> None:
        scope = await asyncio.to_thread(active_scope)
        if not scope:
            log.info("normal phone mode active; waiting for an Agent pack")
            self._record_health()
            self._record_scope(None, None)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL)
            except asyncio.TimeoutError:
                pass
            return
        pack_session = await asyncio.to_thread(session)
        self.key = await asyncio.to_thread(read_key, pack_session["scope"], pack_session.get("sourceScope"))
        self.aesgcm = AESGCM(self.key)
        self.session_token = pack_session["token"]
        self.mcp_url = pack_session["mcpUrl"]
        self.scope = pack_session["scope"]
        self._record_scope(self.scope, self.mcp_url)
        session_expires_at = pack_session["expiresAt"]
        log.info("minting event stream ticket")
        res = self.mcp_call("xphone.open_event_stream", {})
        text = res["content"][0]["text"]
        data = json.loads(text)
        ticket = data["ticket"]
        connection = data["connection"]
        url = self.mcp_url.replace("https://", "wss://").replace("/mcp", "/agent/ws") + f"?connection={connection}"
        proxy_url = (os.environ.get("https_proxy")
                     or os.environ.get("HTTPS_PROXY"))
        log.info("connecting websocket")
        ws = WSClient(url, f"xphone-agent-ticket.{ticket}", USER_AGENT,
                      proxy_url)
        try:
            await ws.connect()
        except WSClientError as e:
            raise RuntimeError(f"ws connect: {e}") from None
        log.info("websocket connected; pulling inbox (ready is optional)")
        launches: dict[str, asyncio.Task] = {}

        async def launch_delegation(job_id: str, task_path: str, deadline_at: int) -> None:
            try:
                await asyncio.to_thread(self.delegation.launch, job_id, task_path, deadline_at)
            except Exception as error:
                log.exception("delegation launch failed: job=%s", job_id)
                try:
                    await ws.send_text(json.dumps({"type": "delegate-failed", "jobId": job_id,
                                                   "error": str(error)}, ensure_ascii=False))
                except (OSError, WSClientError):
                    pass  # The next connection replays the job and reports its saved result.
            finally:
                launches.pop(job_id, None)

        # Pull immediately on (re)connect: the server does not reliably send
        # {"type":"ready"}, and the spec says to pull after notification/reconnect.
        receive = None
        try:
            if self.muse_dispatch:
                await ws.send_text(json.dumps({"type": "story-preparation", "status": "preparing"}))
                try:
                    await asyncio.to_thread(self.content_cache.sync, self.mcp_call,
                                            self.scope["packId"], self.scope["playthroughId"])
                except Exception:
                    await ws.send_text(json.dumps({"type": "story-preparation", "status": "retrying"}))
                    raise
                selected = await asyncio.to_thread(active_scope)
                if not selected or any(selected.get(key) != self.scope.get(key) for key in ("packId", "playthroughId")):
                    return
            await ws.send_text(json.dumps({"type": "story-preparation", "status": "ready"}))
            await asyncio.to_thread(self.pull_inbox)
            awaiting_pong = False
            next_inbox_retry = time.monotonic() + INBOX_RETRY_INTERVAL
            next_ping = time.monotonic() + PING_INTERVAL
            receive = asyncio.create_task(ws.recv_text())
            while not self._stop.is_set():
                if int(time.time() * 1000) + 60_000 >= session_expires_at:
                    log.info("pack session expiring; reconnecting")
                    break
                timeout = max(0, min(next_inbox_retry, next_ping) - time.monotonic())
                completed, _ = await asyncio.wait({receive}, timeout=timeout)
                if time.monotonic() >= next_inbox_retry:
                    next_inbox_retry = time.monotonic() + INBOX_RETRY_INTERVAL
                    if self.muse_dispatch and (self.take_pending_dispatch() or any(
                        attempt.get("nextAt") and attempt["nextAt"] <= time.time()
                        for attempt in self.muse_dispatch.state["events"].copy().values()
                    )):
                        await asyncio.to_thread(self.pull_inbox)
                if not completed:
                    if time.monotonic() < next_ping:
                        continue
                    if awaiting_pong:
                        raise WSClientError("websocket pong timeout")
                    try:
                        current_scope = await asyncio.to_thread(active_scope)
                        if not current_scope or any(current_scope.get(key) != self.scope.get(key)
                                                    for key in ("packId", "playthroughId")):
                            log.info("active scope changed; reconnecting")
                            break
                    except Exception as scope_err:
                        log.warning("active scope check failed: %s", scope_err)
                    await ws.send_text("ping")
                    awaiting_pong = True
                    next_ping = time.monotonic() + PING_INTERVAL
                    self._record_health()
                    continue
                msg = receive.result()
                if msg is None:
                    log.info("server closed connection")
                    break
                receive = asyncio.create_task(ws.recv_text())
                next_ping = time.monotonic() + PING_INTERVAL
                self._record_health()
                if msg == "pong":
                    awaiting_pong = False
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                mtype = data.get("type")
                if mtype == "scope-changed":
                    break
                if mtype == "ready":
                    log.info("received ready; pulling inbox")
                    await asyncio.to_thread(self.pull_inbox)
                elif mtype == "envelope":
                    log.info("envelope notification cursor=%s",
                             data.get("cursor"))
                    await asyncio.to_thread(self.pull_inbox)
                elif mtype in ("event-finished", "inbox-acked"):
                    log.info("queue progress notification type=%s", mtype)
                    await asyncio.to_thread(self.pull_inbox)
                elif mtype == "delegate-task" and self.delegation:
                    job_id = data["jobId"]
                    try:
                        association = await asyncio.to_thread(self.delegation.prepare, job_id, data["taskPath"],
                                                              self.scope["packId"], self.scope["playthroughId"])
                    except Exception as error:
                        log.exception("delegation preparation failed: job=%s", job_id)
                        reply = {"type": "delegate-failed", "jobId": job_id, "error": str(error)}
                    else:
                        reply = {"type": "delegate-prepared", "jobId": job_id, **association}
                    await ws.send_text(json.dumps(reply, ensure_ascii=False))
                elif mtype == "delegate-ready" and self.delegation:
                    job_id = data["jobId"]
                    if job_id not in launches:
                        launches[job_id] = asyncio.create_task(
                            launch_delegation(job_id, data["taskPath"], data["deadlineAt"]))
                elif mtype == "delegation-progress" and self.muse_dispatch:
                    await asyncio.to_thread(self.pull_inbox)
                elif mtype == "submit-task-actions" and self.delegation:
                    request_id = data.get("requestId")
                    try:
                        prepared = await asyncio.to_thread(
                            self.delegation.prepare_actions, data.get("taskPath", ""),
                            self.scope["packId"], self.scope["playthroughId"], self.key)
                        await ws.send_text(json.dumps({"type": "submit-actions-ack",
                                                       "requestId": request_id, "ok": True,
                                                       **prepared}, ensure_ascii=False))
                        log.info("action file sealed: request=%s batch=%s", request_id,
                                 prepared["envelope"]["id"])
                    except Exception as error:
                        log.error("action file failed: request=%s error=%s", request_id, error)
                        await ws.send_text(json.dumps({"type": "submit-actions-ack",
                                                       "requestId": request_id, "ok": False,
                                                       "error": str(error)}, ensure_ascii=False))
                else:
                    log.debug("unhandled message type: %s", mtype)
        finally:
            if receive is not None:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
            if PROVIDER == "muse":
                with self._dispatch_lock_guard:
                    cancels = list(self._dispatch_cancel)
                    for cancel in cancels:
                        cancel.set()
                    self._dispatch_pending.clear()
            await ws.close()

    async def run(self) -> None:
        async def serve_key_transfers() -> None:
            while not self._stop.is_set():
                try:
                    count = await asyncio.to_thread(respond_to_pending)
                    if count:
                        log.info("sent %d encrypted account key transfer(s)", count)
                except Exception as error:
                    log.warning("account key transfer poll failed: %s", type(error).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=15)
                except asyncio.TimeoutError:
                    pass

        transfer_task = asyncio.create_task(serve_key_transfers())
        backoff_idx = 0
        try:
            while not self._stop.is_set():
                try:
                    await self.run_once()
                    backoff_idx = 0
                except Exception as e:
                    if self._stop.is_set():
                        break
                    wait = RECONNECT_BACKOFF[min(backoff_idx,
                                                 len(RECONNECT_BACKOFF) - 1)]
                    log.warning("connection failed (%s); retry in %ss", type(e).__name__, wait, exc_info=True)
                    backoff_idx += 1
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        finally:
            transfer_task.cancel()
            try:
                await transfer_task
            except asyncio.CancelledError:
                pass
        log.info("gateway stopped")

    def stop(self) -> None:
        self._stop.set()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(os.path.join(BASE, "gateway", "gateway.log")),
            logging.StreamHandler(sys.stdout),
        ],
    )
    gw = Gateway()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, gw.stop)
    try:
        loop.run_until_complete(gw.run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
