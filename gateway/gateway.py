#!/usr/bin/env python3
"""XPhone Gateway: persistent WebSocket client for xphone-agent-worker.

Loop:
  1. Mint a one-time ticket via MCP xphone.open_event_stream.
  2. Connect to the agent WebSocket with subprotocol xphone-agent-ticket.<ticket>.
  3. On envelope notifications, pull events via MCP xphone.read_inbox.
  4. Decrypt each envelope (AES-256-GCM, content key read once at startup).
  5. Write one JSON file per event to ~/xphone/inbox/<event-id>.json.
  6. A hook watches the inbox dir and wakes a worker per new file.

Secrets: the content key is read once from ~/.config/xphone/content.key (0600)
into memory. The MCP bearer token is never handled here; MCP calls go through
the vetted connector client which uses the vault surrogate. Nothing secret is
logged: logs carry event ids, cursors and routes only, never key material,
tickets or decrypted content.
"""

import asyncio
import base64
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from wsclient import WSClient, WSClientError

HOME = os.path.expanduser("~")
BASE = os.path.join(HOME, "xphone")
INBOX = os.path.join(BASE, "inbox")
HEALTH_PATH = os.path.join(BASE, "gateway", "health.json")
KEY_PATH = os.path.join(HOME, ".config", "xphone", "content.key")
MCP_CLIENT = os.path.join(
    HOME, "workspace", "custom-connectors",
    "custom.xphone-agent-worker", "bin", "client.py",
)
WS_HOST = "xphone-agent-worker.xphone-maps-gateway.workers.dev"
WS_URL_TMPL = f"wss://{WS_HOST}/agent/ws?connection={{connection}}"
USER_AGENT = "XPhone-Gateway/1.0"

PING_INTERVAL = 30
POLL_INTERVAL = 60
RECONNECT_BACKOFF = [5, 10, 30, 60]
MCP_TIMEOUT = 60

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
        self._stop = asyncio.Event()

    def _record_health(self) -> None:
        tmp = HEALTH_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"lastSuccessfulPull": int(time.time())}, f)
        os.replace(tmp, HEALTH_PATH)

    def mcp_call(self, name: str, args: dict):
        """Call one MCP tool via the vetted connector client. Returns parsed result."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(args, f)
            args_path = f.name
        try:
            proc = subprocess.run(
                ["python3", MCP_CLIENT, "call-tool",
                 "--name", name, "--arguments-file", args_path],
                capture_output=True, text=True, timeout=MCP_TIMEOUT,
            )
        finally:
            os.unlink(args_path)
        if proc.returncode != 0:
            raise RuntimeError(f"MCP {name} failed: {proc.stderr.strip()[:300]}")
        return json.loads(proc.stdout)

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

    def pull_inbox(self) -> None:
        # The Worker removes only acked entries. Re-scan retained entries on
        # every pull so a deleted local file can be recreated after a crash.
        cursor = 0
        while True:
            res = self.mcp_call("xphone.read_inbox",
                                {"cursor": cursor, "limit": 20})
            text = res["content"][0]["text"]
            items = json.loads(text).get("items", [])
            if not items:
                break
            self.handle_items(items)
            self._record_health()
            new_cursor = max(i.get("cursor", cursor) for i in items)
            if new_cursor <= cursor:
                break
            cursor = new_cursor
        self._record_health()

    async def run_once(self) -> None:
        log.info("minting event stream ticket")
        res = self.mcp_call("xphone.open_event_stream", {})
        text = res["content"][0]["text"]
        data = json.loads(text)
        ticket = data["ticket"]
        connection = data["connection"]
        url = WS_URL_TMPL.format(connection=connection)
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
                else:
                    log.debug("unhandled message type: %s", mtype)
        finally:
            await ws.close()

    async def run(self) -> None:
        backoff_idx = 0
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
