"""Append one JSON line per hook decision to ~/.agents/logs/hooks.jsonl.

Logging must never break a hook, so every failure here is swallowed.
"""
import json
import os
import time

LOG = os.path.expanduser("~/.agents/logs/hooks.jsonl")


def log(hook, decision, payload, detail):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "hook": hook,
                "decision": decision,
                "session": payload.get("session_id"),
                "cwd": payload.get("cwd"),
                "harness": os.environ.get("AGENTS_HARNESS") or ("codex" if payload.get("turn_id") else "claude-code"),
                "detail": detail[:500],
            }) + "\n")
    except Exception:
        pass
