#!/usr/bin/env python3
"""XPhone Gateway: follow the current account pack with an outbound WebSocket.

Loop:
  1. Exchange the account token for a short-lived current-pack session.
  2. Mint a one-time ticket via MCP xphone.open_event_stream.
  3. Connect to the agent WebSocket with subprotocol xphone-agent-ticket.<ticket>.
  4. On envelope notifications, pull events via MCP xphone.read_inbox.
  5. Decrypt each envelope (AES-256-GCM, content key read once at startup).
  6. Write one JSON file per event to ~/xphone/inbox/<event-id>.json.
  7. The selected provider handles the retained event; Muse uses muse-cli.

Secrets: the content key is read once from ~/.config/xphone/content.key (0600)
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
import threading
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from wsclient import WSClient, WSClientError
from account_api import active_scope, session, mcp_call as account_mcp_call
from key_transfer import respond_to_pending
from muse_dispatch import MuseDispatch

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "xphone")
INBOX = os.path.join(BASE, "inbox")
HEALTH_PATH = os.path.join(BASE, "gateway", "health.json")
SCOPE_PATH = os.path.join(BASE, "gateway", "scope.json")
KEY_PATH = os.path.join(HOME, ".config", "xphone", "content.key")
USER_AGENT = "XPhone-Gateway/1.0"
PROVIDER = os.environ.get("XPHONE_PROVIDER", "worker")

PING_INTERVAL = 30
POLL_INTERVAL = 60
RECONNECT_BACKOFF = [5, 10, 30, 60]

log = logging.getLogger("xphone-gateway")


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Gateway:
    def __init__(self) -> None:
        with open(KEY_PATH) as f:
            key_b64 = f.read().strip()
        self.key = b64u_decode(key_b64)
        if len(self.key) != 32:
            raise RuntimeError("content key is not 32 bytes")
        self.aesgcm = AESGCM(self.key)
        # Drop the base64 text; keep only key bytes in memory.
        del key_b64
        os.makedirs(INBOX, exist_ok=True)
        self.muse_dispatch = (MuseDispatch(INBOX, os.path.join(BASE, "gateway", "muse-dispatch.json"))
                              if PROVIDER == "muse" else None)
        self._dispatch_lock = threading.Lock()
        self._stop = asyncio.Event()

    def _record_health(self) -> None:
        os.makedirs(os.path.dirname(HEALTH_PATH), exist_ok=True)
        tmp = HEALTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"lastSuccessfulPull": int(time.time())}, f)
        os.replace(tmp, HEALTH_PATH)

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
                log.error("decrypt blocked: event=%s cursor=%s error=%s",
                          event_id, cursor, type(error).__name__)
                raise
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
        if PROVIDER == "muse" and getattr(self, "muse_dispatch", None) and getattr(self, "scope", None):
            try:
                try:
                    self.muse_dispatch.dispatch(
                        retained,
                        self.mcp_call,
                        self.scope["playthroughId"],
                        pack_id=self.scope.get("packId"),
                        key_bytes=getattr(self, "key", None),
                    )
                except TypeError:
                    self.muse_dispatch.dispatch(
                        retained,
                        self.mcp_call,
                        self.scope["playthroughId"],
                    )
            except Exception as error:
                log.warning("muse dispatch deferred: error=%s", type(error).__name__)
        self._record_health()

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
        # Pull immediately on (re)connect: the server does not reliably send
        # {"type":"ready"}, and the spec says to pull after notification/reconnect.
        try:
            await asyncio.to_thread(self.pull_inbox)
            last_pull = time.monotonic()
            awaiting_pong = False
            while not self._stop.is_set():
                if int(time.time() * 1000) + 60_000 >= session_expires_at or await asyncio.to_thread(active_scope) != self.scope:
                    log.info("active pack changed; reconnecting")
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv_text(),
                                                 timeout=PING_INTERVAL)
                except asyncio.TimeoutError:
                    if awaiting_pong:
                        raise WSClientError("websocket pong timeout")
                    await ws.send_text("ping")
                    awaiting_pong = True
                    if time.monotonic() - last_pull >= POLL_INTERVAL:
                        await asyncio.to_thread(self.pull_inbox)
                        last_pull = time.monotonic()
                    continue
                if msg is None:
                    log.info("server closed connection")
                    break
                if msg == "pong":
                    awaiting_pong = False
                    continue
                try:
                    data = json.loads(msg)
                except ValueError:
                    continue
                mtype = data.get("type")
                if mtype == "ready":
                    log.info("received ready; pulling inbox")
                    await asyncio.to_thread(self.pull_inbox)
                    last_pull = time.monotonic()
                elif mtype == "envelope":
                    log.info("envelope notification cursor=%s",
                             data.get("cursor"))
                    await asyncio.to_thread(self.pull_inbox)
                    last_pull = time.monotonic()
                elif mtype in ("event-finished", "inbox-acked"):
                    log.info("queue progress notification type=%s", mtype)
                    await asyncio.to_thread(self.pull_inbox)
                    last_pull = time.monotonic()
                else:
                    log.debug("unhandled message type: %s", mtype)
        finally:
            await ws.close()

    async def run(self) -> None:
        async def serve_key_transfers() -> None:
            while not self._stop.is_set():
                try:
                    count = await asyncio.to_thread(respond_to_pending, self.key)
                    if count:
                        log.info("sent %d encrypted account key transfer(s)", count)
                except Exception as error:
                    log.warning("account key transfer poll failed: %s", type(error).__name__)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5)
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
                    log.warning("connection failed (%s); retry in %ss",
                                type(e).__name__, wait)
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
