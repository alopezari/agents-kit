#!/usr/bin/env python3
"""PreToolUse guard for file tools (Claude Code Edit/Write/MultiEdit/NotebookEdit, Codex apply_patch/Edit/Write, Pi
edit/write via adapters/pi): Impeccable's project files in a shared repository need the user first (design_files.py)."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design_files  # noqa: E402
import post_edit  # noqa: E402
from hooklog import log  # noqa: E402


def written(payload):
    """(paths a file tool writes, the text it adds to a .gitignore among them)."""
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input.get("file_path"), str):  # Pi expands ~ before joining the cwd; edited_paths doesn't
        payload = {**payload, "tool_input": {**tool_input, "file_path": os.path.expanduser(tool_input["file_path"])}}
    paths = post_edit.edited_paths(payload)
    patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""
    patch = "\n".join(map(str, patch)) if isinstance(patch, list) else str(patch)
    paths += [os.path.join(payload.get("cwd") or os.getcwd(), p) for p in re.findall(r"^\*\*\* Move to: (.+)$", patch, re.M)]
    added = [str(tool_input.get(k) or "") for k in ("content", "new_string")]
    added += [str(e.get("new_string") or "") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
    current = ""
    for line in patch.splitlines():
        header = re.match(r"\*\*\* (?:Add|Update) File: (.+)$", line)
        current = header.group(1) if header else current
        if line.startswith("+") and os.path.basename(current).lower() == ".gitignore":
            added.append(line[1:])
    return paths, "\n".join(added)


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0
    cwd = payload.get("cwd") or os.getcwd()
    try:
        paths, gitignore = written(payload)
        approvals = design_files.APPROVALS_DIR
        if any(os.path.realpath(os.path.join(cwd, os.path.expanduser(p))).startswith(os.path.realpath(approvals) + os.sep)
               for p in paths):
            why = f"Writing in {approvals}: approvals come from the user, not the agent."
        else:
            why = design_files.blocked(paths, cwd, payload.get("session_id"), gitignore)
    except Exception as error:  # a crash would let the write through on every harness: say so instead
        why = f"The file guard failed ({type(error).__name__}: {error}). Tell the user: the error is in ~/.agents/logs/hooks.jsonl."
    if not why:
        return 0
    log("guard_files", "deny", payload, why)
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                             "permissionDecisionReason": f"Blocked by ~/.agents/hooks/guard_files.py: {why}"}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
