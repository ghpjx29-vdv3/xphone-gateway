"""Return the account content key only to a signed browser public key."""

import base64
import json
import os
import time
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from account_api import account_token, request
from key_store import read_key


PUBLIC_KEY_PATH = Path(__file__).with_name("key_transfer_public.pem")


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def account_id(token: str) -> str:
    return json.loads(decode(token.split(".")[1]))["sub"]


def verify_ticket(ticket: str, expected_account: str) -> dict:
    payload, signature = ticket.split(".")
    public = serialization.load_pem_public_key(PUBLIC_KEY_PATH.read_bytes())
    if not isinstance(public, ed25519.Ed25519PublicKey):
        raise ValueError("unexpected ticket signer")
    public.verify(decode(signature), payload.encode())
    claims = json.loads(decode(payload))
    if claims["sub"] != expected_account or claims["expiresAt"] <= time.time() * 1000:
        raise ValueError("transfer ticket expired or belongs to another account")
    return claims


def seal_key(content_key: bytes, claims: dict) -> dict:
    recipient = claims["publicKey"]
    peer = ec.EllipticCurvePublicNumbers(int.from_bytes(decode(recipient["x"]), "big"),
                                         int.from_bytes(decode(recipient["y"]), "big"),
                                         ec.SECP256R1()).public_key()
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), peer)
    nonce = os.urandom(12)
    key_text = encode(content_key).encode()
    ciphertext = AESGCM(shared).encrypt(nonce, key_text, claims["requestId"].encode())
    numbers = ephemeral.public_key().public_numbers()
    return {"publicKey": {"kty": "EC", "crv": "P-256", "x": encode(numbers.x.to_bytes(32, "big")),
                          "y": encode(numbers.y.to_bytes(32, "big"))},
            "nonce": encode(nonce), "ciphertext": encode(ciphertext)}


def respond_to_pending() -> int:
    token = account_token()
    requests = request("/account/key-transfer/pending", {}, token)["requests"]
    sent = 0
    for item in requests:
        claims = verify_ticket(item["ticket"], account_id(token))
        envelope = seal_key(read_key(claims["scope"]), claims)
        request("/account/key-transfer/respond", {"requestId": claims["requestId"], "envelope": envelope}, token)
        sent += 1
    return sent
