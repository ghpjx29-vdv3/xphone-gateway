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
                                                    "x": encode(public.x), "y": encode(public.y)},
                                      "provider": "muse"})
    print("\n" + "=" * 52, flush=True)
    print(f"XPhone 8 位配对码: {started['code']} (有效期 5 分钟)", flush=True)
    print("请在 GIGI 手机前端的「个人代理配对」页输入该配对码并确认授权。", flush=True)
    print("=" * 52 + "\n", flush=True)
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
        content_key = unwrap(private_key, bundle["key"])
        token = unwrap(private_key, bundle["token"])
        write_secret(os.path.join(config, "content.key"), content_key)
        write_secret(TOKEN_PATH, token)
        print("配对成功！凭证已安全保存到 ~/.config/xphone/ (权限 0600)。", flush=True)
        print(f"\n个人令牌 (若 Muse 平台连接器需手动填 API Key):\n{token}\n", flush=True)
        return
    raise TimeoutError("XPhone 配对超时，配对码已失效，请重新运行。")


if __name__ == "__main__":
    main()
