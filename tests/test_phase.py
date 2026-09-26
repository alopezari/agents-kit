#!/usr/bin/env python3
"""bin/phase: each step of the flow, stepping back when the change moves, branch renames, and the cached fast path."""
import os
import shutil
import subprocess
import sys
import tempfile
import time

PHASE = os.path.expanduser("~/.agents/bin/phase")
STAMP = os.path.expanduser("~/.agents/hooks/review_stamp.py")
SPEC_PATH = os.path.expanduser("~/.agents/skills/spec/path.sh")


def sh(cwd, *cmd, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env).stdout.strip()


def phase(repo, env=None, refresh=True):
    return sh(repo, PHASE, *(["--refresh"] if refresh else []), env=env)


def status_line_names_the_branch(base):
    # A session opened in a checkout left on another task's branch must see whose flow it is.
    statusline = os.path.expanduser("~/.agents/adapters/claude/statusline.sh")
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "trunk")
    open(os.path.join(repo, "app.py"), "w").write("x = 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    env = {**os.environ, "CHIRP_STATUSLINE_NESTED": "1"}  # no profile segments

    def line():
        phase(repo)
        payload = '{"model": {"display_name": "M"}, "cwd": "%s"}' % repo
        return subprocess.run([statusline], input=payload, capture_output=True, text=True, env=env).stdout

    assert "flow" not in line(), "no flow on the default branch"
    # Guessing a ticket ID with a pattern named "python-3-upgrade" as "python-3" and "A8C-1090" as "C-1090".
    for branch, label in (("26-09/qit-1090-exclude-local-runs", "qit-1090-exclude-lo…"),
                          ("feature/python-3-upgrade", "python-3-upgrade"),
                          ("team/A8C-1090", "A8C-1090"),
                          ("abcdefghijklmnopqrst", "abcdefghijklmnopqrst"),  # 20 characters: kept whole
                          ("abcdefghijklmnopqrstu", "abcdefghijklmnopqrs…")):
        sh(repo, "git", "checkout", "-q", "-b", branch)
        assert f"flow {label}: spec" in line(), (branch, line())


def walks_the_flow(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "trunk")
    open(os.path.join(repo, "app.py"), "w").write("x = 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    assert phase(repo) == "", "no phase on the default branch"

    sh(repo, "git", "checkout", "-q", "-b", "feature/cart")
    assert phase(repo) == "spec"
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# Spec\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    assert phase(repo) == "build"
    sh(repo, "python3", STAMP, "write", "--kind", "verify")
    assert phase(repo) == "self-review"
    sh(repo, "python3", STAMP, "write", "--kind", "review")
    assert phase(repo) == "validate", "app.py is a behavior change, so it needs validation"
    sh(repo, "python3", STAMP, "write", "--kind", "validate")
    assert phase(repo) == "create-pr"

    guide = os.path.join(os.path.dirname(spec), "staging-guide-" + os.path.basename(spec)[len("spec-"):])
    open(guide, "w").write("# Staging guide\n1. Check the cart\n")
    assert phase(repo) == "staging (you)"
    open(guide, "a").write("\n## Results (2026-09-24)\n1. FAIL: total wrong\n")
    assert phase(repo) == "staging: fix"
    open(guide, "a").write("\n## Results (2026-09-25)\n1. PASS\n")
    assert phase(repo) == "create-pr", "only the latest round of results counts"

    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    assert phase(repo) == "build", "an edit after the stamps sends the change back to build"

    fake = os.path.join(base, "bin")
    os.makedirs(fake)
    open(os.path.join(fake, "gh"), "w").write('#!/bin/sh\necho "$PR_STATE"\n')
    os.chmod(os.path.join(fake, "gh"), 0o755)
    open(guide.replace("staging-guide-", "follow-pr-"), "w").write("# follow-pr\n")
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PR_STATE": "OPEN"}
    assert phase(repo, env) == "PR open"
    env["PR_STATE"] = "MERGED"
    assert phase(repo, env) == "PR open", "the PR state is cached for a few minutes, not asked on every refresh"
    os.remove(os.path.join(repo, ".git", "agents", "phase", "feature-cart.json"))
    assert phase(repo, env) == "ship"


def new_repo(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "trunk")
    open(os.path.join(repo, "app.py"), "w").write("x = 1\n")
    sh(repo, "git", "add", "-A")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return repo


def no_spec_is_flagged_not_a_gate(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/hotfix")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    assert phase(repo) == "build (no spec)", "code without a spec is being built, not specced"
    sh(repo, "python3", STAMP, "write", "--kind", "verify")
    sh(repo, "python3", STAMP, "write", "--kind", "review")
    assert phase(repo) == "validate (no spec)"


def staging_hand_off_shows_despite_stale_checks(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/cart")
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# Spec\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    sh(repo, "python3", STAMP, "write", "--kind", "review")
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    sh(repo, "python3", STAMP, "write", "--kind", "validate")
    open(spec.replace("spec-shop-", "staging-guide-shop-"), "w").write("# Staging guide\n1. Check the cart\n")
    assert phase(repo) == "staging (you) · redo verify, self-review", \
        "the session waits on the user's staging test; the stale checks are listed, not shown as the phase"
    open(os.path.join(repo, "app.py"), "a").write("w = 4\n")
    assert phase(repo) == "build", "a guide for an older change is not a hand-off"


def red_verify_after_self_review_stays_at_the_furthest_step(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/cart")
    open(sh(repo, SPEC_PATH), "w").write("# Spec\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    sh(repo, "python3", STAMP, "write", "--kind", "review")
    assert phase(repo) == "validate · redo verify", "self-review is done; a red verify doesn't send it back to build"


def spec_reports_and_stamps_follow_branch_renames(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "session/wary-falcon")
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# SHOP-1: the spec\n")
    guide = spec.replace("spec-shop-", "staging-guide-shop-")
    open(guide, "w").write("# Staging guide\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    sh(repo, "python3", STAMP, "write", "--kind", "verify")

    sh(repo, "git", "branch", "-m", "shop-1/draft")
    sh(repo, "git", "branch", "-m", "shop-1/final")
    moved = sh(repo, SPEC_PATH)
    assert os.path.basename(moved) == "spec-shop-shop-1-final.md", moved
    assert open(moved).read() == "# SHOP-1: the spec\n" and not os.path.exists(spec)
    assert os.path.exists(guide.replace("session-wary-falcon", "shop-1-final")), "reports move with the spec"
    check = subprocess.run(["python3", STAMP, "check", "--kind", "verify"], cwd=repo)
    assert check.returncode == 0, "the verify stamp follows the rename"
    assert phase(repo) == "self-review"


def fast_path_serves_cache_and_refreshes(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "trunk")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    sh(repo, "git", "checkout", "-q", "-b", "feature/x")
    assert phase(repo, refresh=False) == "", "first call has no cache yet"
    cache = os.path.join(repo, ".git", "agents", "phase", "feature-x.json")
    for _ in range(50):
        if os.path.exists(cache) and not os.path.exists(cache.replace(".json", ".lock")):
            break
        time.sleep(0.1)
    assert open(cache).read(), "the first call must start a background refresh"
    assert phase(repo, refresh=False) == "spec"
    assert not os.path.exists(cache.replace(".json", ".lock")), "the refresh releases its lock"


RESULTS = []
for test in (status_line_names_the_branch, walks_the_flow, no_spec_is_flagged_not_a_gate, staging_hand_off_shows_despite_stale_checks,
             red_verify_after_self_review_stays_at_the_furthest_step, spec_reports_and_stamps_follow_branch_renames,
             fast_path_serves_cache_and_refreshes):
    base = tempfile.mkdtemp(prefix="agents-test-phase-")
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
