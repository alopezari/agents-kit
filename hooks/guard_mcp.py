#!/usr/bin/env python3
"""PreToolUse guard for MCP tools that write to shared systems, and the PostToolUse hook that records what they wrote to.

Reads pass. Writes are allowed when the user's current message names the service
(prompt_approvals.py) or they allowed it in Claude Code's dialog (bin/approve), or the user approved it in the
last APPROVAL_MINUTES by creating ~/.agents/approvals/<service> themselves. The shell guard (guard_bash.py)
never lets agents create any of them.

A write the user approved records the objects it touched, where the service's rules say which they are: the ids in
the input's object fields, and the id a create returns. For OBJECT_APPROVAL_HOURS from the last write the user
approved, the same session can write to those objects again without a new approval (an edit, a comment, an
attachment), but not create one; prompt_approvals.py deletes expired records.

Browser MCP calls (Playwright, Chrome DevTools, Claude in Chrome) are logged and never blocked, so the
monthly job can tell validation runs that went around the browser A/B (bin/browse) from runs with no UI.

Profiles add their own rules in profiles/<name>/mcp-writes.json:
  {"direct": {"<server>": "<operation regex>"},
   "gateways": [{"tool": "<tool-name regex>", "service_field": "provider",
                 "operation_fields": ["subtool", "tool"], "writes": {"<service>": ["<operation>", ...]},
                 "objects": {"<service>": OBJECT_RULES}}],
   "direct_objects": {"<server>": OBJECT_RULES}}
where OBJECT_RULES is {"fields": ["<dotted input path>", ...], "created": {"<create operation>": "<response field>"}}.
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
SESSION_APPROVALS = os.path.join(APPROVALS, "session")  # <session>/<service>/<object id>, kept across messages
APPROVAL_MINUTES = 15
OBJECT_APPROVAL_HOURS = 24
OBJECT_ID = re.compile(r"\w[\w.-]{0,199}")  # a file name of its own: never a path, never hidden

PROFILES = os.environ.get("AGENTS_PROFILES_DIR") or os.path.expanduser("~/.agents/profiles")

# MCP servers called directly: mcp__<server>__<operation>
DIRECT_WRITE = {
    "linear": re.compile(r"^(save|create|update|delete|share|unshare|merge|submit|mark|resolve|restore|retire|prepare)_"),
}
BROWSER_MCP = re.compile(r"^mcp__(playwright|chrome-devtools|claude-in-chrome)[\w-]*__")
DIRECT_OBJECTS = {}
GATEWAYS = []  # MCP tools that reach several services through one generic "execute" tool
for path in sorted(glob.glob(os.path.join(PROFILES, "*", "mcp-writes.json"))):
    rules = json.load(open(path))
    DIRECT_WRITE.update({server: re.compile(rx) for server, rx in rules.get("direct", {}).items()})
    DIRECT_OBJECTS.update(rules.get("direct_objects", {}))
    GATEWAYS += rules.get("gateways", [])


def classify(tool_name, tool_input):
    """Return (service, operation, object rules) for a write we guard, else None."""
    m = re.match(r"^mcp__(\w+?)__(.+)$", tool_name)
    if m and m.group(1) in DIRECT_WRITE and DIRECT_WRITE[m.group(1)].match(m.group(2)):
        return m.group(1), m.group(2), DIRECT_OBJECTS.get(m.group(1), {})
    for gateway in GATEWAYS:
        if re.search(gateway["tool"], tool_name):
            service = tool_input.get(gateway["service_field"])
            operation = next((tool_input[f] for f in gateway["operation_fields"] if tool_input.get(f)), None)
            if operation in gateway["writes"].get(service, []):
                return service, operation, gateway.get("objects", {}).get(service, {})
    return None


def guarded_services():
    return set(DIRECT_WRITE) | {service for gateway in GATEWAYS for service in gateway["writes"]}


def approved(service, session):
    if os.path.exists(os.path.join(TURN_APPROVALS, re.sub(r"[^\w-]", "", str(session or "")) or "-", service)):
        return True
    manual = os.path.join(APPROVALS, service)
    try:  # a file: a service named "turn" must not read TURN_APPROVALS as the user's approval
        return os.path.isfile(manual) and time.time() - os.path.getmtime(manual) < APPROVAL_MINUTES * 60
    except OSError:
        return False


def session_folder(session):
    """The session's own folder name, or "" without one: unlike approved(), no "-" every sessionless call would share."""
    return re.sub(r"[^\w-]", "", str(session or ""))


def object_ids(tool_input, objects):
    """The ids in the call's object fields, or None when one of them isn't a plain id."""
    ids = []
    for path in objects.get("fields", []):
        value = tool_input
        for key in path.split("."):
            value = value.get(key) if isinstance(value, dict) else None
        if value not in (None, ""):
            if not (isinstance(value, str) and OBJECT_ID.fullmatch(value)):
                return None
            ids.append(value)
    return ids


def object_record(session, service, object_id):
    return os.path.join(SESSION_APPROVALS, session, service, object_id)


def approved_call(session, service, tool_use_id):
    """The mark a write the user approved leaves for its PostToolUse, which can't ask approved() again: by then the
    dialog's one-call approval is revoked, and a manual one may have expired."""
    call = re.sub(r"[^\w-]", "", str(tool_use_id or ""))
    return os.path.join(SESSION_APPROVALS, session, service, ".call-" + call) if session and call else None


def object_approved(service, operation, objects, tool_input, session):
    """Every object the write names was recorded for this session within OBJECT_APPROVAL_HOURS. Never a create."""
    session, ids = session_folder(session), object_ids(tool_input, objects)
    if not session or not ids or operation in objects.get("created", {}):
        return False
    for object_id in ids:
        path = object_record(session, service, object_id)
        try:
            with open(path) as record:
                exact = record.read() == object_id  # a case-insensitive disk finds ENG-1's record for eng-1 too
            if not exact or time.time() - os.path.getmtime(path) >= OBJECT_APPROVAL_HOURS * 3600:
                return False
        except OSError:
            return False
    return True


def response_field(response, name):
    """`name` in a tool's response: a dict, JSON text, or MCP content (a result's `content`, or its text blocks)."""
    if isinstance(response, dict) and name not in response:
        for part in (response.get("structuredContent"), response.get("content")):
            found = response_field(part, name)
            if found is not None:
                return found
        return None
    if isinstance(response, list):
        response = "".join(block.get("text", "") for block in response if isinstance(block, dict) and block.get("type") == "text")
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except ValueError:
            return None
    return response.get(name) if isinstance(response, dict) else None


def mark_approved_call(payload, service, operation):
    mark = approved_call(session_folder(payload.get("session_id")), service, payload.get("tool_use_id"))
    if not mark:
        return
    try:
        os.makedirs(os.path.dirname(mark), exist_ok=True)
        open(mark, "w").close()
    except OSError as error:
        log("guard_mcp", "record-failed", payload, f"{service}:{operation}: {error}")


def record_objects(payload):
    """Only a call PreToolUse marked as approved by the user records, and restarts, its objects' window: a follow-up
    its record allowed leaves no mark, so it can't stretch the window, nor bring back a record the sweep removed."""
    tool_input = payload.get("tool_input") or {}
    hit = classify(payload.get("tool_name") or "", tool_input)
    session = session_folder(payload.get("session_id"))
    if not hit or not hit[2] or not session:
        return
    service, operation, objects = hit
    mark = approved_call(session, service, payload.get("tool_use_id"))
    if not mark or not os.path.exists(mark):
        return
    ids = object_ids(tool_input, objects) or []
    created = objects.get("created", {})
    returned = response_field(payload.get("tool_response"), created[operation]) if operation in created else None
    if isinstance(returned, str) and OBJECT_ID.fullmatch(returned):
        ids.append(returned)
    try:
        for object_id in ids:
            path = object_record(session, service, object_id)
            with open(f"{mark}-{object_id}", "w") as record:  # whole, or not at all: a follow-up may be reading it
                record.write(object_id)
            os.replace(f"{mark}-{object_id}", path)
        os.remove(mark)  # last: until then it keeps the sweep from removing the folder
    except OSError as error:
        log("guard_mcp", "record-failed", payload, f"{service}:{operation}: {error}")
        return
    if ids:
        log("guard_mcp", "record-object", payload, f"{service}:{operation}:{','.join(ids)}")


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_name = payload.get("tool_name") or ""
    if payload.get("hook_event_name") == "PostToolUse":
        if not BROWSER_MCP.match(tool_name):
            record_objects(payload)
        return 0
    if BROWSER_MCP.match(tool_name):
        log("guard_mcp", "browser-mcp", payload, tool_name)
        return 0
    tool_input = payload.get("tool_input") or {}
    hit = classify(tool_name, tool_input)
    if not hit:
        return 0
    service, operation, objects = hit
    if approved(service, payload.get("session_id")):
        log("guard_mcp", "allow-approved", payload, f"{service}:{operation}")
        if objects:
            mark_approved_call(payload, service, operation)
        return 0
    if object_approved(service, operation, objects, tool_input, payload.get("session_id")):
        log("guard_mcp", "allow-object", payload, f"{service}:{operation}:{','.join(object_ids(tool_input, objects))}")
        return 0
    log("guard_mcp", "deny", payload, f"{service}:{operation}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked by ~/.agents/hooks/guard_mcp.py: `{operation}` writes to {service}, which other people see. "
                f"The user's message didn't ask for {service}"
                + (f", and this write isn't to objects they approved writing to earlier in this session (in the last "
                   f"{OBJECT_APPROVAL_HOURS} h): a create always needs their approval" if objects else "")
                + f". Show them exactly what you would write, then ask them "
                f"to approve it by asking for it in their next message, or by running `touch ~/.agents/approvals/{service}` "
                f"(valid {APPROVAL_MINUTES} minutes), and retry after they confirm. Never create that file yourself."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
