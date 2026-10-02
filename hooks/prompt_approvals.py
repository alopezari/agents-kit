#!/usr/bin/env python3
"""UserPromptSubmit hook for Claude Code and Codex, and Pi's input event (adapters/pi).

Asking for a write is approving it: when the user's message names a service guard_mcp.py guards
(Linear, or one a profile declares), or one of Impeccable's project files design_files.py guards,
writes to it are allowed until the user's next message.
Only the user's own messages reach this hook, so an agent can't grant itself one; mentioning the
service only to read from it approves writes for that turn too.
"""
import json
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design_files  # noqa: E402
import guard_mcp  # noqa: E402
from hooklog import log  # noqa: E402

STALE_SECONDS = 24 * 3600


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    session = re.sub(r"[^\w-]", "", str(payload.get("session_id") or ""))
    if not session:
        return 0
    shutil.rmtree(os.path.join(guard_mcp.TURN_APPROVALS, session), ignore_errors=True)  # the last turn's
    os.makedirs(guard_mcp.TURN_APPROVALS, exist_ok=True)
    for name in os.listdir(guard_mcp.TURN_APPROVALS):  # sessions long gone
        path = os.path.join(guard_mcp.TURN_APPROVALS, name)
        try:
            if time.time() - os.path.getmtime(path) > STALE_SECONDS:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:  # another session's hook removed it first
            pass
    prompt = str(payload.get("prompt") or "")
    named = sorted(s for s in guard_mcp.guarded_services() if re.search(rf"(?<!\w){re.escape(s)}(?!\w)", prompt, re.I))
    named += design_files.approval_names(prompt)
    if named:
        os.makedirs(os.path.join(guard_mcp.TURN_APPROVALS, session))
        for service in named:
            open(os.path.join(guard_mcp.TURN_APPROVALS, session, service), "w").close()
        log("prompt_approvals", "approve-turn", payload, ",".join(named))
    return 0


if __name__ == "__main__":
    sys.exit(main())
