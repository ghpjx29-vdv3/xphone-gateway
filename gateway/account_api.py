"""Account pairing and short-lived pack sessions for the XPhone gateway."""

import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

CONFIG_DIR = Path.home() / ".config" / "xphone"
TOKEN_PATH = str(CONFIG_DIR / "account.token")
configured_binding = os.environ.get("XPHONE_LOCAL_GATEWAY_BINDING")
binding_path = Path(configured_binding) if configured_binding else CONFIG_DIR / "tunnel.json"
if configured_binding or binding_path.is_file():
    with binding_path.open(encoding="utf-8") as handle:
        binding = json.load(handle)
    WORKER_URL = binding["relayOrigin"].rstrip("/")
    GATEWAY_ID = binding["gatewayId"]
    KEY_TRANSFER_URL = WORKER_URL
    LOCAL_RELAY_SECRET = binding["relaySecret"]
    LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
else:
    WORKER_URL = os.environ.get(
        "XPHONE_WORKER_URL", "https://xphone-agent-tunnel-relay.xphone-maps-gateway.workers.dev"
    ).rstrip("/")
    GATEWAY_ID = os.environ.get("XPHONE_GATEWAY_ID", "")
    KEY_TRANSFER_URL = os.environ.get("XPHONE_KEY_TRANSFER_URL", WORKER_URL).rstrip("/")
    LOCAL_RELAY_SECRET = ""


def gateway_endpoint(url: str) -> tuple[str, dict[str, str]]:
    if not LOCAL_RELAY_SECRET:
        return url, {}
    public = urlsplit(url)
    relay = urlsplit(WORKER_URL)
    if public.netloc != relay.netloc or public.scheme not in ("https", "wss"):
        return url, {}
    scheme = "ws" if public.scheme == "wss" else "http"
    path = public.path or "/"
    return urlunsplit((scheme, "127.0.0.1:8765", path, public.query, "")), {
        "X-XPhone-Relay-Key": LOCAL_RELAY_SECRET,
        "X-XPhone-Public-URL": urlunsplit(("https", relay.netloc, path, public.query, "")),
    }


def _open_json(url: str, payload: bytes | None, headers: dict) -> dict:
    target, relay_headers = gateway_endpoint(url)
    request = urllib.request.Request(target, data=payload, headers={**headers, **relay_headers})
    opener = LOCAL_OPENER.open if relay_headers else urllib.request.urlopen
    with opener(request, timeout=60) as response:
        return json.load(response)


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
    if GATEWAY_ID and path in ("/pair/start", "/pair/redeem", "/pair/complete"):
        from urllib.parse import urlencode
        path += "?" + urlencode({"gatewayId": GATEWAY_ID})
    return _open_json(base_url + path, payload, headers)


def active_scope() -> dict | None:
    return request("/account/active", token=account_token())["scope"]


def session() -> dict:
    return request("/account/gateway-session", {}, account_token())


def mcp_call(name: str, args: dict, session_token: str, mcp_url: str) -> dict:
    data = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": name, "arguments": args}}, separators=(",", ":")).encode()
    result = _open_json(mcp_url, data, {
        "Authorization": f"Bearer {session_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "XPhone-Gateway/1.0",
    })
    if "error" in result or result.get("result", {}).get("isError"):
        raise RuntimeError(f"MCP {name} rejected")
    return result["result"]
