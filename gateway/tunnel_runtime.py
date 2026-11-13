#!/usr/bin/env python3
"""Activate a personal Gateway and connect it to the XPhone relay."""

import argparse
import getpass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, build_opener
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

CONFIG = Path.home() / ".config" / "xphone" / "tunnel.json"
GATEWAY_DIR = Path(__file__).resolve().parent
GATEWAY_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
SECRET = re.compile(r"[A-Za-z0-9_-]{43}")


def origin(value):
    parts = urlsplit(value)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or parts.path not in ("", "/") or parts.query or parts.fragment):
        raise ValueError("Expected an HTTPS origin")
    return parts.scheme + "://" + parts.netloc


def activate(args):
    code = (sys.stdin.readline() if args.code_stdin else getpass.getpass("XPhone Gateway activation code: ")).strip()
    if not GATEWAY_ID.fullmatch(args.gateway_id) or not SECRET.fullmatch(code):
        raise ValueError("Invalid Gateway activation")
    base = origin(args.account_origin) + "/api/agent-worker/gateway"
    payload = json.dumps({"gatewayId": args.gateway_id, "code": code}).encode()
    if CONFIG.exists():
        if not args.code_stdin:
            raise FileExistsError("Gateway already has local Tunnel credentials")
        binding = json.loads(CONFIG.read_text(encoding="utf-8"))
        if binding.get("gatewayId") != args.gateway_id or binding.get("relayOrigin") != origin(args.relay_origin):
            raise ValueError("This Gateway already belongs to another connection")
    else:
        request = Request(base + "/activate", data=payload, headers={"Content-Type": "application/json", "User-Agent": "XPhone-Gateway/1.0"})
        with urlopen(request, timeout=65) as response:
            binding = json.load(response)
        if binding.get("gatewayId") != args.gateway_id or not SECRET.fullmatch(binding.get("relaySecret", "")):
            raise ValueError("Invalid Gateway activation response")
        binding["relayOrigin"] = origin(args.relay_origin)
        CONFIG.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = CONFIG.with_suffix(".pending")
        fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(binding, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG)
    if args.code_stdin:
        receipt = Request(base + "/setup/activated", data=payload, headers={
            "Content-Type": "application/json", "User-Agent": "XPhone-Gateway/1.0", "X-XPhone-Relay-Key": binding["relaySecret"],
        })
        with urlopen(receipt, timeout=15):
            pass
    print("XPhone connection credentials saved.")


def child_env(binding):
    return {
        **os.environ,
        "XPHONE_WORKER_URL": binding["relayOrigin"],
        "XPHONE_KEY_TRANSFER_URL": binding["relayOrigin"],
        "XPHONE_GATEWAY_ID": binding["gatewayId"],
        "XPHONE_LOCAL_GATEWAY_BINDING": str(CONFIG),
    }


def catalog_server(binding):
    endpoint = binding["relayOrigin"] + "/gateway/catalog-object"
    headers = {
        "Content-Type": "application/json", "User-Agent": "XPhone-Gateway/1.0",
        "X-XPhone-Gateway-Id": binding["gatewayId"], "X-XPhone-Relay-Key": binding["relaySecret"],
    }

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args):
            return None

    opener = build_opener(NoRedirect)

    class CatalogRequest(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlsplit(self.path)
            supplied = self.headers.get("X-XPhone-Relay-Key", "")
            if url.path != "/gateway/catalog-object" or not hmac.compare_digest(supplied.encode(), binding["relaySecret"].encode()):
                self.send_error(404)
                return
            keys = parse_qs(url.query).get("key", [])
            if len(keys) != 1 or len(keys[0]) > 512:
                self.send_error(400)
                return
            request = Request(endpoint, data=json.dumps({"key": keys[0]}).encode(), headers=headers)
            try:
                with opener.open(request, timeout=10) as response:
                    content = response.read()
            except HTTPError as error:
                print(f"XPhone catalog upstream returned HTTP {error.code}", file=sys.stderr, flush=True)
                self.send_error(error.code)
                return
            except (URLError, TimeoutError, OSError) as error:
                print(f"XPhone catalog request failed: {type(error).__name__}", file=sys.stderr, flush=True)
                self.send_error(502)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", 0), CatalogRequest)


def run(args):
    binding = json.loads(CONFIG.read_text(encoding="utf-8"))
    runtime = Path(args.worker_repo).resolve()
    wrangler = runtime / "node_modules" / "wrangler" / "bin" / "wrangler.js"
    if not wrangler.is_file():
        raise FileNotFoundError("Install the xphone-agent-worker runtime dependencies first")
    state = CONFIG.parent / "gateway-runtime"
    state.mkdir(mode=0o700, exist_ok=True)
    catalog = catalog_server(binding)
    variables = {
        "GRANT_HMAC_SECRET": binding["relaySecret"],
        "TOKEN_HMAC_SECRET": binding["relaySecret"],
        "XPHONE_CATALOG_ORIGIN": f"http://127.0.0.1:{catalog.server_port}",
        "XPHONE_GATEWAY_RELAY_SECRET": binding["relaySecret"],
        "XPHONE_RELAY_ORIGIN": binding["relayOrigin"],
        "CLIENT_ORIGINS": args.client_origins,
        "MCP_ALLOWED_ORIGINS": args.mcp_origins,
    }
    env_file = state / "runtime.env"
    fd = os.open(env_file, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        for name, value in variables.items():
            handle.write(name + "=" + json.dumps(value) + "\n")
    processes = []
    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    threading.Thread(target=catalog.serve_forever, daemon=True).start()
    try:
        processes.append(subprocess.Popen([
            args.node, str(wrangler), "dev", "--config", "wrangler.gateway-local.jsonc",
            "--local", "--env-file", str(env_file), "--persist-to", str(state),
            "--ip", "127.0.0.1", "--port", "8765", "--inspector-port", "0",
        ], cwd=runtime, env={**os.environ, "CI": "true"}))
        processes.append(subprocess.Popen([
            args.connector, "connect", args.relay_connect_url, str(CONFIG),
        ]))
        while all(process.poll() is None for process in processes):
            time.sleep(1)
        raise RuntimeError("XPhone connection stopped")
    finally:
        catalog.shutdown()
        catalog.server_close()
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def launch(command, provider, confirm_receipt):
    binding = json.loads(CONFIG.read_text(encoding="utf-8"))
    script = "pair.py" if command == "pair" else "gateway.py"
    env = child_env(binding)
    arguments = [sys.executable, str(GATEWAY_DIR / script)]
    if command == "pair":
        arguments.extend(["--provider", provider])
        if confirm_receipt:
            arguments.append("--confirm-receipt")
    else:
        env["XPHONE_PROVIDER"] = "muse"
    return subprocess.call(arguments, cwd=GATEWAY_DIR, env=env)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("activate")
    setup.add_argument("--account-origin", required=True)
    setup.add_argument("--relay-origin", required=True)
    setup.add_argument("--gateway-id", required=True)
    setup.add_argument("--code-stdin", action="store_true")
    start = commands.add_parser("run")
    start.add_argument("--worker-repo", required=True)
    start.add_argument("--node", default="node")
    start.add_argument("--connector", required=True)
    start.add_argument("--relay-connect-url", required=True)
    start.add_argument("--client-origins", default="https://phone.xenoflex.xyz")
    start.add_argument("--mcp-origins", default="")
    pair = commands.add_parser("pair")
    pair.add_argument("--provider", choices=["muse", "chatgpt"], default="muse")
    pair.add_argument("--confirm-receipt", action="store_true")
    commands.add_parser("muse")
    args = parser.parse_args()
    if args.command == "activate":
        activate(args)
    elif args.command == "run":
        run(args)
    else:
        sys.exit(launch(args.command, getattr(args, "provider", "muse"), getattr(args, "confirm_receipt", False)))


if __name__ == "__main__":
    main()
