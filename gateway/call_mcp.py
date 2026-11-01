#!/usr/bin/env python3
"""Execute MCP tool calls using the active account pack session."""

import argparse
import json
import os
import sys
import time

from account_api import session, mcp_call as account_mcp_call

HOME = os.path.expanduser("~")
SESSION_CACHE_PATH = os.path.join(HOME, "xphone", "gateway", "session.json")
SCOPE_PATH = os.path.join(HOME, "xphone", "gateway", "scope.json")


def get_session() -> dict:
    # 1. Try reading cached session
    if os.path.isfile(SESSION_CACHE_PATH):
        try:
            with open(SESSION_CACHE_PATH, "r", encoding="utf-8") as f:
                cached = json.load(f)
            exp_sec = cached.get("expiresAt", 0) / 1000
            # Ensure valid for at least 60 more seconds
            if exp_sec > time.time() + 60:
                # Also verify against scope.json if scope.json exists
                if os.path.isfile(SCOPE_PATH):
                    with open(SCOPE_PATH, "r", encoding="utf-8") as sf:
                        current_scope = json.load(sf).get("scope")
                    if current_scope and cached.get("scope"):
                        if (cached["scope"].get("packId") == current_scope.get("packId") and
                                cached["scope"].get("playthroughId") == current_scope.get("playthroughId")):
                            return cached
                else:
                    return cached
        except Exception:
            pass

    # 2. Fetch fresh session from account_api
    sess = session()
    try:
        os.makedirs(os.path.dirname(SESSION_CACHE_PATH), exist_ok=True)
        tmp = SESSION_CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(sess, f, ensure_ascii=False)
        os.replace(tmp, SESSION_CACHE_PATH)
    except Exception:
        pass
    return sess


def main() -> None:
    parser = argparse.ArgumentParser(description="Call XPhone MCP tool via Gateway session")
    parser.add_argument("cmd", nargs="?", default="call-tool", help="Subcommand, e.g. call-tool")
    parser.add_argument("--name", help="Tool name")
    parser.add_argument("--arguments-file", help="Path to JSON file containing arguments")
    parser.add_argument("--arguments", help="JSON string containing arguments")
    parser.add_argument("pos_args", nargs="*", help="Optional positional arguments")

    args = parser.parse_args()

    tool_name = args.name
    call_args = {}

    if not tool_name:
        if args.cmd != "call-tool":
            tool_name = args.cmd
        elif args.pos_args:
            tool_name = args.pos_args[0]

    if not tool_name:
        parser.error("--name <tool_name> is required")

    if args.arguments_file:
        with open(args.arguments_file, "r", encoding="utf-8") as f:
            call_args = json.load(f)
    elif args.arguments:
        call_args = json.loads(args.arguments)
    elif args.pos_args:
        for arg in args.pos_args:
            if arg != tool_name and (arg.startswith("{") or os.path.isfile(arg)):
                if os.path.isfile(arg):
                    with open(arg, "r", encoding="utf-8") as f:
                        call_args = json.load(f)
                else:
                    call_args = json.loads(arg)
                break

    sess = get_session()
    result = account_mcp_call(tool_name, call_args, sess["token"], sess["mcpUrl"])
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
