#!/usr/bin/env python3
"""PostToolUse hook for file edits, shared by Claude Code, Codex and Pi (via adapters/pi).

Runs a fast syntax check on each edited file and records that the session
edited files, so the Stop hook only runs its checks after real changes.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import log  # noqa: E402

MARKER_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks")

CHECKS = {
    ".php": ["php", "-l"],
    ".sh": ["bash", "-n"],
    ".bash": ["bash", "-n"],
    ".py": [sys.executable, "-m", "py_compile"],
    ".js": ["node", "--check"],
    ".mjs": ["node", "--check"],
    ".cjs": ["node", "--check"],
}


def edited_paths(payload):
    tool_input = payload.get("tool_input") or {}
    paths = [tool_input.get("file_path") or tool_input.get("notebook_path")]
    # Codex apply_patch carries the file list inside the patch text.
    patch = tool_input.get("patch") or tool_input.get("input") or tool_input.get("command") or ""
    if isinstance(patch, list):
        patch = "\n".join(map(str, patch))
    paths += re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", patch, re.MULTILINE)
    cwd = payload.get("cwd") or os.getcwd()
    return [os.path.join(cwd, p) for p in paths if p]


def check(path):
    ext = os.path.splitext(path)[1].lower()
    if not os.path.isfile(path):
        return None
    if ext == ".json":
        try:
            with open(path, encoding="utf-8") as fh:
                json.load(fh)
        except (ValueError, UnicodeDecodeError) as err:
            return f"{path}: invalid JSON: {err}"
        return None
    cmd = CHECKS.get(ext)
    if not cmd:
        return None
    try:
        result = subprocess.run(cmd + [path], capture_output=True, text=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return f"{path}: syntax check failed:\n{(result.stderr or result.stdout).strip()[-1500:]}"
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    paths = edited_paths(payload)
    if not paths:
        return 0
    os.makedirs(MARKER_DIR, exist_ok=True)
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "unknown"))
    with open(os.path.join(MARKER_DIR, f"{session}.edited"), "a") as fh:
        fh.writelines(os.path.realpath(p) + "\n" for p in paths)

    errors = [e for e in (check(p) for p in paths) if e]
    if errors:
        log("post_edit", "block", payload, errors[0])
        print(json.dumps({"decision": "block", "reason": "\n\n".join(errors)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
