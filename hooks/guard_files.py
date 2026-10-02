#!/usr/bin/env python3
"""PreToolUse guard for file tools (Claude Code Edit/Write/MultiEdit/NotebookEdit, Codex apply_patch/Edit/Write, Pi
edit/write via adapters/pi): Impeccable's project files in a shared repository need the user first (design_files.py)."""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design_files  # noqa: E402
from hooklog import log  # noqa: E402


def written(tool_input):
    """(paths a file tool writes, the text it adds to a .gitignore among them)."""
    paths = [tool_input.get("file_path") or tool_input.get("notebook_path") or tool_input.get("path")]
    added = [str(tool_input.get(k) or "") for k in ("content", "new_string")]
    added += [str(e.get("new_string") or "") for e in tool_input.get("edits") or [] if isinstance(e, dict)]
    patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""
    if isinstance(patch, list):
        patch = "\n".join(map(str, patch))
    for header, body in re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$\n((?:(?!\*\*\* ).*\n?)*)", str(patch), re.M):
        paths.append(header.strip())
        if os.path.basename(header.strip()) == ".gitignore":
            added.append("\n".join(line[1:] for line in body.splitlines() if line.startswith("+")))
    paths += re.findall(r"^\*\*\* Move to: (.+)$", str(patch), re.M)
    gitignore = "\n".join(added) if any(p and os.path.basename(p) == ".gitignore" for p in paths) else ""
    return [p for p in paths if p], gitignore


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
        paths, gitignore = written(tool_input)
        approvals = os.path.realpath(design_files.APPROVALS_DIR)
        if any(os.path.realpath(os.path.join(cwd, os.path.expanduser(p))).startswith(approvals + os.sep) for p in paths):
            why = f"Writing in {design_files.APPROVALS_DIR}: approvals come from the user, not the agent."
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
