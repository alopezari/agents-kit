#!/usr/bin/env python3
"""bin/build-ab, the build-model A/B: alternation per tier, one arm per branch, what stays out of the test.

Runs against a throwaway HOME, so the real A/B log is never touched.
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

KIT = os.path.expanduser("~/.agents")
RESULTS = []


def run(cmd, cwd, env):
    return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True)


def setup(base, branches=("a",)):
    home = os.path.join(base, "home")
    for rel in ("bin/build-ab", "bin/repo-name", "hooks/hooklog.py"):
        os.makedirs(os.path.join(home, ".agents", os.path.dirname(rel)), exist_ok=True)
        os.symlink(os.path.join(KIT, rel), os.path.join(home, ".agents", rel))
    main = os.path.join(base, "shop")
    os.makedirs(main)
    run(["git", "init", "-q", "-b", "trunk"], main, None)
    run(["git", "commit", "-q", "--allow-empty", "-m", "init"], main, None)
    trees = {}
    for branch in branches:
        trees[branch] = os.path.join(base, f"shop-worktree-{branch}")
        run(["git", "worktree", "add", "-q", "-b", branch, trees[branch], "trunk"], main, None)
    env = {k: v for k, v in os.environ.items() if k not in ("AGENTS_HARNESS", "CODEX_THREAD_ID", "CLAUDECODE")}
    env.update(HOME=home, CLAUDECODE="1")
    return home, trees, main, env


def log_path(home):
    return os.path.join(home, ".agents", "logs", "build-ab.jsonl")


def events(home):
    return [json.loads(line) for line in open(log_path(home))] if os.path.exists(log_path(home)) else []


def assign(home, cwd, env, tier):
    return run([os.path.join(home, ".agents", "bin", "build-ab"), "assign", tier], cwd, env)


def arms_alternate_per_tier(base):
    home, trees, _, env = setup(base, ("a", "b", "c", "d"))
    got = [assign(home, trees[b], env, tier).stdout.strip()
           for b, tier in (("a", "standard"), ("b", "standard"), ("c", "low"), ("d", "standard"))]
    assert got == ["sonnet", "opus", "sonnet", "sonnet"], got
    first = events(home)[0]
    assert {k: first[k] for k in ("event", "repo", "branch", "tier", "model", "harness")} == \
        {"event": "assign", "repo": "shop", "branch": "a", "tier": "standard", "model": "sonnet", "harness": "claude-code"}, first
    assert first["ts"], first
    assert len(events(home)) == 4, events(home)


def a_branch_keeps_its_arm(base):
    home, trees, _, env = setup(base, ("a", "b"))
    assign(home, trees["a"], env, "standard")
    assert assign(home, trees["b"], env, "standard").stdout.strip() == "opus"
    again = assign(home, trees["b"], env, "low")
    assert again.returncode == 0 and again.stdout.strip() == "opus", (again.stdout, again.stderr)
    assert len(events(home)) == 2, "a branch's second assign logs nothing"


def what_stays_out_of_the_test(base):
    home, trees, main, env = setup(base)
    high = assign(home, trees["a"], env, "high")
    assert high.returncode == 0 and high.stdout.strip() == "opus" and not events(home), (high.stdout, events(home))
    bad = assign(home, trees["a"], env, "medium")
    assert bad.returncode == 2 and "medium" in bad.stderr and not events(home), (bad.returncode, bad.stderr)
    run(["git", "checkout", "-q", "--detach"], main, None)
    detached = assign(home, main, env, "standard")
    assert detached.returncode == 2 and "branch" in detached.stderr and not events(home), (detached.returncode, detached.stderr)


def a_broken_line_is_skipped_and_counted(base):
    home, trees, _, env = setup(base, ("a",))
    os.makedirs(os.path.dirname(log_path(home)))
    with open(log_path(home), "w") as fh:
        fh.write('{"event": "assign", "repo": "shop", "branch": "old", "tier": "standard", "model": "sonnet"}\n{"event": "ass\n[1]\n')
    got = assign(home, trees["a"], env, "standard")
    assert got.returncode == 0 and got.stdout.strip() == "opus", (got.stdout, got.stderr)
    assert "2 lines" in got.stderr, got.stderr


def a_line_cut_short_keeps_the_next_event_whole(base):
    home, trees, _, env = setup(base, ("a", "b"))
    os.makedirs(os.path.dirname(log_path(home)))
    with open(log_path(home), "w") as fh:
        fh.write('{"event": "assign", "repo": "shop", "branch": "old", "tier": "standard", "model": "sonnet"}\n{"event": "ass')
    assert assign(home, trees["a"], env, "standard").stdout.strip() == "opus"
    assert assign(home, trees["b"], env, "standard").stdout.strip() == "sonnet", "a's event was counted"


def the_arm_behind_goes_next_until_level(base):
    home, trees, _, env = setup(base, ("a", "b", "c"))
    os.makedirs(os.path.dirname(log_path(home)))
    with open(log_path(home), "w") as fh:
        fh.writelines(json.dumps({"event": "assign", "repo": "shop", "branch": f"old{i}", "tier": "low", "model": "sonnet"}) + "\n"
                      for i in range(2))
    got = [assign(home, trees[b], env, "low").stdout.strip() for b in ("a", "b", "c")]
    assert got == ["opus", "opus", "sonnet"], got


def concurrent_assigns_split_the_arms(base):
    branches = ("a", "b", "c", "d")
    home, trees, _, env = setup(base, branches)
    procs = [subprocess.Popen([os.path.join(home, ".agents", "bin", "build-ab"), "assign", "standard"], cwd=trees[b], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for b in branches]
    got = sorted(p.communicate(timeout=30)[0].strip() for p in procs)
    assert got == ["opus", "opus", "sonnet", "sonnet"], got
    assert sorted(e["branch"] for e in events(home)) == list(branches), events(home)


def each_repo_has_its_own_branches(base):
    home, trees, main, env = setup(base, ("a",))
    cafe = os.path.join(base, "cafe")
    os.makedirs(cafe)
    run(["git", "init", "-q", "-b", "a"], cafe, None)
    run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cafe, None)
    assert assign(home, trees["a"], env, "standard").stdout.strip() == "sonnet"
    assert assign(home, cafe, env, "standard").stdout.strip() == "opus", "branch a in another repo is another task"
    assert [(e["repo"], e["branch"]) for e in events(home)] == [("shop", "a"), ("cafe", "a")], events(home)


def a_repo_without_a_name_is_refused(base):
    home, trees, _, env = setup(base)
    nameless = os.path.join(home, ".agents", "bin", "repo-name")
    os.remove(nameless)
    with open(nameless, "w") as fh:
        fh.write("#!/bin/sh\nexit 1\n")
    os.chmod(nameless, 0o755)
    got = assign(home, trees["a"], env, "standard")
    assert got.returncode == 2 and "repo" in got.stderr and not events(home), (got.returncode, got.stderr, events(home))


def a_broken_line_is_counted_for_a_known_branch_too(base):
    home, trees, _, env = setup(base, ("a",))
    os.makedirs(os.path.dirname(log_path(home)))
    with open(log_path(home), "w") as fh:
        fh.write('{"event": "assign", "repo": "shop", "branch": "a", "tier": "low", "model": "opus"}\n{"event": "ass\n')
    got = assign(home, trees["a"], env, "standard")
    assert got.stdout.strip() == "opus" and "1 line" in got.stderr, (got.stdout, got.stderr)


def an_assign_waits_for_the_log_lock(base):
    """Counting and appending happen under one lock: while another assign holds it, this one neither reads nor writes."""
    home, trees, _, env = setup(base, ("a", "b"))
    os.makedirs(os.path.dirname(log_path(home)))
    held = open(log_path(home), "a")
    fcntl.flock(held, fcntl.LOCK_EX)
    waiting = subprocess.Popen([os.path.join(home, ".agents", "bin", "build-ab"), "assign", "standard"], cwd=trees["a"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    assert waiting.poll() is None and not events(home), "assign went ahead while the log was locked"
    held.write(json.dumps({"event": "assign", "repo": "shop", "branch": "b", "tier": "standard", "model": "sonnet"}) + "\n")
    held.flush()
    fcntl.flock(held, fcntl.LOCK_UN)
    held.close()
    assert waiting.communicate(timeout=30)[0].strip() == "opus", "it counts the line written while it waited"


def codex_is_logged_as_codex(base):
    home, trees, _, env = setup(base)
    assign(home, trees["a"], {**env, "CODEX_THREAD_ID": "t1"}, "low")
    assert events(home)[0]["harness"] == "codex", events(home)


for test in (arms_alternate_per_tier, a_branch_keeps_its_arm, what_stays_out_of_the_test,
             a_broken_line_is_skipped_and_counted, each_repo_has_its_own_branches, a_repo_without_a_name_is_refused,
             a_broken_line_is_counted_for_a_known_branch_too, a_line_cut_short_keeps_the_next_event_whole,
             the_arm_behind_goes_next_until_level, concurrent_assigns_split_the_arms, an_assign_waits_for_the_log_lock, codex_is_logged_as_codex):
    base = tempfile.mkdtemp(prefix="agents-test-build-ab-")
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
