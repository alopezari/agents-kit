#!/usr/bin/env python3
"""review-mining/outcomes.py and the escape log it reads: per-PR metrics, follow-up candidates, and the joins
to follow-pr's escapes and to agent sessions. GitHub is replaced by canned GraphQL answers."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import contextlib
import importlib.util
import io
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
    prs = [{"repo": "o/shop", "number": 1, "merged": "2026-10-01T10:00:00Z", "files": ["src/cart.php", "CHANGELOG.md", "changelog.d/cart.md"]},
           {"repo": "o/shop", "number": 2, "merged": "2026-10-25T10:00:00Z", "files": ["src/cart.php"]}]
    later = [
        {"number": 1, "title": "Fix cart totals", "url": "u1", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 5, "title": "Fix cart rounding", "url": "u5", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 6, "title": "Revert \"Cart totals\"", "url": "u6", "files": {"nodes": [{"path": "src/cart.php"}]}},
        {"number": 7, "title": "Fix changelog typo", "url": "u7", "files": {"nodes": [{"path": "CHANGELOG.md"}]}},
        {"number": 9, "title": "Fix the entry", "url": "u9", "files": {"nodes": [{"path": "changelog.d/cart.md"}]}},
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
    outcomes.attach_escapes(prs, {})
    assert [(e["source"], e["lens_ran"]) for e in prs[0]["escapes"]] == [("review", True), ("bot", False)], prs

    entries = [
        {"kind": "lens", "name": "spec", "repo": "shop", "branch": "session/x"},
        {"kind": "rename", "repo": "shop", "name": "session/x", "to": "shop-1/draft"},
        {"kind": "rename", "repo": "shop", "name": "shop-1/draft", "to": "feature/cart"},
        {"kind": "lens", "name": "tests", "repo": "shop", "branch": "session/y"},
        {"kind": "rename", "repo": "blog", "name": "session/y", "to": "feature/cart"},
        {"kind": "escape", "name": "review", "repo": "shop", "branch": "feature/cart", "verdict": "confirmed",
         "category": "C.requirement", "lens": "spec"},
        {"kind": "escape", "name": "bot", "repo": "shop", "branch": "feature/cart", "verdict": "confirmed",
         "category": "P4.5", "lens": "tests"},
    ]
    open(log, "w").write("".join(json.dumps(e) + "\n" for e in entries))
    prs = [{"repo": "o/shop", "branch": "feature/cart"}]
    outcomes.attach_escapes(prs, {})
    assert [(e["lens"], e["lens_ran"]) for e in prs[0]["escapes"]] == [("spec", True), ("tests", False)], \
        f"a lens run follows every rename to the PR's branch, and only those: {prs}"


def sessions_join_by_repo_and_branch(base):
    prs = [{"repo": "o/shop", "branch": "feature/cart", "opened": "2026-10-01T12:00:00Z", "merged": "2026-10-02T12:00:00Z"}]
    sessions = [
        {"project": "~/p/shop-worktree-session-x", "repos": {"o/shop": ["shop"]}, "branches": ["session/x", "feature/cart"], "start_iso": "2026-10-01T10:00:00Z",
         "minutes": 30, "tokens": {"in": 100, "out": 50, "cache_read": 9000}, "models": {"opus": 3}, "effort": ["high"],
         "phases": {"session/x": {"build": {"out": 500, "cache_read": 0, "cache_write": 0, "minutes": 9}}, "feature/cart": {"build": {"out": 30, "cache_read": 0, "cache_write": 0, "minutes": 12.5}, "validate": {"out": 20, "cache_read": 0, "cache_write": 0, "minutes": 5}}}},
        {"project": "~/p/shop", "repos": {"O/Shop": ["shop"]}, "branches": ["feature/cart"], "start_iso": "2026-10-01T11:00:00Z",
         "minutes": 10, "tokens": {"in": 10, "out": 5, "cache_read": 0}, "models": {"sonnet": 1, "opus": 1}, "effort": ["medium"],
         "phases": {"feature/cart": {"build": {"out": 5, "cache_read": 0, "cache_write": 0, "minutes": 2}}}},
        {"project": "~/p/blog", "repos": {"o/blog": ["blog"]}, "branches": ["feature/cart"], "start_iso": "2026-09-01T00:00:00Z",
         "minutes": 99, "tokens": {"in": 999, "out": 999, "cache_read": 0}, "models": {"haiku": 1}, "effort": ["low"],
         "phases": {"feature/cart": {"build": {"out": 999, "cache_read": 0, "cache_write": 0, "minutes": 99}}}},
    ]
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    with open(outcomes.QUALITY_LOG, "w") as fh:  # the spec ran on the session branch, renamed before the PR
        fh.write(json.dumps({"kind": "rename", "repo": "shop", "name": "session/x", "to": "feature/cart"}) + "\n")
    outcomes.attach_sessions(prs, sessions)
    only_before = [{**prs[0]}]
    outcomes.attach_sessions(only_before, [{**sessions[0], "branches": ["session/x"]}])
    assert only_before[0]["sessions"] and only_before[0]["sessions"]["phases"]["build"]["out"] == 530, \
        f"a session that ended before the rename still joins: {only_before[0]['sessions']}"
    assert prs[0]["sessions"] == {"count": 2, "tokens": 165, "cache_read_tokens": 9000, "minutes": 40,
                                  "models": ["opus", "sonnet"], "effort": ["high", "medium"],
                                  "hours_to_pr": 2.0, "hours_to_merge": 24.0,
                                  "phases": {"build": {"out": 535, "cache_read": 0, "cache_write": 0, "minutes": 23.5}, "validate": {"out": 20, "cache_read": 0, "cache_write": 0, "minutes": 5}},
                                  "attention": {"prompts": 0, "questions": 0, "corrections": 0, "wait_minutes": 0, "reply_minutes": 0,
                                                "coverage": "partial"}}, prs[0]["sessions"]
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    assert outcomes.summarize([{**prs[0], **counts, "follow_ups": "pending"}])["sessions_joined"]["per_phase"]["validate"]["minutes"] == 5, \
        "phases on the PR's branch and its earlier names only, summed over its sessions in its repo"


def a_checkout_named_unlike_its_repo(base):
    """~/.agents is alopezari/agents-kit: sessions join by origin remote, and the quality log by the checkout's name."""
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    entries = [{"kind": "rename", "repo": ".agents", "name": "session/x", "to": "kit-draft"},
               {"kind": "rename", "repo": "agents-kit", "name": "kit-draft", "to": "kit-change"},  # a clone named after the repo
               {"kind": "lens", "name": "tests", "repo": ".agents", "branch": "session/x"},
               {"kind": "escape", "name": "bot", "repo": ".agents", "branch": "kit-change", "verdict": "confirmed", "lens": "tests"}]
    open(outcomes.QUALITY_LOG, "w").write("".join(json.dumps(e) + "\n" for e in entries))
    prs = [{"repo": "alopezari/agents-kit", "branch": "kit-change", "opened": "2026-10-01T12:00:00Z", "merged": "2026-10-02T12:00:00Z"}]
    sessions = [{"project": "~/.agents-worktree-session-x", "repos": {"alopezari/agents-kit": [".agents", "agents-kit"]}, "branches": ["session/x"],
                 "start_iso": "2026-10-01T10:00:00Z", "minutes": 1, "tokens": {"in": 1, "out": 1, "cache_read": 0},
                 "phases": {"session/x": {"spec": {"out": 7, "cache_read": 0, "cache_write": 0, "minutes": 1}}}},
                {"project": "~/p/fork-of-agents-kit", "repos": {"someone/agents-kit": ["agents-kit"]}, "branches": ["kit-change"],
                 "start_iso": "2026-10-01T10:00:00Z", "minutes": 1, "tokens": {"in": 1, "out": 1, "cache_read": 0}}]
    outcomes.attach_sessions(prs, sessions)
    joined = prs[0]["sessions"] or {}
    assert joined.get("count") == 1 and joined["phases"].get("spec", {}).get("out") == 7, \
        f"the session under the earlier name joins, the fork with the same name doesn't: {prs[0]['sessions']}"
    outcomes.attach_escapes(prs, outcomes.local_names(sessions))
    assert [(e["source"], e["lens_ran"]) for e in prs[0]["escapes"]] == [("bot", True)], prs[0]["escapes"]


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


def attention_per_pr_and_its_coverage(base):
    def share(prompts, wait, questions=0):
        return {"out": 1, "prompts": prompts, "questions": questions, "corrections": 0, "wait_minutes": wait, "reply_minutes": min(wait, 15)}
    prs = [{"repo": "o/shop", "branch": branch, "opened": "2026-10-02T00:00:00Z", "merged": "2026-10-03T00:00:00Z"}
           for branch in ("feature/cart", "fix/tax", "codex/x", "nobody")]
    claude = {"harness": "claude-code", "repos": {"o/shop": ["shop"]}, "start_iso": "2026-10-01T00:00:00Z", "minutes": 1,
              "tokens": {"in": 1, "out": 1, "cache_read": 0}, "models": {}, "effort": []}
    sessions = [
        {**claude, "branches": ["session/x", "feature/cart", "fix/tax"],
         "phases": {"session/x": {"spec": share(1, 0)}, "feature/cart": {"build": share(2, 30, questions=1), "validate": share(1, 5)},
                    "fix/tax": {"build": share(1, 4)}}},
        {**claude, "branches": ["fix/tax"], "phases": {"fix/tax": {"build": share(3, 20), "self-review": share(1, 2)}}},
        {**claude, "repos": {"o/blog": ["blog"]}, "branches": ["feature/cart"], "phases": {"feature/cart": {"build": share(50, 500)}}},
        {**claude, "branches": ["codex/x"], "phases": {"codex/x": {"build": share(1, 1)}}},
        {**claude, "harness": "codex", "branches": ["codex/x"]},
    ]
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    with open(outcomes.QUALITY_LOG, "w") as fh:
        fh.write(json.dumps({"kind": "rename", "repo": "shop", "name": "session/x", "to": "feature/cart"}) + "\n")
    outcomes.attach_sessions(prs, sessions)
    cart, tax, mixed, nobody = (p["sessions"] for p in prs)
    assert cart["attention"] == {"prompts": 4, "questions": 1, "corrections": 0, "wait_minutes": 35, "reply_minutes": 20,
                                 "coverage": "complete"}, f"the renamed branch's spec counts, the blog repo's doesn't: {cart}"
    assert tax["attention"] == {"prompts": 5, "questions": 0, "corrections": 0, "wait_minutes": 26, "reply_minutes": 21,
                                "coverage": "complete"}, tax
    assert mixed["attention"]["coverage"] == "partial", f"a Codex session's share is unknown: {mixed}"
    assert nobody is None
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    summary = outcomes.summarize([{**p, **counts, "follow_ups": "pending"} for p in prs])["sessions_joined"]["attention"]
    assert summary == {"complete_prs": 2, "partial_prs": 1, "unknown_prs": 1,
                       "per_pr": {"prompts": 4.5, "questions": 0.5, "corrections": 0, "wait_minutes": 30.5, "reply_minutes": 20.5},
                       "median_per_pr": {"prompts": 4.5, "questions": 0.5, "corrections": 0, "wait_minutes": 30.5, "reply_minutes": 20.5}}, \
        f"means over complete PRs only, per PR rather than per session: {summary}"
    skewed = [{**prs[0], **counts, "follow_ups": "pending",
               "sessions": {**cart, "attention": {**cart["attention"], "wait_minutes": wait}}} for wait in (1, 2, 900)]
    waits = outcomes.summarize(skewed)["sessions_joined"]["attention"]
    assert (waits["per_pr"]["wait_minutes"], waits["median_per_pr"]["wait_minutes"]) == (301, 2), waits


def a_share_never_measured_is_not_zero(base):
    prs = [{"repo": "o/shop", "branch": branch, "opened": "2026-10-02T00:00:00Z", "merged": "2026-10-03T00:00:00Z"}
           for branch in ("old", "intake")]
    claude = {"harness": "claude-code", "repos": {"o/shop": ["shop"]}, "start_iso": "2026-10-01T00:00:00Z", "minutes": 1,
              "tokens": {"in": 1, "out": 1, "cache_read": 0}, "models": {}, "effort": []}
    no_share = {"in": 1, "out": 5, "cache_read": 0, "cache_write": 0, "minutes": 3}
    sessions = [{**claude, "branches": ["old"], "phases": {"old": {"build": no_share}}},  # a sessions file from before
                {**claude, "branches": ["intake"], "phases": {"intake": {"build": {**dict.fromkeys(no_share, 0), "prompts": 1,
                 "questions": 0, "corrections": 0, "wait_minutes": 0, "reply_minutes": 0}}}}]
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    outcomes.attach_sessions(prs, sessions)
    old, intake = (p["sessions"]["attention"]["coverage"] for p in prs)
    assert (old, intake) == ("partial", "complete"), (old, intake)
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    joined = outcomes.summarize([{**p, **counts, "follow_ups": "pending"} for p in prs])["sessions_joined"]
    assert joined["prs_with_phases"] == 1, f"a PR that spent no tokens leaves the cost per phase alone: {joined}"


def signals_join_by_host_repo_and_number(base):
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    line = {"kind": "pr", "host": "github.com", "remote": "O/Shop", "name": 7, "repo": ".agents", "branch": "feature/cart"}
    entries = [
        {**line, "ts": "2026-10-01T09:00:00+0000", "added": 10},
        {**line, "ts": "2026-10-01T12:00:00+0200", "added": 20},  # 10:00 UTC: the latest before the merge
        {**line, "ts": "2026-10-01T10:00:00-0100", "added": 99},  # 11:00 UTC: after the merge
        # Later than the right line and still before the merge, so only the key keeps them out.
        {**line, "ts": "2026-10-01T10:15:00+0000", "remote": "o/blog", "added": 98},
        {**line, "ts": "2026-10-01T10:15:00+0000", "host": "ghe.example.com", "added": 97},
        {**line, "ts": "2026-10-01T10:15:00+0000", "name": 8, "added": 96},  # same branch, another PR
        {**line, "ts": "2026-10-01T09:00:00+0000", "name": 9, "added": 1},
        {**line, "ts": "2026-10-01T09:00:00+0000", "name": 9, "added": 2},  # same second: the later append wins
        {"kind": "lens", "name": "tests", "repo": ".agents", "branch": "feature/cart", "ts": "2026-10-01T09:45:00+0000"},
    ]
    open(outcomes.QUALITY_LOG, "w").write("".join(json.dumps(e) + "\n" for e in entries))
    prs = [{"host": "github.com", "repo": "o/shop", "number": 7, "branch": "feature/cart", "merged": "2026-10-01T10:30:00Z"},
           {"host": "github.com", "repo": "o/shop", "number": 9, "branch": "feature/cart", "merged": "2026-10-01T10:30:00Z"}]
    outcomes.attach_signals(prs)
    assert (prs[0]["signals"] or {}).get("added") == 20, prs[0]["signals"]
    assert (prs[1]["signals"] or {}).get("added") == 2, prs[1]["signals"]
    prs[1]["number"] = 10
    outcomes.attach_signals(prs)
    assert prs[1]["signals"] is None, f"never matched by branch: {prs[1]['signals']}"


def signal_cells(base):
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    fix = {"kind": "fix", "url": "u", "title": "Fix", "shared_files": ["a"]}
    revert = {**fix, "kind": "revert"}
    confirmed = {"source": "bot", "verdict": "confirmed", "lens": "tests", "lens_ran": True, "category": "P4.5", "pr": 1}

    def signals(size, **fields):
        return {"added": size, "deleted": 0, "tier": "standard", "red_check": "passed", "verify": "pass", "verify_current": True,
                "coverage": {"covered": 8, "measured": 10}, "tampered": {"removed": 0, "loosened": 0, "changed": 0,
                "skipped_or_focused": 0}, "tests_deleted": 0, "test_files": 1, **fields}

    prs = [
        {"signals": signals(49), "follow_ups": [fix, fix], "escapes": [confirmed, confirmed]},
        {"signals": signals(50, red_check="failed", tier="high", coverage={"covered": 7, "measured": 10}),
         "follow_ups": [revert], "escapes": [{**confirmed, "verdict": "rejected"}]},
        {"signals": signals(400, red_check="skipped", coverage=None, tampered={"removed": 1, "loosened": 0, "changed": 0,
                                                                              "skipped_or_focused": 0}),
         "follow_ups": [], "escapes": []},
        {"signals": signals(401, red_check="not_run", tests_deleted=2, test_files=0), "follow_ups": [fix],
         "escapes": [{**confirmed, "pr": 99}]},  # an earlier PR's escape on a reused branch: not this one's
        {"signals": signals(401, red_check=None, test_files=0), "follow_ups": [], "escapes": []},
        {"signals": signals(401, red_check="failed", verify_current=False), "follow_ups": [], "escapes": []},
        {"signals": signals(401, verify_current=None), "follow_ups": [], "escapes": []},  # logged from a dirty tree
        {"signals": signals(None, red_check=None, verify="none", coverage=None, tampered=None, tests_deleted=None,
                            test_files=None, tier=None),
         "follow_ups": [], "escapes": [confirmed]},
        {"signals": signals(10), "follow_ups": "pending", "escapes": [confirmed]},  # not settled: left out
        {"signals": None, "follow_ups": [fix], "escapes": []},
        {"follow_ups": [], "escapes": []},  # a PR the signals were never attached to
    ]
    got = outcomes.summarize_signals([{**counts, "number": 1, **p} for p in prs])

    def cell(prs, fix=0, revert=0, escape=0):
        def rate(n):
            return round(n / prs, 2) if prs else None
        return {"prs": prs, "fix": fix, "fix_rate": rate(fix), "revert": revert, "revert_rate": rate(revert),
                "escape": escape, "escape_rate": rate(escape)}

    assert got["settled_prs"] == 10 and got["without_signals"] == cell(2, fix=1), got
    assert got["size"] == {"<50": cell(1, fix=1, escape=1), "50-400": cell(2, revert=1), ">400": cell(4, fix=1),
                           "unknown": cell(1, escape=1)}, got["size"]
    # Within a band, a signal is set only against PRs of that band: the 400-line PR alone shows its weakened test.
    assert got["by_size"]["50-400"]["tampered"] == {"any": cell(1), "none": cell(1, revert=1), "unknown": cell(0)}, \
        got["by_size"]["50-400"]["tampered"]
    assert got["by_size"]["<50"]["red_check"]["passed"] == cell(1, fix=1, escape=1), got["by_size"]["<50"]
    assert "size" not in got["splits"] and set(got["by_size"]) == {"<50", "50-400", ">400", "unknown"}, got
    splits = got["splits"]
    assert splits["tier"] == {"low": cell(0), "standard": cell(6, fix=2, escape=1), "high": cell(1, revert=1),
                              "unknown": cell(1, escape=1)}, splits["tier"]
    # A red check from a verify the change has outgrown, or not known to cover it, says nothing; nor does its coverage.
    assert splits["red_check"] == {"failed": cell(1, revert=1), "passed": cell(1, fix=1, escape=1),
                                   "skipped_or_not_run": cell(2, fix=1), "not_applicable": cell(1),
                                   "unknown": cell(3, escape=1)}, splits["red_check"]
    assert splits["coverage"] == {"80_or_more": cell(3, fix=2, escape=1), "below_80": cell(1, revert=1),
                                  "none": cell(1), "unknown": cell(3, escape=1)}, splits["coverage"]
    assert splits["tampered"] == {"any": cell(1), "none": cell(6, fix=2, revert=1, escape=1),
                                  "unknown": cell(1, escape=1)}, splits["tampered"]
    assert splits["tests_deleted"] == {"any": cell(1, fix=1), "none": cell(6, fix=1, revert=1, escape=1),
                                       "unknown": cell(1, escape=1)}, splits["tests_deleted"]
    assert splits["test_files"] == {"any": cell(5, fix=1, revert=1, escape=1), "none": cell(2, fix=1),
                                    "unknown": cell(1, escape=1)}, splits["test_files"]



def structure_cells(base):
    """The complexity a PR added and the clones it brought, as structure-facts measured them: a part not measured, or one
    cut short at 0, is unknown; one cut short above 0 keeps its bucket, as it added at least that much."""
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    fix = {"kind": "fix", "url": "u", "title": "Fix", "shared_files": ["a"]}
    revert = {**fix, "kind": "revert"}

    def structure(added, clones, complete=True):
        return {"complexity": None if added is None else {"added": added, "removed": 3, "max_function": added,
                                                          "functions": 2, "complete": complete},
                "new_clones": None if clones is None else {"count": clones, "complete": complete}}

    prs = [({"structure": structure(0, 0)}, [fix]),
           ({"structure": structure(9, 3)}, [revert]),
           ({"structure": structure(10, 0)}, []),
           ({"structure": structure(0, 0, complete=False)}, []),
           ({"structure": structure(12, 2, complete=False)}, [fix]),
           ({"structure": structure(None, None)}, []),
           ({"structure": None}, []),  # logged from a dirty tree, or structure-facts failed
           ({}, []),  # a line from before the kit recorded structure
           ({"added": 500, "structure": structure(1, 1)}, [fix]),
           # Each part on its own: one not measured, or cut short at 0, leaves the other's bucket alone.
           ({"structure": {**structure(5, 0), "new_clones": None}}, []),
           ({"structure": {**structure(0, 2), "complexity": None}}, []),
           ({"structure": {**structure(0, 4), "complexity": structure(0, 0, complete=False)["complexity"]}}, [fix])]
    got = outcomes.summarize_signals([{**counts, "number": 1, "escapes": [], "follow_ups": follow_ups,
                                       "signals": {"added": 10, "deleted": 0, **signals}} for signals, follow_ups in prs])

    def cell(prs, fix=0, revert=0):
        def rate(n):
            return round(n / prs, 2) if prs else None
        return {"prs": prs, "fix": fix, "fix_rate": rate(fix), "revert": revert, "revert_rate": rate(revert),
                "escape": 0, "escape_rate": rate(0)}

    splits = got["splits"]
    assert splits["complexity_added"] == {"none": cell(1, fix=1), "1-9": cell(3, fix=1, revert=1),
                                          "10_or_more": cell(2, fix=1), "unknown": cell(6, fix=1)}, splits["complexity_added"]
    assert splits["new_clones"] == {"any": cell(5, fix=3, revert=1), "none": cell(2, fix=1), "unknown": cell(5)}, \
        splits["new_clones"]
    assert got["by_size"][">400"]["complexity_added"] == {"none": cell(0), "1-9": cell(1, fix=1), "10_or_more": cell(0),
                                                          "unknown": cell(0)}, got["by_size"][">400"]
    assert got["by_size"]["<50"]["new_clones"]["any"] == cell(4, fix=2, revert=1), got["by_size"]["<50"]


def the_prompt_names_every_signal_split(base):
    """run.sh's Signals section is the reader's only list of the splits: one it leaves out goes unread."""
    prompt = open(os.path.join(KIT, "review-mining", "run.sh")).read()
    section = prompt[prompt.index('Add a "Signals" section'):prompt.index('Add an "Escapes" section')]
    missing = [split for split in outcomes.SIGNAL_SPLITS if split not in section]
    assert not missing, f"the Signals section never names {missing}"

def signals_read_the_prs_settled_since_last_month(base):
    """A PR merged in the FOLLOW_UP_DAYS before SINCE was pending at last month's run: its signals count now, while the
    other summaries keep to PRs merged since SINCE."""
    real = (outcomes.fetch_window, outcomes.BASELINE_CACHE, outcomes.QUALITY_LOG, sys.argv)
    outcomes.QUALITY_LOG = os.path.join(base, "quality.jsonl")
    outcomes.BASELINE_CACHE = os.path.join(base, "baseline.json")
    open(outcomes.QUALITY_LOG, "w").write("".join(json.dumps(e) + "\n" for e in (
        {"kind": "pr", "ts": "2026-10-09T10:00:00+0000", "host": "github.com", "remote": "O/Shop", "name": 1,
         "repo": "shop", "branch": "b", "added": 10, "deleted": 0},
        {"kind": "escape", "ts": "2026-10-11T10:00:00+0000", "name": "bot", "repo": "shop", "branch": "b", "pr": 1,
         "verdict": "confirmed", "lens": "tests", "category": "P4.5"})))
    sessions = os.path.join(base, "sessions.json")
    open(sessions, "w").write("[]")
    counts = dict.fromkeys(("review_threads", "changes_requested", "red_pushes", "commits_after_first_review"), 0)
    pr = {**counts, "host": "github.com", "repo": "o/shop", "branch": "b", "opened": "2026-10-01T00:00:00Z",
          "files": [], "follow_ups": []}
    earlier = {**pr, "number": 1, "merged": "2026-10-10T10:00:00Z"}
    later = {**pr, "number": 2, "merged": "2026-10-20T10:00:00Z", "follow_ups": "pending"}
    windows = []

    def fake_fetch(start, end, now, errors):
        windows.append(start)
        return [] if start < outcomes.KIT_START else [dict(earlier), dict(later)]

    outcomes.fetch_window = fake_fetch
    try:
        for since, kit_start in (("2026-10-20", "2026-10-06"), ("2026-09-30", outcomes.KIT_START)):
            windows.clear()
            output = os.path.join(base, "out.json")
            sys.argv = ["outcomes.py", since, sessions, output]
            with contextlib.redirect_stdout(io.StringIO()):
                outcomes.main()
            result = json.load(open(output))
            assert windows[-1] == kit_start, (since, windows)
            if since == "2026-10-20":
                since_kit = result["summary"]["since_kit"]
                assert since_kit["merged_prs"] == 1 and [p["number"] for p in result["prs"]] == [2], result
                assert since_kit["follow_ups"]["settled_prs"] == 0, "the earlier PR stays out of the existing summaries"
                assert "signals" not in since_kit and result["signals"]["settled_prs"] == 1, result["signals"]
                assert result["signals"]["size"]["<50"] == {"prs": 1, "fix": 0, "fix_rate": 0.0, "revert": 0,
                                                            "revert_rate": 0.0, "escape": 1, "escape_rate": 1.0}, \
                    "its pr line and escape were joined"
                assert [p["number"] for p in result["earlier_prs"]] == [1], "its evidence is exported"
                assert result["window"][0] == "2026-10-20" and result["signals_window"][0] == "2026-10-06", result
    finally:
        outcomes.fetch_window, outcomes.BASELINE_CACHE, outcomes.QUALITY_LOG, sys.argv = real


RESULTS = []
for test in (pr_metrics_count_people_not_me_or_bots, follow_ups_need_a_shared_file_and_a_fix_title,
             escapes_join_to_their_branch_and_lens, sessions_join_by_repo_and_branch, a_checkout_named_unlike_its_repo,
             quality_log_names_the_repo_not_the_worktree, attention_per_pr_and_its_coverage,
             a_share_never_measured_is_not_zero, signals_join_by_host_repo_and_number, signal_cells,
             structure_cells, the_prompt_names_every_signal_split, signals_read_the_prs_settled_since_last_month):
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
