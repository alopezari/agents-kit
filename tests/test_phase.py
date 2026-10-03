#!/usr/bin/env python3
"""bin/phase: each step of the flow, stepping back when the change moves, branch renames, and the cached fast path."""
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# Branch renames append to the quality log under $HOME: a home of our own keeps them out of the real one.
KIT = os.path.realpath(os.path.expanduser("~/.agents"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-phase-home-")
os.makedirs(os.path.expanduser("~/.agents/logs"))
os.environ["TMPDIR"] = os.path.expanduser("~/tmp")  # path.sh moves specs out of $TMPDIR: never the real one
os.makedirs(os.environ["TMPDIR"])
for entry in set(os.listdir(KIT)) - {"logs"}:
    os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
os.environ.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
QUALITY_LOG = os.path.expanduser("~/.agents/logs/quality.jsonl")
PHASE = os.path.expanduser("~/.agents/bin/phase")
STAMP = os.path.expanduser("~/.agents/hooks/review_stamp.py")
SPEC_PATH = os.path.expanduser("~/.agents/skills/spec/path.sh")


def sh(cwd, *cmd, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env).stdout.strip()


def stamp(repo, kind):
    """Write a stamp as the kit does: verify kinds only come from the verify runner, which records them in-process,
    and validate needs a report with evidence."""
    if kind.startswith("verify"):
        code = f"import sys; sys.path.insert(0, {os.path.dirname(STAMP)!r}); import review_stamp as r; r.record_verify({kind!r}, r.fingerprint())"
        return sh(repo, "python3", "-c", code)
    if kind == "validate":  # the writer needs a report whose checks name their evidence
        reports = os.path.expanduser("~/.agents/bin/reports")
        with open(sh(repo, reports, "path", "validation"), "w") as fh:
            fh.write("| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n| A1 | app runs | + | PASS | a1.txt |\n")
        evidence = sh(repo, reports, "path", "evidence")
        assert os.path.isdir(evidence), f"no evidence directory from {reports}: {evidence!r}"
        with open(os.path.join(evidence, "a1.txt"), "w") as fh:
            fh.write("$ python3 app.py\nexit 0\n")
    return sh(repo, "python3", STAMP, "write", "--kind", kind)


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
    # Guessing a ticket ID with a pattern named "python-3-upgrade" as "python-3" and "X9Y-1090" as "Y-1090".
    for branch, label in (("26-09/shop-1090-exclude-local-runs", "shop-1090-exclude-l…"),
                          ("feature/python-3-upgrade", "python-3-upgrade"),
                          ("team/X9Y-1090", "X9Y-1090"),
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
    stamp(repo, "verify-empty")
    assert phase(repo) == "self-review · verify checked nothing", "a verify that checked nothing moves on, flagged"
    stamp(repo, "verify")
    assert phase(repo) == "self-review"
    stamp(repo, "review")
    assert phase(repo) == "validate", "app.py is a behavior change, so it needs validation"
    stamp(repo, "validate")
    assert phase(repo) == "create-pr"

    guide = os.path.join(os.path.dirname(spec), "staging-guide-" + os.path.basename(spec)[len("spec-"):])
    open(guide, "w").write("# Staging guide\n## Before the merge\n### S1. Deploy\n## After the merge\n### P1. Backfill\n## Results (2026-09-23)\n")
    assert phase(repo) == "staging (you)", "a Results heading with no results is still pending"
    two = "# Staging guide\n## Before the merge\n### S1. Deploy\n### S2. Check\n## After the merge\n### P1. Backfill\n"
    head = "\n## Results (2026-09-23)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
    for rows, expected, why in [
            ("| S1 | PASS | [out](e/S1.txt) |\n", "staging (you)", "S2 has no result yet"),
            ("| S1 | PASS | [out](e/S1.txt) |\n| S2 | NOT RUN | Expected PASS; waiting for access |\n", "staging (you)", "NOT RUN is pending"),
            ("| S1 | PASS | no FAIL line |\n| S2 | NOT RUN | need to check there is no FAIL line |\n", "staging (you)", "only the Result column counts"),
            ("| S1 | PASS | [out](e/S1.txt) |\n| S2 | FAIL | [out](e/S2.txt) |\n", "staging: fix", "a FAIL row"),
            ("| S1 | PASS | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n", "staging: no evidence", "no saved files"),
            ("| S1 | PASS | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n", "staging: re-run",
             "every S step passed, but no staging stamp covers this change")]:
        if expected == "staging: re-run":
            evidence = sh(repo, os.path.expanduser("~/.agents/bin/reports"), "path", "evidence")
            for name in ("S1.txt", "S2.txt"):
                open(os.path.join(evidence, name), "w").write("$ step\nexit 0\n")
        open(guide, "w").write(two + head + rows)
        assert phase(repo) == expected, why
    passed = two + head + "| S1 | PASS | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n"
    stamp(repo, "staging")
    assert phase(repo) == "create-pr", "every S step passed for this exact change"
    os.remove(os.path.join(repo, ".git", "agents", "stamps", "feature~cart", "staging.stamp"))  # the guides below need none
    open(guide, "w").write("# Staging guide\n## Before the merge\nNothing staging can prove.\n## After the merge\n### P1. Backfill\n")
    assert phase(repo) == "create-pr", "a guide with steps only after the merge doesn't hold the PR"
    open(guide, "w").write("# Staging guide\n1. Check the cart\n")
    assert phase(repo) == "staging (you)"
    open(guide, "a").write("\n## Results (2026-09-24)\n1. FAIL: total wrong\n")
    assert phase(repo) == "staging: fix"
    open(guide, "a").write("\n## Results (2026-09-25)\n1. PASS\n")
    assert phase(repo) == "create-pr", "only the latest round of results counts"
    table = "\n## Results (2026-09-26)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
    open(guide, "a").write(table + "| S1 | PASS | no FAIL line in [output](e/S1.txt) |\n| S2 | FAIL | total wrong: [output](e/S2.txt) |\n")
    assert phase(repo) == "staging: fix", "a FAIL row"
    open(guide, "a").write(table + "| S1 | PASS | no FAIL line in [output](e/S1.txt) |\n| S2 | PASS | [output](e/S2.txt) |\n")
    assert phase(repo) == "create-pr", "a PASS row whose evidence mentions FAIL is still a pass"

    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    assert phase(repo) == "build", "an edit after the stamps sends the change back to build"

    fake = os.path.join(base, "bin")
    os.makedirs(fake)
    open(os.path.join(fake, "gh"), "w").write('#!/bin/sh\necho "$PR_STATE"\n')
    os.chmod(os.path.join(fake, "gh"), 0o755)
    open(guide.replace("staging-guide-", "follow-pr-"), "w").write("# follow-pr\n")
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PR_STATE": "OPEN"}
    assert phase(repo, env) == "PR open · redo verify, self-review, validate", "z = 3 is covered by no check"
    for kind in ("verify", "review", "validate"):
        stamp(repo, kind)
    assert phase(repo, env) == "PR open"
    open(guide, "w").write(passed)
    stamp(repo, "staging")
    open(os.path.join(repo, "app.py"), "a").write("w = 4\n")
    for kind in ("verify", "review", "validate"):
        stamp(repo, kind)
    assert phase(repo, env) == "PR open · redo staging", "w = 4 came after the staging run"
    for rows in ("| S1 | PASS | [out](e/S1.txt) |\n| S2 | FAIL | [out](e/S2.txt) |\n", "| S1 | PASS | [out](e/S1.txt) |\n"):
        open(guide, "w").write(two + head + rows)
        assert phase(repo, env) == "PR open · redo staging", f"a re-run that hasn't passed yet: {rows!r}"
    open(guide, "w").write(passed)
    stamp(repo, "staging")
    assert phase(repo, env) == "PR open"
    env["PR_STATE"] = "MERGED"
    assert phase(repo, env) == "PR open", "the PR state is cached for a few minutes, not asked on every refresh"
    os.remove(os.path.join(repo, ".git", "agents", "phase", "feature~cart.json"))
    assert phase(repo, env) == "ship"


def pr_opened_without_follow_pr(base):
    # A PR opened by hand has no follow-pr report; its staging guide without results showed "staging (you)".
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/cart")
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# Spec\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    for kind in ("verify", "review", "validate"):
        stamp(repo, kind)
    open(spec.replace("spec-shop-", "staging-guide-shop-"), "w").write("# Staging guide\n1. Check the cart\n")
    fake = os.path.join(base, "bin")
    os.makedirs(fake)
    open(os.path.join(fake, "gh"), "w").write('#!/bin/sh\necho "$PR_STATE"\necho called >> "$GH_CALLS"\n')
    os.chmod(os.path.join(fake, "gh"), 0o755)
    calls = os.path.join(base, "gh-calls")
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}", "PR_STATE": "OPEN", "GH_CALLS": calls}
    assert phase(repo, env) == "staging (you)", "a branch that was never pushed has no PR"
    assert not os.path.exists(calls), "and gh isn't asked about it"

    remote = os.path.join(base, "remote.git")
    sh(base, "git", "init", "-q", "--bare", remote)
    sh(repo, "git", "remote", "add", "origin", remote)
    sh(repo, "git", "push", "-q", "origin", "trunk")
    sh(repo, "git", "fetch", "-q", "origin")
    sh(repo, "git", "branch", "-q", "--set-upstream-to", "origin/trunk")
    os.remove(os.path.join(repo, ".git", "agents", "phase", "feature~cart.json"))
    assert phase(repo, env) == "staging (you)" and not os.path.exists(calls), \
        "tracking origin/trunk, as a branch started from it does, isn't being pushed"
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "cart")
    sh(repo, "git", "push", "-q", "-u", "origin", "feature/cart")
    for kind in ("verify", "review", "validate"):
        stamp(repo, kind)
    assert phase(repo, env) == "PR open"
    env["PR_STATE"] = ""
    os.remove(os.path.join(repo, ".git", "agents", "phase", "feature~cart.json"))
    assert phase(repo, env) == "staging (you)", "a pushed branch with no PR is still in the flow"
    assert phase(repo, env) == "staging (you)"
    assert len(open(calls).read().split()) == 2, "no PR is cached like any other answer"


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
    stamp(repo, "verify")
    stamp(repo, "review")
    assert phase(repo) == "validate (no spec)"


def staging_hand_off_shows_despite_stale_checks(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/cart")
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# Spec\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    stamp(repo, "review")
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    stamp(repo, "validate")
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
    stamp(repo, "review")
    assert phase(repo) == "validate · redo verify", "self-review is done; a red verify doesn't send it back to build"


def spec_reports_and_stamps_follow_branch_renames(base):
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "session/wary-falcon")
    spec = sh(repo, SPEC_PATH)
    open(spec, "w").write("# SHOP-1: the spec\n")
    guide = spec.replace("spec-shop-", "staging-guide-shop-")
    open(guide, "w").write("# Staging guide\n")
    evidence = sh(repo, os.path.expanduser("~/.agents/bin/reports"), "path", "evidence")
    assert os.path.isdir(evidence), evidence
    open(os.path.join(evidence, "a1.txt"), "w").write("$ true\n")
    validation = spec.replace("spec-shop-", "validation-shop-")
    open(validation, "w").write(f"| A1 | x | + | PASS | [out]({os.path.basename(evidence)}/a1.txt) |\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    stamp(repo, "verify")

    sh(repo, "git", "branch", "-m", "shop-1/draft")
    sh(repo, "git", "branch", "-m", "shop-1/final")
    moved = sh(repo, SPEC_PATH)
    assert os.path.basename(moved) == "spec-shop-shop-1~final.md", moved
    assert open(moved).read() == "# SHOP-1: the spec\n" and not os.path.exists(spec)
    assert os.path.exists(guide.replace("session~wary-falcon", "shop-1~final")), "reports move with the spec"
    assert open(os.path.join(evidence.replace("session~wary-falcon", "shop-1~final"), "a1.txt")).read() == "$ true\n", \
        "the evidence moves with its report"
    moved_validation = validation.replace("session~wary-falcon", "shop-1~final")
    assert "(evidence-shop-shop-1~final/a1.txt)" in open(moved_validation).read(), "its links follow the move"
    check = subprocess.run(["python3", STAMP, "check", "--kind", "verify"], cwd=repo)
    assert check.returncode == 0, "the verify stamp follows the rename"
    assert phase(repo) == "self-review"
    renames = [json.loads(line) for line in (open(QUALITY_LOG) if os.path.exists(QUALITY_LOG) else []) if '"rename"' in line]
    assert [(r["repo"], r["name"], r["to"]) for r in renames] == [("shop", "session/wary-falcon", "shop-1/final")], \
        f"lens runs logged on the session branch must still join to the PR's branch: {renames}"


def a_rename_log_that_cannot_be_written_leaves_the_spec_path(base):
    # A sandbox that can't write ~/.agents/logs: the log raised and path.sh printed a traceback, no path.
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "session/x")
    open(sh(repo, SPEC_PATH), "w").write("# spec\n")
    sh(repo, "git", "branch", "-m", "feature/x")
    open(QUALITY_LOG, "a").close()
    os.chmod(QUALITY_LOG, 0o444)
    try:
        result = subprocess.run([SPEC_PATH], cwd=repo, capture_output=True, text=True)
    finally:
        os.chmod(QUALITY_LOG, 0o644)
    assert result.returncode == 0 and result.stdout.strip().endswith("spec-shop-feature~x.md"), result
    assert "could not log the rename" in result.stderr, result.stderr


def slash_and_dash_branches_keep_their_own_files(base):
    # Both used to be keyed "feature-x": the second branch saw the first one's spec and stamps.
    repo = new_repo(base)
    sh(repo, "git", "checkout", "-q", "-b", "feature/x")
    open(sh(repo, SPEC_PATH), "w").write("# slash\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    stamp(repo, "review")
    sh(repo, "git", "stash", "-q")
    sh(repo, "git", "checkout", "-q", "-b", "feature-x", "trunk")
    sh(repo, "git", "stash", "pop", "-q")
    assert not os.path.exists(sh(repo, SPEC_PATH)), "the dash branch has no spec of its own yet"
    check = subprocess.run(["python3", STAMP, "check"], cwd=repo, capture_output=True)
    assert check.returncode == 1, "the slash branch's review stamp doesn't cover the dash branch"


def files_under_the_old_dash_key_move_unless_that_branch_exists(base):
    repo = new_repo(base)
    agents = os.path.join(repo, ".git", "agents")
    os.makedirs(os.path.join(agents, "stamps", "feature-y"))
    os.makedirs(os.path.join(agents, "phase"))
    json.dump({"pr": "MERGED"}, open(os.path.join(agents, "phase", "feature-y.json"), "w"))
    open(os.path.join(agents, "spec-shop-feature-y.md"), "w").write("# old key\n")
    open(os.path.join(agents, "review-shop-feature-y.md"), "w").write("# review\n")
    sh(repo, "git", "checkout", "-q", "-b", "feature/y")
    assert open(sh(repo, SPEC_PATH)).read() == "# old key\n", "a spec saved under the dash key moves to the new key"
    assert os.path.exists(os.path.join(agents, "review-shop-feature~y.md")) and os.path.isdir(os.path.join(agents, "stamps", "feature~y"))
    assert json.load(open(os.path.join(agents, "phase", "feature~y.json"))) == {"pr": "MERGED"}, "the phase cache moves too"

    open(os.path.join(agents, "spec-shop-feature-z.md"), "w").write("# the dash branch's\n")
    sh(repo, "git", "branch", "feature-z", "trunk")
    sh(repo, "git", "checkout", "-q", "-b", "feature/z")
    assert not os.path.exists(sh(repo, SPEC_PATH)), "a real feature-z branch keeps its files"
    assert os.path.exists(os.path.join(agents, "spec-shop-feature-z.md"))

    open(os.path.join(agents, "spec-shop-feature-w.md"), "w").write("# old key\n")
    os.chmod(agents, 0o555)  # a sandbox's read-only .git: the move fails and the spec goes to $TMPDIR
    try:
        sh(repo, "git", "checkout", "-q", "-b", "feature/w")
        env = {**os.environ, "TMPDIR": os.path.join(base, "tmp")}
        os.makedirs(env["TMPDIR"])
        found = sh(repo, SPEC_PATH, env=env)
        assert found.startswith(env["TMPDIR"]), found
        sh(repo, "git", "checkout", "-q", "-b", "feature/v")
        found = sh(repo, SPEC_PATH, env=env)
        open(found, "w").write("# written in the sandbox\n")
        open(found.replace("/spec-", "/review-"), "w").write("# review\n")
        again = subprocess.run([SPEC_PATH], cwd=repo, capture_output=True, text=True, env=env)
        assert again.stdout.strip() == found, "while .git stays read-only, the $TMPDIR spec is the one"
        assert again.stderr == "", f"and quietly, on every call: {again.stderr}"
    finally:
        os.chmod(agents, 0o755)
    moved = sh(repo, SPEC_PATH, env=env)
    assert moved == os.path.join(os.path.realpath(agents), "spec-shop-feature~v.md"), moved
    assert open(moved).read() == "# written in the sandbox\n" and not os.path.exists(found), "macOS clears $TMPDIR"
    assert os.path.exists(os.path.join(agents, "review-shop-feature~v.md")), "its reports move with it"


def fast_path_serves_cache_and_refreshes(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "trunk")
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    sh(repo, "git", "checkout", "-q", "-b", "feature/x")
    assert phase(repo, refresh=False) == "", "first call has no cache yet"
    cache = os.path.join(repo, ".git", "agents", "phase", "feature~x.json")
    for _ in range(50):
        if os.path.exists(cache) and not os.path.exists(cache.replace(".json", ".lock")):
            break
        time.sleep(0.1)
    assert open(cache).read(), "the first call must start a background refresh"
    assert phase(repo, refresh=False) == "spec"
    assert not os.path.exists(cache.replace(".json", ".lock")), "the refresh releases its lock"


TESTS = (status_line_names_the_branch, walks_the_flow, pr_opened_without_follow_pr, no_spec_is_flagged_not_a_gate, staging_hand_off_shows_despite_stale_checks,
         red_verify_after_self_review_stays_at_the_furthest_step, spec_reports_and_stamps_follow_branch_renames,
         a_rename_log_that_cannot_be_written_leaves_the_spec_path,
         slash_and_dash_branches_keep_their_own_files, files_under_the_old_dash_key_move_unless_that_branch_exists,
         fast_path_serves_cache_and_refreshes)

if sys.argv[1:]:
    by_name = {t.__name__: t for t in TESTS}
    RESULTS = []
    for name in sys.argv[1:]:
        base = tempfile.mkdtemp(prefix="agents-test-phase-")
        try:
            by_name[name](base)
            RESULTS.append((name, None))
        except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
            RESULTS.append((name, f"{type(error).__name__}: {error}"))
        finally:
            shutil.rmtree(base, ignore_errors=True)
    shutil.rmtree(os.environ["HOME"], ignore_errors=True)
    for name, error in RESULTS:
        print(f"{'FAIL' if error else 'ok  '} {name}")
        if error:
            print(f"     {error}")
    sys.exit(1 if any(error for _, error in RESULTS) else 0)

# Each test in a process of its own: the tests share this HOME's quality log, which renames append to.
def run_alone(t):
    done = subprocess.run([sys.executable, os.path.abspath(__file__), t.__name__], capture_output=True, text=True,
                          errors="replace")
    # Passed only when it says so and exits 0: a test that exits early, 0 or not, never reported.
    passed = done.returncode == 0 and f"ok   {t.__name__}\n" in done.stdout
    if passed:
        return True, done.stdout
    said = "" if f"FAIL {t.__name__}" in done.stdout else f"FAIL {t.__name__}\n     exit {done.returncode}, no result\n"
    return False, said + done.stdout + done.stderr


with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
    outcomes = list(pool.map(run_alone, TESTS))
shutil.rmtree(os.environ["HOME"], ignore_errors=True)
print("".join(out for _, out in outcomes), end="")
failed = sum(not passed for passed, _ in outcomes)
print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
sys.exit(1 if failed else 0)
