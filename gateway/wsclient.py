#!/usr/bin/env python3
"""Minimal asyncio WebSocket client.

Exists because the target server (Cloudflare fronting xphone-agent-worker)
answers the upgrade with `Connection: close` on the 101 response, which both
`websockets` and `websocket-client` reject as invalid, and because the server
rejects requests that carry an `Origin` header at all. This client performs
the HTTP CONNECT tunnel, TLS and handshake manually with full header control,
then speaks RFC 6455 frames directly.

Only what the gateway needs is implemented: sending masked text frames,
receiving text frames, automatic pong replies, and close handling.
"""

import asyncio
import base64
import hashlib
import os
import ssl
from urllib.parse import urlparse

OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSClientError(Exception):
    pass


class WSClient:
    def __init__(self, url: str, subprotocol: str, user_agent: str,
                 proxy_url: str | None):
        self.url = url
        self.subprotocol = subprotocol
        self.user_agent = user_agent
        self.proxy_url = proxy_url
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def connect(self) -> None:
        u = urlparse(self.url)
        host, port = u.hostname, u.port or 443
        path = u.path or "/"
        if u.query:
            path += "?" + u.query

        # Plain TCP (optionally through an HTTP CONNECT tunnel), blocking is
        # fine here because it is quick; the socket is handed to asyncio below.
        import socket as _socket
        if self.proxy_url:
            p = urlparse(self.proxy_url)
            sock = _socket.create_connection((p.hostname, p.port or 3128),
                                             timeout=20)
            sock.sendall(
                f"CONNECT {host}:{port} HTTP/1.1\r\n"
                f"Host: {host}:{port}\r\n\r\n".encode())
            resp = b""
            while b"\r\n\r\n" not in resp:
                chunk = sock.recv(4096)
                if not chunk:
                    raise WSClientError("proxy closed CONNECT")
                resp += chunk
            if not resp.startswith(b"HTTP/1.1 200"):
                raise WSClientError(f"proxy CONNECT failed: {resp[:80]!r}")
        else:
            sock = _socket.create_connection((host, port), timeout=20)
        sock.setblocking(False)

        ctx = ssl.create_default_context()
        reader, writer = await asyncio.open_connection(
            sock=sock, ssl=ctx, server_hostname=host,
            ssl_handshake_timeout=20)

        key = base64.b64encode(os.urandom(16)).decode()
        # NOTE: no Origin header on purpose; the server rejects present Origin.
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Protocol: {self.subprotocol}\r\n"
            f"User-Agent: {self.user_agent}\r\n"
            "\r\n"
        )
        writer.write(req.encode())
        await writer.drain()
        head = await self._read_http_head(reader)
        lines = head.split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            raise WSClientError(f"handshake failed: {lines[0][:120]!r}")
        expected = base64.b64encode(
            hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if headers.get("sec-websocket-accept") != expected:
            raise WSClientError("bad Sec-WebSocket-Accept")

        self.reader, self.writer = reader, writer

    async def _read_http_head(self, reader: asyncio.StreamReader) -> str:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=20)
            if not chunk:
                break
            buf += chunk
        return buf.decode("latin1", errors="replace")

    async def _read_exact(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = await self.reader.read(n - len(data))
            if not chunk:
                raise WSClientError("connection closed mid-frame")
            data += chunk
        return data

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask_key = os.urandom(4)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.writer.write(header + mask_key + masked)
        await self.writer.drain()

    async def send_text(self, text: str) -> None:
        await self._send_frame(OP_TEXT, text.encode("utf-8"))

    async def recv_text(self) -> str | None:
        """Return the next text message, or None on clean close."""
        while True:
            b1 = (await self._read_exact(1))[0]
            b2 = (await self._read_exact(1))[0]
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                length = int.from_bytes(await self._read_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(await self._read_exact(8), "big")
            if masked:
                mask_key = await self._read_exact(4)
            payload = await self._read_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ mask_key[i % 4]
                                for i, b in enumerate(payload))
            if opcode == OP_TEXT:
                return payload.decode("utf-8")
            if opcode == OP_PING:
                await self._send_frame(OP_PONG, payload)
            elif opcode == OP_CLOSE:
                self._closed = True
                return None
            # OP_PONG and others: ignore

    async def close(self) -> None:
        try:
            if self.writer and not self._closed:
                await self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            if self.writer:
                self.writer.close()
        except Exception:
            pass
