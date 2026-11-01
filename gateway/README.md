# XPhone personal-agent gateway

## Set up

1. Pull this repository into the agent cloud computer and create a Python virtual environment with `cryptography` installed.
2. Run `python gateway/pair.py` in that environment. It prints an 8-character code valid for five minutes. Keep the process running.
3. On the XPhone PWA, switch to Personal Agent mode, open the API settings panel, enter the code, and confirm. The PWA encrypts its account content key to the gateway's temporary public key. The Worker relays the ciphertext and an encrypted account token. The code alone cannot redeem either value; the gateway also holds a private redemption secret.
4. The script writes `~/.config/xphone/content.key` and `~/.config/xphone/account.token` with mode `0600`. The token may instead be supplied through the platform Secure Vault as `XPHONE_ACCOUNT_TOKEN`; once that is configured, remove the local account token file. Never paste either secret into chat or commit it.
5. Configure the outbound proxy if needed, then start `gateway/gateway.py` with the supplied systemd unit or another process supervisor. The gateway reads the active pack from `/account/active`, exchanges its account token for a one-hour pack session, and opens that pack's event stream. Switching the PWA to another Agent pack closes the old stream and reconnects automatically. Normal phone mode leaves the gateway idle.

`XPHONE_WORKER_URL` can override the default Worker base URL. The content key is account-wide; encrypted envelopes still include pack and playthrough IDs in authenticated associated data. Each pack has its own save.

## Event handling

The gateway writes decrypted events to `~/xphone/inbox/<event-id>.json`. The hook in `hooks/` wakes the agent. For a response, use `gateway/submit_actions.py --pack-id <active-pack> --playthrough-id <active-playthrough> --event-id <event-id> --step <stable-step> --actions-file <json>`. The script gets a current pack session, verifies the requested pack is active, encrypts the action batch, and checks delivery status before retrying. Reuse the event ID and step on retries.

The current action schema comes from `xphone.list_app_actions`. Blank phones support character creation and update, binding an existing Fish Audio voice ID, replacing the map catalogue, importing selected photos, notes, messaging, and the existing social actions. For private phone data, read encrypted observations. The Worker cannot read message or photo content.

## Health and testing

`~/xphone/gateway/health.json` records the last successful pull or idle poll. Run `python -m unittest discover -s gateway -p test_recovery.py` after installing dependencies. Pairing and end-to-end action delivery require a running Worker and PWA; use a single `agent.reply` action as the first bubble test.
