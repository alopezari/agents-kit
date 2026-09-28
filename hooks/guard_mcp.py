#!/usr/bin/env python3
"""PreToolUse guard for MCP tools that write to shared systems.

Reads pass. Writes are allowed when the user's current message names the service
(prompt_approvals.py), or the user approved it in the last APPROVAL_MINUTES by
creating ~/.agents/approvals/<service> themselves. The shell guard (guard_bash.py)
never lets agents create either.

Browser MCP calls (Playwright, Chrome DevTools, Claude in Chrome) are logged and never blocked, so the
monthly job can tell validation runs that went around the browser A/B (bin/browse) from runs with no UI.

Profiles add their own rules in profiles/<name>/mcp-writes.json:
  {"direct": {"<server>": "<operation regex>"},
   "gateways": [{"tool": "<tool-name regex>", "service_field": "provider",
                 "operation_fields": ["subtool", "tool"], "writes": {"<service>": ["<operation>", ...]}}]}
"""
import glob
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import log  # noqa: E402

APPROVALS = os.path.expanduser("~/.agents/approvals")
TURN_APPROVALS = os.path.join(APPROVALS, "turn")
APPROVAL_MINUTES = 15

PROFILES = os.environ.get("AGENTS_PROFILES_DIR") or os.path.expanduser("~/.agents/profiles")

# MCP servers called directly: mcp__<server>__<operation>
DIRECT_WRITE = {
    "linear": re.compile(r"^(save|create|update|delete|share|unshare|merge|submit|mark|resolve|restore|retire|prepare)_"),
}
BROWSER_MCP = re.compile(r"^mcp__(playwright|chrome-devtools|claude-in-chrome)[\w-]*__")
GATEWAYS = []  # MCP tools that reach several services through one generic "execute" tool
for path in sorted(glob.glob(os.path.join(PROFILES, "*", "mcp-writes.json"))):
    rules = json.load(open(path))
    DIRECT_WRITE.update({server: re.compile(rx) for server, rx in rules.get("direct", {}).items()})
    GATEWAYS += rules.get("gateways", [])


def classify(tool_name, tool_input):
    """Return (service, operation) for a write we guard, else None."""
    m = re.match(r"^mcp__(\w+?)__(.+)$", tool_name)
    if m and m.group(1) in DIRECT_WRITE and DIRECT_WRITE[m.group(1)].match(m.group(2)):
        return m.group(1), m.group(2)
    for gateway in GATEWAYS:
        if re.search(gateway["tool"], tool_name):
            service = tool_input.get(gateway["service_field"])
            operation = next((tool_input[f] for f in gateway["operation_fields"] if tool_input.get(f)), None)
            if operation in gateway["writes"].get(service, []):
                return service, operation
    return None


def guarded_services():
    return set(DIRECT_WRITE) | {service for gateway in GATEWAYS for service in gateway["writes"]}


def approved(service, session):
    if os.path.exists(os.path.join(TURN_APPROVALS, re.sub(r"[^\w-]", "", str(session or "")) or "-", service)):
        return True
    try:
        return time.time() - os.path.getmtime(os.path.join(APPROVALS, service)) < APPROVAL_MINUTES * 60
    except OSError:
        return False


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_name = payload.get("tool_name") or ""
    if BROWSER_MCP.match(tool_name):
        log("guard_mcp", "browser-mcp", payload, tool_name)
        return 0
    hit = classify(tool_name, payload.get("tool_input") or {})
    if not hit:
        return 0
    service, operation = hit
    if approved(service, payload.get("session_id")):
        log("guard_mcp", "allow-approved", payload, f"{service}:{operation}")
        return 0
    log("guard_mcp", "deny", payload, f"{service}:{operation}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked by ~/.agents/hooks/guard_mcp.py: `{operation}` writes to {service}, which other people see. "
                f"The user's message didn't ask for {service}. Show them exactly what you would write, then ask them "
                f"to approve it by asking for it in their next message, or by running `touch ~/.agents/approvals/{service}` "
                f"(valid {APPROVAL_MINUTES} minutes), and retry after they confirm. Never create that file yourself."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
