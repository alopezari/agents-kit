#!/usr/bin/env python3
"""review-mining/outcomes.py and the escape log it reads: per-PR metrics, follow-up candidates, and the joins
to follow-pr's escapes and to agent sessions. GitHub is replaced by canned GraphQL answers."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

KIT = os.path.expanduser("~/.agents")
spec = importlib.util.spec_from_file_location("outcomes", os.path.join(KIT, "review-mining", "outcomes.py"))
outcomes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(outcomes)
NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)


def pr_metrics_count_people_not_me_or_bots(base):
    node = {
        "createdAt": "2026-10-01T10:00:00Z",
        "reviewThreads": {"nodes": [{"comments": {"nodes": [{"author": {"login": login}}]}}
                                    for login in ("reviewer", "me", "coderabbitai[bot]", "reviewer")]},
        "reviews": {"nodes": [
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-10-02T10:00:00Z", "author": {"login": "reviewer"}},
            {"state": "CHANGES_REQUESTED", "submittedAt": "2026-10-01T11:00:00Z", "author": {"login": "copilot-pull-request-reviewer"}},
        ]},
        "commits": {"nodes": [
            {"commit": {"committedDate": "2026-09-30T10:00:00Z", "statusCheckRollup": {"state": "FAILURE"}}},
            {"commit": {"committedDate": "2026-10-01T12:00:00Z", "statusCheckRollup": {"state": "FAILURE"}}},
            {"commit": {"committedDate": "2026-10-03T10:00:00Z", "statusCheckRollup": {"state": "SUCCESS"}}},
        ]},
    }
    metrics = outcomes.pr_metrics("me", node)
    assert metrics == {"review_threads": 2, "changes_requested": 1, "red_pushes": 1, "commits_after_first_review": 1}, \
        f"red pushes count only after opening; the bot's review is not the first review: {metrics}"


def follow_ups_need_a_shared_file_and_a_fix_title(base):
    prs = [{"repo": "o/shop", "number": 1, "merged": "2026-10-01T10:00:00Z", "files": ["src/cart.php", "CHANGELOG.md"]},
           {"repo": "o/shop", "number": 2, "merged": "2026-10-25T10:00:00Z", "files": ["src/cart.php"]}]
    later = [
        {"number": 1, "title": "Fix cart totals", "url": "u1", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 5, "title": "Fix cart rounding", "url": "u5", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 6, "title": "Revert \"Cart totals\"", "url": "u6", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 7, "title": "Fix changelog typo", "url": "u7", "files": {"nodes": [{"path": "CHANGELOG.md"}]}},
        {"number": 8, "title": "Add cart widget", "url": "u8", "files": {"nodes": [{"path": "src/cart.php"}]}},
    ]
    queries = []

    def fake_graphql(host, query):
        queries.append(query)
        return {"f0": {"nodes": later}}, []

    outcomes.graphql = fake_graphql
    outcomes.add_follow_ups("github.com", prs, NOW, [])
    found = [(f["kind"], f["url"]) for f in prs[0]["follow_ups"]]
    assert found == [("fix", "u5"), ("revert", "u6")], \
        f"the PR itself, a shared changelog only, and a non-fix title are not follow-ups: {found}"
    assert prs[1]["follow_ups"] == "pending", "a PR merged less than 14 days ago has no settled follow-ups"
    assert len(queries) == 1 and "merged:2026-10-01T10:00:00Z..2026-10-15T10:00:00Z" in queries[0], queries


def escapes_join_to_their_branch_and_lens(base):
    log = os.path.join(base, "quality.jsonl")
    entries = [
        {"kind": "lens", "name": "tests", "repo": "shop", "branch": "feature/cart"},
        {"kind": "escape", "name": "review", "repo": "shop", "branch": "feature/cart", "verdict": "confirmed",
         "category": "P4.5", "lens": "tests"},
        {"kind": "escape", "name": "bot", "repo": "shop", "branch": "feature/cart", "verdict": "confirmed",
         "category": "C.bug", "lens": "correctness"},
        {"kind": "escape", "name": "ci", "repo": "shop", "branch": "feature/other", "verdict": "confirmed", "lens": "tests"},
    ]
    open(log, "w").write("".join(json.dumps(e) + "\n" for e in entries))
    outcomes.QUALITY_LOG = log
    prs = [{"repo": "o/shop", "branch": "feature/cart"}]
    outcomes.attach_escapes(prs)
    assert [(e["source"], e["lens_ran"]) for e in prs[0]["escapes"]] == [("review", True), ("bot", False)], prs


def sessions_join_by_repo_and_branch(base):
    prs = [{"repo": "o/shop", "branch": "feature/cart", "opened": "2026-10-01T12:00:00Z", "merged": "2026-10-02T12:00:00Z"}]
    sessions = [
        {"project": "~/p/shop-worktree-session-x", "branches": ["session/x", "feature/cart"], "start_iso": "2026-10-01T10:00:00Z",
         "minutes": 30, "tokens": {"in": 100, "out": 50, "cache_read": 9000}},
        {"project": "~/p/shop", "branches": ["feature/cart"], "start_iso": "2026-10-01T11:00:00Z",
         "minutes": 10, "tokens": {"in": 10, "out": 5, "cache_read": 0}},
        {"project": "~/p/blog", "branches": ["feature/cart"], "start_iso": "2026-09-01T00:00:00Z",
         "minutes": 99, "tokens": {"in": 999, "out": 999, "cache_read": 0}},
    ]
    outcomes.attach_sessions(prs, sessions)
    assert prs[0]["sessions"] == {"count": 2, "tokens": 165, "cache_read_tokens": 9000, "minutes": 40,
                                  "hours_to_pr": 2.0, "hours_to_merge": 24.0}, prs[0]["sessions"]


def quality_log_names_the_repo_not_the_worktree(base):
    home = os.path.join(base, "home")
    for rel in ("bin/quality-log", "bin/repo-name"):
        os.makedirs(os.path.join(home, ".agents", "bin"), exist_ok=True)
        os.symlink(os.path.join(KIT, rel), os.path.join(home, ".agents", rel))
    main = os.path.join(base, "shop")
    os.makedirs(main)
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=main, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "i"], cwd=main, check=True)
    worktree = os.path.join(base, "shop-worktree-session-xyz")
    subprocess.run(["git", "worktree", "add", "-q", "-b", "feature/cart", worktree], cwd=main, check=True)
    env = {**os.environ, "HOME": home}
    log = os.path.join(home, ".agents", "bin", "quality-log")
    subprocess.run([log, "lens", "tests", "--findings", "1", "--confirmed", "1"], cwd=worktree, env=env, check=True)
    subprocess.run([log, "escape", "review", "--verdict", "confirmed", "--category", "C.bug", "--lens", "correctness",
                    "--pr", "12"], cwd=worktree, env=env)
    entries = [json.loads(line) for line in open(os.path.join(home, ".agents", "logs", "quality.jsonl"))]
    assert entries[0]["repo"] == "shop", f"escapes join to lens runs by repo, so a worktree must log the repo: {entries[0]}"
    assert [(e["kind"], e.get("pr")) for e in entries] == [("lens", None), ("escape", 12)], entries


RESULTS = []
for test in (pr_metrics_count_people_not_me_or_bots, follow_ups_need_a_shared_file_and_a_fix_title,
             escapes_join_to_their_branch_and_lens, sessions_join_by_repo_and_branch,
             quality_log_names_the_repo_not_the_worktree):
    base = tempfile.mkdtemp(prefix="agents-test-outcomes-")
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
