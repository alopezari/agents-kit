"""Append one JSON line per hook decision to ~/.agents/logs/hooks.jsonl (in AGENTS_LOG_DIR instead when set: tests).

Logging must never break a hook, so every failure here is swallowed.
"""
import json
import os
import time

LOG = os.path.join(os.environ.get("AGENTS_LOG_DIR") or os.path.expanduser("~/.agents/logs"), "hooks.jsonl")


def harness(payload):
    return os.environ.get("AGENTS_HARNESS") or ("codex" if payload.get("turn_id") else "claude-code")


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
                "harness": harness(payload),
                "detail": detail[:500],
                **({"suite": os.environ["AGENTS_SUITE_RUN"]} if os.environ.get("AGENTS_SUITE_RUN") else {}),
            }) + "\n")
    except Exception:
        pass
