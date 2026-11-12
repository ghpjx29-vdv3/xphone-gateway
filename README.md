# XPhone Gateway

This checkout contains the personal agent gateway, the local XPhone runtime, and the outbound WebSocket connector. Use the `main` branch for deployment.

## Pair an agent

Use a persistent Linux machine with Git, Python 3.10 or later with `venv`, Node.js 22 or later with npm, and Go 1.23 or later. For Muse, sign in to `muse-cli` on that machine.

Copy the connection instructions in XPhone and give them to your personal agent. The agent clones or updates this repository, then runs `python3 gateway/deploy.py` from the repository root with the arguments in the instructions. The command installs the pinned dependencies, builds the connector, starts the local runtime, and opens the connection to XPhone. With `--pair`, it prints `XPHONE_PAIR_CODE`. Enter that eight-character code in XPhone. Keep activation codes private and provide them to `--activate` through standard input.

## Update an existing deployment

Use the same Linux machine and account that already run XPhone.

```bash
cd ~/xphone-gateway
git pull --ff-only origin main
python3 gateway/deploy.py --provider muse
```

For ChatGPT, use `--provider chatgpt`. The command restarts the source deployment and reuses the existing account binding and local data under `~/.config/xphone/`. Add `--pair` when deliberately starting a new pairing.

If you are switching from the older `xphone-gateway.service` systemd setup, stop and disable that service before starting this deployment so only one agent consumes messages.

Service logs are under `~/.config/xphone/setup/`. The transport log records startup and connection issues. The Muse log records agent activity. Keep logs private if they contain pairing or user data.
