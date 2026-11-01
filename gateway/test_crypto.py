#!/usr/bin/env python3
"""Offline decryption validation against a real envelope from read_inbox.

Tries AES-256-GCM with the content key (base64url, 32 bytes), 12-byte nonce,
ciphertext with appended 16-byte tag. AAD tried as raw b64 string and decoded.
Prints only success/failure and plaintext preview length, never the key.
"""
import base64
import json
import sys

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def main() -> None:
    key_b64 = open("/home/hatch/.config/xphone/content.key").read().strip()
    key = b64u(key_b64)
    print(f"key bytes: {len(key)}", flush=True)
    if len(key) != 32:
        print("KEY IS NOT 32 BYTES, aborting")
        sys.exit(2)

    env = json.load(open("/home/hatch/xphone/gateway/test_envelope.json"))
    nonce = b64u(env["nonce"])
    ct_and_tag = b64u(env["ciphertext"])
    print(f"nonce bytes: {len(nonce)}, ct+tag bytes: {len(ct_and_tag)}", flush=True)

    aad_raw = env["aad"]
    aad_candidates = {
        "aad_as_b64_string": aad_raw.encode(),
        "aad_decoded": b64u(aad_raw),
        "aad_none": None,
    }

    aesgcm = AESGCM(key)
    for name, aad in aad_candidates.items():
        try:
            pt = aesgcm.decrypt(nonce, ct_and_tag, aad)
            print(f"SUCCESS with {name}: plaintext {len(pt)} bytes")
            print(f"preview: {pt[:120]!r}")
            return
        except Exception as e:
            print(f"fail with {name}: {type(e).__name__}")
    print("ALL CANDIDATES FAILED")
    sys.exit(1)


if __name__ == "__main__":
    main()
