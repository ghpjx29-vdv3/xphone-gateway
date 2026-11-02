# xphone-gateway

An outbound WebSocket gateway that connects a personal agent to the active XPhone Agent phone. It receives encrypted events, decrypts them locally, and submits encrypted app actions. No inbound public endpoint is needed.

See [gateway/README.md](gateway/README.md) for setup, pairing, and operations.

The 8-character Worker code is used only for the first account pairing or an intentional agent replacement. A new browser signed in to the same XPhone account recovers the existing content key from the running gateway automatically: the account server signs its one-time browser public key, the gateway verifies that signature, and the Worker relays only ciphertext. Keep the updated gateway running even when the phone is in Normal mode so new browsers can recover their key. The pinned verification key is `gateway/key_transfer_public.pem`.
