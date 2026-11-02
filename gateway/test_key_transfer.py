import base64
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

import key_transfer


class KeyTransferTests(unittest.TestCase):
    def test_signed_browser_key_receives_only_encrypted_content_key(self):
        signer = ed25519.Ed25519PrivateKey.generate()
        recipient = ec.generate_private_key(ec.SECP256R1())
        numbers = recipient.public_key().public_numbers()
        claims = {"sub": "account-1", "requestId": "request-1", "expiresAt": int(time.time() * 1000) + 60_000,
                  "publicKey": {"kty": "EC", "crv": "P-256", "x": key_transfer.encode(numbers.x.to_bytes(32, "big")),
                                "y": key_transfer.encode(numbers.y.to_bytes(32, "big"))}}
        payload = key_transfer.encode(json.dumps(claims).encode())
        ticket = payload + "." + key_transfer.encode(signer.sign(payload.encode()))
        with tempfile.TemporaryDirectory() as directory:
            public_path = Path(directory) / "public.pem"
            public_path.write_bytes(signer.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
            with patch.object(key_transfer, "PUBLIC_KEY_PATH", public_path):
                verified = key_transfer.verify_ticket(ticket, "account-1")
                with self.assertRaises(ValueError):
                    key_transfer.verify_ticket(ticket, "account-2")
        content_key = bytes(range(32))
        envelope = key_transfer.seal_key(content_key, verified)
        self.assertNotIn(key_transfer.encode(content_key), json.dumps(envelope))
        peer = envelope["publicKey"]
        public = ec.EllipticCurvePublicNumbers(int.from_bytes(key_transfer.decode(peer["x"]), "big"),
                                               int.from_bytes(key_transfer.decode(peer["y"]), "big"),
                                               ec.SECP256R1()).public_key()
        shared = recipient.exchange(ec.ECDH(), public)
        plaintext = AESGCM(shared).decrypt(key_transfer.decode(envelope["nonce"]),
                                           key_transfer.decode(envelope["ciphertext"]), b"request-1")
        self.assertEqual(key_transfer.decode(plaintext.decode()), content_key)


if __name__ == "__main__":
    unittest.main()
