#!/usr/bin/env python3
"""bin/browse, the browser A/B harness: alternation, what each event logs, and closing a run.

Runs against a throwaway HOME and fake tools, so the real A/B log and browsers are never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.expanduser("~/.agents")
RESULTS = []


def run(cmd, cwd, env):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def setup(base):
    home = os.path.join(base, "home")
    for rel in ("bin/browse", "bin/repo-name", "skills/spec/path.sh", "hooks/review_stamp.py"):
        os.makedirs(os.path.join(home, ".agents", os.path.dirname(rel)), exist_ok=True)
        os.symlink(os.path.join(KIT, rel), os.path.join(home, ".agents", rel))
    tools = os.path.join(base, "tools")
    os.makedirs(tools)
    for tool in ("playwright-cli", "agent-browser"):
        path = os.path.join(tools, tool)
        open(path, "w").write(f'#!/bin/sh\necho "{tool} $*"\n[ "$1" = fail ] && exit 3\nexit 0\n')
        os.chmod(path, 0o755)
    main = os.path.join(base, "shop")
    os.makedirs(main)
    run(["git", "init", "-q", "-b", "trunk"], main, None)
    run(["git", "commit", "-q", "--allow-empty", "-m", "init"], main, None)
    worktree = os.path.join(base, "shop-worktree-session-xyz")
    run(["git", "worktree", "add", "-q", "-b", "feature/cart", worktree, "trunk"], main, None)
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTS_HARNESS", "CODEX_THREAD_ID", "CLAUDECODE")}
    env.update(HOME=home, PATH=f"{tools}:{env['PATH']}", TMPDIR=os.path.join(base, "tmp"), CLAUDECODE="1")
    os.makedirs(env["TMPDIR"])
    return home, worktree, env


def events(home):
    log = os.path.join(home, ".agents", "logs", "browser-ab.jsonl")
    return [json.loads(line) for line in open(log)] if os.path.exists(log) else []


def full_run_alternates_and_logs(base):
    home, worktree, env = setup(base)
    browse = os.path.join(home, ".agents", "bin", "browse")

    first = run([browse, "assign"], worktree, env).stdout.strip()
    assert first == "playwright-cli", first
    assert run([browse, "assign"], worktree, env).stdout.strip() == first, "a run keeps its tool until finish"
    call = run([browse, "open", "https://example.test"], worktree, env)
    assert call.returncode == 0 and call.stdout == "playwright-cli open https://example.test\n", call
    failed = run([browse, "fail"], worktree, env)
    assert failed.returncode == 3, "the tool's exit code must reach the agent"
    done = run([browse, "finish", "--checks", "4", "--passed", "3", "--tool-issues", "1", "--notes", "slow"], worktree, env)
    assert done.returncode == 0, done.stderr

    second = run([browse, "assign"], worktree, env).stdout.strip()
    assert second == "agent-browser", f"runs must alternate tools, got {second} twice"

    log = events(home)
    assert [e["event"] for e in log] == ["assign", "call", "call", "finish", "assign"], log
    assert {e["run"] for e in log} == {"shop@feature/cart"}, "runs are keyed by repo and branch, not the worktree dir"
    assert {e["harness"] for e in log} == {"claude-code"}, log
    calls = [e for e in log if e["event"] == "call"]
    assert [c["exit"] for c in calls] == [0, 3] and calls[0]["out_bytes"] == len(call.stdout), calls
    finish = log[3]
    assert (finish["checks"], finish["passed"], finish["tool_issues"], finish["notes"]) == (4, 3, 1, "slow"), finish
    assert log[4]["run_id"] != log[0]["run_id"], "a new run gets a new id"


def codex_started_from_claude_is_codex(base):
    home, worktree, env = setup(base)
    run([os.path.join(home, ".agents", "bin", "browse"), "assign"], worktree, {**env, "CODEX_THREAD_ID": "t1"})
    assert events(home)[0]["harness"] == "codex", events(home)


def finish_without_assignment_fails(base):
    home, worktree, env = setup(base)
    done = run([os.path.join(home, ".agents", "bin", "browse"), "finish", "--checks", "1", "--passed", "1"], worktree, env)
    assert done.returncode == 1 and not events(home), (done.returncode, events(home))


for test in (full_run_alternates_and_logs, codex_started_from_claude_is_codex, finish_without_assignment_fails):
    base = tempfile.mkdtemp(prefix="agents-test-browse-")
    try:
        test(base)
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {error}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
sys.exit(1 if any(error for _, error in RESULTS) else 0)
