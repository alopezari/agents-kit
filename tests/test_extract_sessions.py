#!/usr/bin/env python3
"""usage/extract_sessions.py: a Claude Code transcript split into phases of the flow, per branch."""
import importlib.util
import json
import os
import shutil
import subprocess
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


def bash(command, msg_id="b"):
    return {"type": "tool_use", "id": f"t-{msg_id}", "name": "Bash", "input": {"command": command}}


def session(home, rows, renames=(), cwd="repo"):
    """Rows' "/repo" is a checkout of o/repo at HOME/repo; with `cwd`, the session ran in HOME/<cwd> instead."""
    checkout = os.path.join(home, "repo")
    os.makedirs(checkout)
    subprocess.run(["git", "init", "-q", checkout], check=True)
    subprocess.run(["git", "-C", checkout, "remote", "add", "origin", "git@github.com:o/repo.git"], check=True)
    os.makedirs(os.path.join(home, ".claude", "projects", "p"))
    with open(os.path.join(home, ".claude", "projects", "p", "s1.jsonl"), "w") as fh:
        fh.writelines(json.dumps({**r, "cwd": os.path.join(home, cwd)} if r.get("cwd") else r) + "\n" for r in rows)
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


def branches_started_by_commands(home):
    """The session directory stays on one branch (or detached) while the agent starts each change in a worktree."""
    rows = [user(0, "go", "phase-cost"),
            *assistant(1, "m0", "phase-cost", [skill("self-review")]),
            *assistant(2, "m1", "phase-cost", [bash("cd ~/kit && git worktree add -q -b short-outputs $SP/kit-so origin/main")]),
            *assistant(3, "m2", "phase-cost", [TEXT]),                      # its gitBranch hasn't changed: still short-outputs
            *assistant(4, "m3", "HEAD", [TEXT]),                            # the session directory detached
            *assistant(5, "m4", "HEAD", [bash("git fetch -q; git switch -qc guard origin/main"), skill("validate")]),
            *assistant(6, "m5", "HEAD", [bash("git branch -m guard guard-says-the-order")]),
            *assistant(7, "m6", "HEAD", [bash("python3 - <<'EOF'\nprint('git switch -c bogus')\nEOF", "m6")])]
    found = session(home, rows, cwd="repo-worktree-session-gone")
    assert found["branches"] == ["HEAD", "guard", "guard-says-the-order", "phase-cost", "short-outputs"], found["branches"]
    phases = found["phases"]
    assert sorted(phases["short-outputs"]) == ["build"] and phases["short-outputs"]["build"]["out"] == 20, phases
    assert "guard" not in phases and phases["guard-says-the-order"]["validate"]["out"] == 30, \
        f"a rename carries the branch's phase and its spending to the new name: {phases}"
    assert found["repos"] == {"o/repo": "repo"}, f"a removed worktree's repo comes from its main checkout: {found['repos']}"


def writing_the_spec_marks_the_work_before_it(home):
    """Agents write most specs without invoking the skill again; the spec file is what every spec leaves behind."""
    rows = [user(0, "go", "session/x"),
            *assistant(1, "m0", "session/x", [TEXT]),                                   # reading the issue and the code
            *assistant(2, "m1", "session/x", [TEXT]),
            *assistant(3, "m2", "session/x", [edit("/g/agents/spec-repo-x.md")]),       # the spec, written with Write
            *assistant(4, "m3", "session/x", [bash("git branch -m kit-change")]),
            *assistant(5, "m4", "kit-change", [edit("/repo/app.py")]),                  # the first code edit ends it
            *assistant(6, "m5", "kit-change", [TEXT]),
            *assistant(7, "m6", "kit-change", [{**edit("/g/agents/spec-repo-kit-change.md"), "name": "Edit"}]),
            *assistant(8, "m7", "kit-change", [edit("/repo/app.py")]),
            *assistant(9, "m8", "other", [TEXT]),
            *assistant(10, "m9", "other", [skill("self-review")])]
    phases = session(home, rows)["phases"]
    assert (phases["kit-change"]["spec"]["out"], phases["kit-change"]["build"]["out"]) == (40, 40), \
        f"m0-m3 are the spec (m0 and m1 before the file existed), m4-m7 build: a spec edit mid-build is not a new spec: {phases}"
    assert sorted(phases["other"]) == ["build", "self-review"], f"without a spec, the work before a skill stays build: {phases}"


def short_gaps_add_up(home):
    rows = [user(1, "go", "a")] + [row for i in range(31) for row in assistant(1 + 2 * i // 60, f"m{i}", "a", [TEXT], second=2 * i % 60)]
    found = session(home, rows)
    assert found["phases"]["a"]["build"]["minutes"] == 1.0, found["phases"]["a"]


for test in (phases_per_branch_and_skill, a_rename_keeps_the_phase, what_ends_a_phase, branches_started_by_commands,
             writing_the_spec_marks_the_work_before_it, short_gaps_add_up):
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
