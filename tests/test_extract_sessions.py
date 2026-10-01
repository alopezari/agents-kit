#!/usr/bin/env python3
"""usage/extract_sessions.py: a Claude Code transcript split into phases of the flow, per branch."""
import importlib.util
import json
import os
import shutil
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []


def load(home):
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def user(minute, text, branch):
    return {"type": "user", "timestamp": f"2026-10-01T10:{minute:02d}:00Z", "cwd": "/repo", "gitBranch": branch,
            "message": {"role": "user", "content": text}}


def assistant(minute, msg_id, branch, blocks, out=10):
    """One line per content block, each repeating the message's usage, as Claude Code writes them."""
    usage = {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}
    return [{"type": "assistant", "timestamp": f"2026-10-01T10:{minute:02d}:00Z", "cwd": "/repo", "gitBranch": branch,
             "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage, "content": [block]}} for block in blocks]


def skill(name):
    return {"type": "tool_use", "id": f"t-{name}", "name": "Skill", "input": {"skill": name}}


TEXT = {"type": "text", "text": "ok"}


def phases_per_branch_and_skill(home):
    edit = {"type": "tool_use", "id": "t2", "name": "Edit", "input": {"file_path": "/repo/app.py"}}
    write_spec = {"type": "tool_use", "id": "t3", "name": "Write", "input": {"file_path": "/repo/.git/agents/spec-a.md"}}
    rows = [user(0, "build the thing", "a"),
            *assistant(1, "m0", "a", [skill("spec"), write_spec], out=7),
            *assistant(1, "m1", "a", [TEXT, edit]),                     # the first edit in the tree ends the spec
            *assistant(2, "m1b", "a", [TEXT]),
            {**user(0, "replayed by a resume", "a"), "timestamp": "2026-09-30T10:00:00Z"},
            *assistant(3, "m2", "a", [skill("self-review")]),
            *assistant(5, "m3", "a", [TEXT]),
            user(40, "<command-name>/validate</command-name>", "a"),   # the user typed it after 35 idle minutes
            *assistant(41, "m4", "a", [TEXT]),
            *assistant(43, "m5", "b", [TEXT])]                           # a new branch starts at build
    os.makedirs(os.path.join(home, ".claude", "projects", "p"))
    with open(os.path.join(home, ".claude", "projects", "p", "s1.jsonl"), "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    session, = list(load(home).claude_sessions())
    a, b = session["phases"]["a"], session["phases"]["b"]
    assert sorted(a) == ["build", "self-review", "spec", "validate"], a
    assert a["spec"] == {"in": 2, "out": 17, "cache_read": 2000, "cache_write": 200, "minutes": 1}, a["spec"]
    assert a["build"] == {"in": 1, "out": 10, "cache_read": 1000, "cache_write": 100, "minutes": 1}, a["build"]
    assert a["self-review"]["out"] == 20 and a["self-review"]["minutes"] == 3, \
        f"an older timestamp from a resume neither subtracts time nor hides the next gap: {a['self-review']}"
    assert a["validate"]["minutes"] == 1, "the 35-minute gap is the user away, not work"
    assert sorted(b) == ["build"] and b["build"]["out"] == 10, b
    assert session["tokens"]["out"] == 67, "each message counted once, not once per content block"


for test in (phases_per_branch_and_skill,):
    base = tempfile.mkdtemp(prefix="agents-test-extract-sessions-")
    home = os.environ["HOME"]
    try:
        test(base)
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {error}"))
    finally:
        os.environ["HOME"] = home
        shutil.rmtree(base, ignore_errors=True)

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
