#!/usr/bin/env python3
"""bin/pr-comments: the pull request's comments the follow-pr report doesn't list yet, with a fake gh."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
PR_COMMENTS = os.path.join(KIT, "bin", "pr-comments")
REPORTS = os.path.join(KIT, "bin", "reports")
# Answers `gh pr view` and the three comment lists from answers.json; a list returns only its first item unless
# --paginate is passed, as a first page would. "error" makes every call fail as gh does offline.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
answers = json.load(open(os.path.join(os.environ["FAKE_GH_DIR"], "answers.json")))
args = sys.argv[1:]
if answers.get("error"):
    print("error connecting to api.github.com", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["pr", "view"]:
    print(json.dumps(answers["pr"]))
    sys.exit(0)
endpoint = next(a for a in args if a.startswith("repos/"))
kind = "inline" if "/pulls/" in endpoint and endpoint.endswith("/comments") else \
       "review" if endpoint.endswith("/reviews") else "conversation"
items = answers[kind] if "--paginate" in args else answers[kind][:1]
print("\n".join(json.dumps(item) for item in items))
'''
RESULTS = []


def user(login, bot=False):
    return {"login": login, "type": "Bot" if bot else "User"}


ANSWERS = {
    "pr": {"number": 7, "author": {"login": "me"}},
    "inline": [
        {"id": 101, "user": user("ana"), "path": "cart.py", "line": 12, "body": "This rounds twice.",
         "html_url": "https://github.com/o/r/pull/7#discussion_r101"},
        {"id": 102, "user": user("me"), "path": "cart.py", "line": 12, "body": "Fixed in abc.",
         "html_url": "https://github.com/o/r/pull/7#discussion_r102"},
        {"id": 103, "user": user("ana"), "path": "tax.py", "line": None, "body": "Already handled one.",
         "html_url": "https://github.com/o/r/pull/7#discussion_r103"},
    ],
    "conversation": [
        {"id": 201, "user": user("review-bot[bot]", bot=True), "body": "Two findings:\n- a\n- b",
         "html_url": "https://github.com/o/r/pull/7#issuecomment-201"},
    ],
    "review": [
        {"id": 301, "user": user("ana"), "state": "APPROVED", "body": "", "html_url": "https://github.com/o/r/pull/7#pullrequestreview-301"},
        {"id": 302, "user": user("bo"), "state": "CHANGES_REQUESTED", "body": "Please add a test.",
         "html_url": "https://github.com/o/r/pull/7#pullrequestreview-302"},
    ],
}


def run(base, answers, in_repo=True):
    repo = os.path.join(base, "shop")
    os.makedirs(os.path.join(base, "bin"))
    os.makedirs(repo)
    if not in_repo:
        env = {**os.environ, "PATH": os.path.join(base, "bin") + ":" + os.environ["PATH"], "FAKE_GH_DIR": base}
        open(os.path.join(base, "bin", "gh"), "w").write(FAKE_GH)
        os.chmod(os.path.join(base, "bin", "gh"), 0o755)
        json.dump(answers, open(os.path.join(base, "answers.json"), "w"))
        return subprocess.run([PR_COMMENTS], cwd=repo, capture_output=True, text=True, env=env, timeout=60)
    subprocess.run(["git", "init", "-q", "-b", "feature/cart"], cwd=repo, check=True)
    open(os.path.join(base, "bin", "gh"), "w").write(FAKE_GH)
    os.chmod(os.path.join(base, "bin", "gh"), 0o755)
    json.dump(answers, open(os.path.join(base, "answers.json"), "w"))
    report = subprocess.run([REPORTS, "path", "follow-pr"], cwd=repo, capture_output=True, text=True).stdout.strip()
    with open(report, "w") as fh:
        fh.write("## Run 2026-10-01 10:00\nCI:      suite: pass\n"
                 "103 https://github.com/o/r/pull/7#discussion_r103 — ana — Rejected — handled at tax.py:4 — reply posted\n"
                 "Status:  waiting on ana\n")
    env = {**os.environ, "PATH": os.path.join(base, "bin") + ":" + os.environ["PATH"], "FAKE_GH_DIR": base}
    return subprocess.run([PR_COMMENTS], cwd=repo, capture_output=True, text=True, env=env, timeout=60)


def lists_only_new_comments_from_others(base):
    out = run(base, ANSWERS)
    assert out.returncode == 0, out
    assert out.stdout == """101 inline ana https://github.com/o/r/pull/7#discussion_r101
  cart.py:12
  This rounds twice.

201 conversation review-bot[bot] (bot) https://github.com/o/r/pull/7#issuecomment-201
  Two findings:
  - a
  - b

302 review bo CHANGES_REQUESTED https://github.com/o/r/pull/7#pullrequestreview-302
  Please add a test.

3 new comments on #7 (1 already in the follow-pr report, 1 by the author, 1 review without text).
""", out.stdout


def none_new_says_so(base):
    out = run(base, {**ANSWERS, "inline": ANSWERS["inline"][1:], "conversation": [], "review": ANSWERS["review"][:1]})
    assert out.returncode == 0 and out.stdout == \
        "No new comments on #7 (1 already in the follow-pr report, 1 by the author, 1 review without text).\n", out.stdout


def github_unreadable_is_not_no_comments(base):
    out = run(base, {"error": True})
    assert out.returncode == 4 and "couldn't read" in out.stderr and out.stdout == "", out


def a_deleted_account_and_an_empty_body_still_print(base):
    ghost = {"id": 202, "user": None, "body": None, "html_url": "https://github.com/o/r/pull/7#issuecomment-202"}
    out = run(base, {**ANSWERS, "inline": [], "conversation": [ghost], "review": []})
    assert out.returncode == 0 and out.stdout.startswith(
        "202 conversation ghost https://github.com/o/r/pull/7#issuecomment-202\n  (no text)\n"), out


def no_follow_pr_report_path_is_not_a_first_run(base):
    out = run(base, ANSWERS, in_repo=False)
    assert out.returncode == 4 and "follow-pr report" in out.stderr and out.stdout == "", out


for test in (lists_only_new_comments_from_others, none_new_says_so, github_unreadable_is_not_no_comments,
             a_deleted_account_and_an_empty_body_still_print, no_follow_pr_report_path_is_not_a_first_run):
    base = tempfile.mkdtemp(prefix="agents-test-pr-comments-")
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
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
