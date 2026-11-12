#!/usr/bin/env python3
"""Build and start XPhone from this Git checkout."""

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from tunnel_runtime import CONFIG, GATEWAY_DIR, origin

ROOT = GATEWAY_DIR.parent
STATE = CONFIG.parent
SETUP = STATE / "setup"
PYTHON = STATE / "venv" / "bin" / "python"
CONNECTOR = STATE / "bin" / "xphone-vps-relay"
RUNTIME = ROOT / "runtime"
WRANGLER = RUNTIME / "node_modules" / "wrangler" / "bin" / "wrangler.js"
LAUNCHER = GATEWAY_DIR / "tunnel_runtime.py"
CONNECT_URL = "wss://phone.xenoflex.xyz/api/xphone-vps/connect"
PAIR_CODE = re.compile(r"^XPHONE_PAIR_CODE=([A-HJ-NP-Z2-9]{8})$", re.MULTILINE)


def run(command, cwd=ROOT, **kwargs):
    subprocess.run([str(part) for part in command], cwd=cwd, check=True, **kwargs)


def build():
    if not PYTHON.with_name("pip").is_file():
        run([sys.executable, "-m", "venv", PYTHON.parent.parent])
    run([PYTHON, "-m", "pip", "install", "-r", GATEWAY_DIR / "requirements.txt"])
    run(["npm", "ci", "--no-audit", "--no-fund"], cwd=RUNTIME)
    CONNECTOR.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = CONNECTOR.with_suffix(".new")
    run(["go", "build", "-trimpath", "-o", staged, "."], cwd=ROOT / "connector",
        env={**os.environ, "CGO_ENABLED": "0"})
    staged.replace(CONNECTOR)


def seed_runtime():
    seed = RUNTIME / "seed"
    files = sorted(path for path in seed.rglob("*") if path.is_file())
    checksum = hashlib.sha256()
    for path in files:
        checksum.update(path.relative_to(seed).as_posix().encode() + b"\0" + path.read_bytes())
    version = checksum.hexdigest()
    stamp = SETUP / "seed.sha256"
    if stamp.exists() and stamp.read_text().strip() == version:
        return
    for path in files:
        key = path.relative_to(seed).as_posix()
        run(["node", WRANGLER, "r2", "object", "put", "xphone-agent-documents/" + key,
             "--file", path, "--local", "--persist-to", STATE / "gateway-runtime",
             "--config", "wrangler.gateway-local.jsonc"], cwd=RUNTIME)
    stamp.write_text(version + "\n")


def job_pid(name):
    marker = "xphone-agent-" + name
    try:
        pid = int((SETUP / f"{name}.pid").read_text())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if marker.encode() in command and os.getpgid(pid) == pid:
            return pid
    except (FileNotFoundError, ProcessLookupError, ValueError):
        pass


def stop_job(name):
    pid = job_pid(name)
    if not pid:
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 20
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Could not stop the running XPhone {name} process.")
        time.sleep(0.25)


def start_job(name, command, once=False, env=None):
    log = SETUP / f"{name}.log"
    wrapper = [PYTHON, Path(__file__).resolve(), "_run", f"xphone-agent-{name}",
               "once" if once else "restart", *command]
    with log.open("w" if once else "a", encoding="utf-8") as output:
        process = subprocess.Popen(wrapper, stdin=subprocess.DEVNULL, stdout=output,
                                   stderr=subprocess.STDOUT, start_new_session=True, env=env)
    (SETUP / f"{name}.pid").write_text(f"{process.pid}\n")
    return process


def wait_for_relay(binding, process):
    relay = urlsplit(CONNECT_URL)
    target = f"https://{relay.netloc}/api/xphone-vps/gateway/{binding['gatewayId']}/health"
    request = Request(target, headers={
        "User-Agent": "XPhone-Gateway/1.0",
        "X-XPhone-Relay-Key": binding["relaySecret"],
        "X-XPhone-Public-URL": binding["relayOrigin"] + "/health",
    })
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(request, timeout=5) as response:
                if json.load(response).get("ok") is True:
                    return
        except URLError:
            pass
        time.sleep(1)
    raise RuntimeError(f"XPhone did not become ready. See {SETUP / 'transport.log'}.")


def wait_for_code(process):
    log = SETUP / "pairing.log"
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = PAIR_CODE.search(log.read_text(encoding="utf-8"))
        if result:
            return result.group(1)
        if process.poll() is not None:
            break
        time.sleep(0.25)
    raise RuntimeError(f"XPhone did not return a pairing code. See {log}.")


def deploy(args):
    os.umask(0o077)
    SETUP.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (SETUP / "install.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.which("systemctl") and subprocess.run(
            ["systemctl", "is-active", "--quiet", "xphone-gateway.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        ).returncode == 0:
            raise RuntimeError("Stop the existing xphone-gateway systemd service before switching to this deployment.")
        env = os.environ.copy()
        if args.provider == "muse":
            cli = shutil.which("muse-cli") or str(Path.home() / ".local" / "bin" / "muse-cli")
            run([cli, "status"], stdout=subprocess.DEVNULL)
            env["XPHONE_MUSE_CLI"] = cli
        if args.activate:
            run([sys.executable, LAUNCHER, "activate", "--code-stdin",
                 "--account-origin", args.account_origin, "--relay-origin", args.relay_origin,
                 "--gateway-id", args.gateway_id])
        binding = json.loads(CONFIG.read_text(encoding="utf-8"))
        if args.gateway_id and binding["gatewayId"] != args.gateway_id:
            raise RuntimeError("This machine is connected to a different XPhone account.")
        if args.relay_origin and binding["relayOrigin"] != origin(args.relay_origin):
            raise RuntimeError("This machine is connected to a different XPhone account.")
        build()
        for name in ("pairing", "muse", "transport"):
            stop_job(name)
        seed_runtime()
        transport = start_job("transport", [PYTHON, LAUNCHER, "run", "--worker-repo", RUNTIME,
                              "--connector", CONNECTOR, "--relay-connect-url", CONNECT_URL,
                              "--mcp-origins", "https://chatgpt.com,https://chat.openai.com"], env=env)
        wait_for_relay(binding, transport)
        if args.provider == "muse":
            start_job("muse", [PYTHON, Path(__file__).resolve(), "_muse"], env=env)
        if args.pair:
            pairing = start_job("pairing", [PYTHON, LAUNCHER, "pair", "--provider", args.provider,
                                "--confirm-receipt"], once=True, env=env)
            print("XPHONE_PAIR_CODE=" + wait_for_code(pairing), flush=True)
        else:
            print("XPhone is ready.", flush=True)


def main():
    if sys.argv[1:2] == ["_run"]:
        _, _, _, mode, *command = sys.argv
        while True:
            result = subprocess.call(command)
            if mode == "once":
                return result
            time.sleep(3)
    if sys.argv[1:2] == ["_muse"]:
        while not (STATE / "account.token").is_file():
            time.sleep(1)
        return subprocess.call([sys.executable, str(LAUNCHER), "muse"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["muse", "chatgpt"], required=True)
    parser.add_argument("--gateway-id")
    parser.add_argument("--account-origin", default="https://therese.xenoflex.xyz")
    parser.add_argument("--relay-origin")
    parser.add_argument("--activate", action="store_true", help="Read a private activation code from stdin.")
    parser.add_argument("--pair", action="store_true", help="Start pairing and return the eight-character code.")
    args = parser.parse_args()
    if args.activate and (not args.gateway_id or not args.relay_origin):
        parser.error("Activation needs --gateway-id and --relay-origin.")
    deploy(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
