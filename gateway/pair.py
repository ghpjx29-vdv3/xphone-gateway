#!/usr/bin/env python3
"""Show an 8-character code and receive the account token and content key."""

import base64
import os
import time
import urllib.error

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from account_api import TOKEN_PATH, request


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def unwrap(private_key, envelope: dict) -> str:
    pub = envelope["publicKey"]
    peer = ec.EllipticCurvePublicNumbers(int.from_bytes(decode(pub["x"]), "big"),
                                         int.from_bytes(decode(pub["y"]), "big"),
                                         ec.SECP256R1()).public_key()
    shared = private_key.exchange(ec.ECDH(), peer)
    # Web Crypto deriveBits returns raw ECDH shared secret, without a KDF.
    plaintext = AESGCM(shared).decrypt(decode(envelope["nonce"]),
                                       decode(envelope["ciphertext"]), None)
    return plaintext.decode("utf-8")


def write_secret(path: str, value: str) -> None:
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(value + "\n")
    os.chmod(path, 0o600)


def main() -> None:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public = private_key.public_key().public_numbers()
    encode = lambda integer: base64.urlsafe_b64encode(integer.to_bytes(32, "big")).rstrip(b"=").decode()
    started = request("/pair/start", {"publicKey": {"kty": "EC", "crv": "P-256",
                                                    "x": encode(public.x), "y": encode(public.y)}})
    print(f"XPhone pairing code: {started['code']} (expires in 5 minutes)", flush=True)
    while time.time() * 1000 < started["expiresAt"]:
        try:
            result = request("/pair/redeem", {"code": started["code"], "secret": started["secret"]})
        except urllib.error.HTTPError as error:
            if error.code == 202:
                time.sleep(2)
                continue
            raise
        if result.get("pending"):
            time.sleep(2)
            continue
        bundle = result["bundle"]
        config = os.path.join(os.path.expanduser("~"), ".config", "xphone")
        write_secret(os.path.join(config, "content.key"), unwrap(private_key, bundle["key"]))
        write_secret(TOKEN_PATH, unwrap(private_key, bundle["token"]))
        print("XPhone paired. Credentials stored with mode 0600.", flush=True)
        return
    raise TimeoutError("XPhone pairing code expired")


if __name__ == "__main__":
    main()
