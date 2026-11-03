"""Seal semantic actions on the gateway; routing metadata is authenticated plaintext."""

import base64
import json
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def seal(key: bytes, pack_id: str, playthrough_id: str, kind: str,
         identity: str, payload: dict, *, header: dict | None = None,
         metadata: dict | None = None) -> dict:
    metadata = metadata or {}
    version = 2 if kind == "action_batch" else 1
    aad_data = {"v": version, "packId": pack_id, "playthroughId": playthrough_id,
                "direction": "client", "kind": kind, "id": identity}
    if kind == "journal_frame":
        aad_data.update({key: metadata[key] for key in ("tickId", "streamId", "seq", "phase")})
    if header is not None:
        aad_data["header"] = header
    associated = json.dumps(aad_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce, json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), associated)
    return {"id": identity, "kind": kind, "version": version,
            "nonce": b64u(nonce), "ciphertext": b64u(ciphertext), "aad": b64u(associated),
            **metadata, **({"header": header} if header is not None else {})}
