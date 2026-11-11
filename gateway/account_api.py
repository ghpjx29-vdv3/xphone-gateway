"""Account pairing and short-lived pack sessions for the XPhone gateway."""

import json
import os
import urllib.error
import urllib.request

WORKER_URL = os.environ.get(
    "XPHONE_WORKER_URL", "https://xphone-agent-worker.xphone-maps-gateway.workers.dev"
).rstrip("/")
KEY_TRANSFER_URL = os.environ.get(
    "XPHONE_KEY_TRANSFER_URL", "https://phone.xenoflex.xyz"
).rstrip("/")
TOKEN_PATH = os.path.join(os.path.expanduser("~"), ".config", "xphone", "account.token")


def account_token() -> str:
    value = os.environ.get("XPHONE_ACCOUNT_TOKEN")
    if value:
        return value.strip()
    with open(TOKEN_PATH, encoding="utf-8") as handle:
        return handle.read().strip()


def request(path: str, body: dict | None = None, token: str | None = None) -> dict:
    payload = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "User-Agent": "XPhone-Gateway/1.0"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    base_url = KEY_TRANSFER_URL if path.startswith("/account/key-transfer/") else WORKER_URL
    req = urllib.request.Request(base_url + path, data=payload, headers=headers,
                                 method="POST" if body is not None else "GET")
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.load(response)


def active_scope() -> dict | None:
    return request("/account/active", token=account_token())["scope"]


def session() -> dict:
    return request("/account/gateway-session", {}, account_token())


def mcp_call(name: str, args: dict, session_token: str, mcp_url: str) -> dict:
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}, separators=(",", ":")).encode()
    req = urllib.request.Request(mcp_url, data=data, method="POST", headers={
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "XPhone-Gateway/1.0",
    })
    with urllib.request.urlopen(req, timeout=60) as response:
        result = json.load(response)
    if "error" in result or result.get("result", {}).get("isError"):
        raise RuntimeError(f"MCP {name} rejected")
    return result["result"]
