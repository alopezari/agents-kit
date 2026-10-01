#!/usr/bin/env python3
"""usage/extract_sessions.py: a Claude Code transcript split into phases of the flow, per branch."""
import importlib.util
import json
import os
import shutil
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
TEXT = {"type": "text", "text": "ok"}


def at(minute, second=0):
    return f"2026-10-01T10:{minute:02d}:{second:02d}Z"


def user(minute, text, branch, uuid=None):
    return {"type": "user", "timestamp": at(minute), "cwd": "/repo", "gitBranch": branch, "uuid": uuid or f"u{minute}{branch}",
            "message": {"role": "user", "content": text}}


def assistant(minute, msg_id, branch, blocks, out=10, second=0):
    """One line per content block, each repeating the message's usage, as Claude Code writes them."""
    usage = {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}
    return [{"type": "assistant", "timestamp": at(minute, second), "cwd": "/repo", "gitBranch": branch, "uuid": f"{msg_id}-{i}",
             "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage, "content": [block]}}
            for i, block in enumerate(blocks)]


def skill(name):
    return {"type": "tool_use", "id": f"t-{name}", "name": "Skill", "input": {"skill": name}}


def edit(path):
    return {"type": "tool_use", "id": f"t-{path}", "name": "Write" if "spec-" in path else "Edit", "input": {"file_path": path}}


def session(home, rows, renames=()):
    os.makedirs(os.path.join(home, ".claude", "projects", "p"))
    with open(os.path.join(home, ".claude", "projects", "p", "s1.jsonl"), "w") as fh:
        fh.writelines(json.dumps(r) + "\n" for r in rows)
    os.makedirs(os.path.join(home, ".agents", "logs"))
    with open(os.path.join(home, ".agents", "logs", "quality.jsonl"), "w") as fh:
        fh.writelines(json.dumps({"kind": "rename", "repo": repo, "name": old, "to": new}) + "\n" for repo, old, new in renames)
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found, = list(module.claude_sessions())
    return found


def phases_per_branch_and_skill(home):
    rows = [user(0, "build the thing", "a"),
            *assistant(1, "m0", "a", [TEXT, skill("spec"), edit("/tmp/agents-specs/spec-a.md")], out=7),
            *assistant(2, "m1", "a", [TEXT, edit("/repo/app.py")]),       # the first code edit ends the spec
            {**user(0, "replayed by a resume", "a"), "timestamp": "2026-09-30T10:00:00Z", "uuid": "old"},
            *assistant(3, "m2", "a", [TEXT, skill("self-review")]),        # the skill in a later block owns the message
            {"type": "file-history-snapshot", "timestamp": at(4), "uuid": "fh"},  # no branch: still branch a's time
            *assistant(5, "m3", "a", [TEXT]),
            user(40, "<command-name>/validate</command-name>", "a"),       # typed after 35 idle minutes
            *assistant(41, "m4", "a", [TEXT]),
            *assistant(43, "m5", "b", [TEXT]),                              # another branch starts at build
            *assistant(44, "m6", "a", [TEXT]),                              # and so does coming back
            *assistant(45, "m2", "a", [TEXT, skill("self-review")]),       # a replayed message changes nothing
            ]
    found = session(home, rows)
    a, b = found["phases"]["a"], found["phases"]["b"]
    assert sorted(a) == ["build", "self-review", "spec", "validate"], a
    assert a["spec"] == {"in": 1, "out": 7, "cache_read": 1000, "cache_write": 100, "minutes": 1}, a["spec"]
    assert (a["build"]["out"], a["build"]["minutes"]) == (20, 2), f"m1 and m6, 1 minute each: {a['build']}"
    assert (a["self-review"]["out"], a["self-review"]["minutes"]) == (20, 3), \
        f"m2 and m3, minutes 2 to 5 through the branchless entry and past the replayed timestamp: {a['self-review']}"
    assert a["validate"]["minutes"] == 1, "the 35-minute gap is the user away, not work"
    assert sorted(b) == ["build"] and b["build"]["out"] == 10, b
    assert found["tokens"]["out"] == 67, "each message counted once, not once per content block or replay"


def a_rename_keeps_the_phase(home):
    rows = [user(0, "go", "session/x"), *assistant(1, "m0", "session/x", [skill("validate")]), *assistant(2, "m1", "feature/cart", [TEXT]),
            *assistant(3, "m2", "other", [skill("validate")]), *assistant(4, "m3", "next", [TEXT])]
    found = session(home, rows, renames=[("repo", "session/x", "session/y"), ("repo", "session/y", "feature/cart"),
                                         ("elsewhere", "other", "next")])
    assert sorted(found["phases"]["feature/cart"]) == ["validate"], f"a chain of renames is one change: {found['phases']}"
    assert sorted(found["phases"]["next"]) == ["build"], f"another repo's rename doesn't apply here: {found['phases']}"


def what_ends_a_phase(home):
    rows = [user(0, "go", "a"),
            *assistant(1, "m0", "a", [skill("spec"), edit("/repo/app.py")]),           # starts the spec and builds at once
            *assistant(2, "m1", "a", [TEXT]),
            *assistant(3, "m2", "a", [skill("spec")]),
            *assistant(4, "m3", "a", [edit("/repo/spec-parser.py")]),                  # code, despite its name
            *assistant(5, "m4", "a", [skill("validate")]),
            {**user(6, "<command-name>/self-review</command-name>", "a"), "gitBranch": ""},  # no branch: no change
            *assistant(7, "m5", "a", [TEXT])]
    phases = session(home, rows)["phases"]["a"]
    assert (phases["build"]["out"], phases["spec"]["out"], phases["validate"]["out"]) == (30, 10, 20), phases


def short_gaps_add_up(home):
    rows = [user(1, "go", "a")] + [row for i in range(31) for row in assistant(1 + 2 * i // 60, f"m{i}", "a", [TEXT], second=2 * i % 60)]
    found = session(home, rows)
    assert found["phases"]["a"]["build"]["minutes"] == 1.0, found["phases"]["a"]


for test in (phases_per_branch_and_skill, a_rename_keeps_the_phase, what_ends_a_phase, short_gaps_add_up):
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
