#!/usr/bin/env python3
"""Submit encrypted action_batch envelopes to xphone.submit_action_batch.

Plaintext: {"batchId": ..., "actions": [{"actionId": ..., ...}]}
AAD (compact, ordered keys, UTF-8): {"v":1,"packId":...,"playthroughId":...,
  "direction":"client","kind":"action_batch","id":<batchId>}
Crypto: AES-256-GCM, 32B key (base64url of content.key), 12B random nonce.
Envelope: {id: batchId, kind: action_batch, version: 1,
  nonce/ciphertext/aad: base64url no-pad}.
"""
import base64
import argparse
import json
import os
import subprocess
import tempfile
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

HOME = os.path.expanduser("~")
KEY_PATH = os.path.join(HOME, ".config", "xphone", "content.key")
MCP_CLIENT = os.path.join(
    HOME, "workspace", "custom-connectors",
    "custom.xphone-agent-worker", "bin", "client.py")


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def mcp_call(name: str, args: dict, timeout: int = 60) -> dict:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json",
                                     delete=False) as f:
        json.dump(args, f)
        p = f.name
    try:
        r = subprocess.run(
            ["python3", MCP_CLIENT, "call-tool", "--name", name,
             "--arguments-file", p],
            capture_output=True, text=True, timeout=timeout)
    finally:
        os.unlink(p)
    if r.returncode != 0:
        raise RuntimeError(f"MCP {name} failed: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def submit_batch(actions: list, pack_id: str, playthrough_id: str,
                 batch_id: str | None = None) -> str:
    key = b64u_dec(open(KEY_PATH).read().strip())
    aesgcm = AESGCM(key)
    batch_id = batch_id or str(uuid.uuid4())
    plaintext = json.dumps(
        {"batchId": batch_id, "actions": actions},
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    aad = json.dumps(
        {"v": 1, "packId": pack_id, "playthroughId": playthrough_id,
         "direction": "client", "kind": "action_batch", "id": batch_id},
        ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    ct = aesgcm.encrypt(nonce, plaintext, aad)
    envelope = {
        "id": batch_id,
        "kind": "action_batch",
        "version": 1,
        "nonce": b64u(nonce),
        "ciphertext": b64u(ct),
        "aad": b64u(aad),
    }
    res = mcp_call("xphone.submit_action_batch", {"envelope": envelope})
    text = res["content"][0]["text"]
    print(f"batch {batch_id} submitted: {text[:300]}", flush=True)
    # Persist full batch id for delivery checks
    with open(os.path.join(HOME, "xphone", "gateway", "batches.log"),
              "a") as f:
        f.write(f"{batch_id} {text[:120]}\n")
    return batch_id


def submit_event_actions(event_id: str, step: str, actions: list,
                         pack_id: str, playthrough_id: str) -> str:
    identity = f"xphone:{playthrough_id}:{event_id}:{step}"
    batch_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
    result = mcp_call("xphone.get_delivery", {"batchId": batch_id})
    delivery = json.loads(result["content"][0]["text"])
    if delivery["status"] in ("queued", "applied"):
        print(f"batch {batch_id} already {delivery['status']}", flush=True)
        return batch_id
    if delivery["status"] != "unknown":
        raise RuntimeError(f"batch {batch_id} is {delivery['status']}")
    for index, action in enumerate(actions):
        action["actionId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identity}:{index}"))
    return submit_batch(actions, pack_id, playthrough_id, batch_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="Send one encrypted XPhone bubble-test reply")
    parser.add_argument("--pack-id", required=True)
    parser.add_argument("--playthrough-id", required=True)
    parser.add_argument("--event-id")
    parser.add_argument("--step")
    parser.add_argument("--actions-file")
    args = parser.parse_args()
    if args.event_id:
        if not args.step or not args.actions_file:
            parser.error("--event-id requires --step and --actions-file")
        with open(args.actions_file, encoding="utf-8") as f:
            actions = json.load(f)
        submit_event_actions(args.event_id, args.step, actions,
                             args.pack_id, args.playthrough_id)
        return
    submit_batch([{
        "actionId": str(uuid.uuid4()),
        "type": "agent.reply",
        "messageId": f"bubble-{uuid.uuid4()}",
        "messageType": "text",
        "text": "🫧 冒泡测试：网关链路正常",
    }], args.pack_id, args.playthrough_id)


if __name__ == "__main__":
    main()
