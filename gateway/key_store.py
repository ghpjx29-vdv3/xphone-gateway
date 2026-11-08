"""Content keys indexed by XPhone playthrough."""

import base64
import hashlib
import os

CONFIG = os.path.join(os.path.expanduser("~"), ".config", "xphone")
LEGACY_KEY_PATH = os.path.join(CONFIG, "content.key")


def key_path(scope: dict) -> str:
    name = f"{scope['packId']}:{scope['playthroughId']}".encode("utf-8")
    return os.path.join(CONFIG, "content-keys", hashlib.sha256(name).hexdigest() + ".key")


def read_key(scope: dict, source_scope: dict | None = None) -> bytes:
    path = key_path(scope)
    if not os.path.exists(path):
        source_path = key_path(source_scope) if source_scope else LEGACY_KEY_PATH
        if not os.path.exists(source_path) and source_scope:
            source_path = LEGACY_KEY_PATH
        with open(source_path, encoding="utf-8") as handle:
            write_key(scope, handle.read().strip())
    with open(path, encoding="utf-8") as handle:
        encoded = handle.read().strip()
    key = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    if len(key) != 32:
        raise RuntimeError("content key is not 32 bytes")
    return key


def write_key(scope: dict, encoded: str) -> None:
    path = key_path(scope)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(encoded + "\n")
    os.chmod(path, 0o600)
