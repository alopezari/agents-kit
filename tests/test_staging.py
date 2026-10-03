#!/usr/bin/env python3
"""bin/staging: the steps before the merge, and the PASS/FAIL row each one gets, as review_stamp reads them."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
REPORTS = os.path.join(KIT, "bin", "reports")
STAGING = os.path.join(KIT, "bin", "staging")
sys.path.insert(0, os.path.join(KIT, "hooks"))
sys.dont_write_bytecode = True
import review_stamp  # noqa: E402

TODAY = datetime.date.today().isoformat()
GUIDE = """# Staging guide: cart

## Before the merge

### S1. Pay with a test card

- **Why:** checkout works.

### S2. Refund it

### S3. Put the environment back as it was

## After the merge

### P1. Watch the error rate
"""
RESULTS = []


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def new_repo(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "feature/cart")
    return repo


def paths(repo):
    guide, evidence = (sh(repo, REPORTS, "path", kind).stdout.strip() for kind in ("staging-guide", "evidence"))
    os.makedirs(evidence, exist_ok=True)
    return guide, evidence


def save(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def read(path):
    with open(path) as fh:
        return fh.read()


def steps(repo):
    out = sh(repo, STAGING, "steps", "--json")
    assert out.returncode == 0, out
    return json.loads(out.stdout)


def lists_the_steps_before_the_merge_with_their_latest_result_and_evidence(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE + "\n## Results (2026-09-30)\n\n| Step | Result | Evidence |\n|---|---|---|\n| S1 | FAIL | S1.txt |\n"
                        "\n## Results (2026-10-01)\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n"
                        "| S1 | PASS | user | S1.txt |\n"
                        "\n## Results after the deploy (2026-10-02)\n\n| Step | Result | Evidence |\n|---|---|---|\n| S2 | FAIL | x |\n")
    for name, text in (("S1.txt", "ok"), ("S1b.txt", "ok"), ("S2-receipt.png", "png"), ("S3.txt", ""), ("S10.txt", "x"),
                       ("S2.log", "x")):
        save(os.path.join(evidence, name), text)
    got = steps(repo)
    assert [s["id"] for s in got] == ["S1", "S2", "S3"], got
    assert got[0]["title"] == "Pay with a test card", got[0]
    assert (got[0]["result"], got[0]["by"]) == ("PASS", "user"), "the latest round, not ship's table or an earlier one"
    assert (got[1]["result"], got[1]["by"]) == ("", ""), got[1]
    assert got[0]["evidence"] == [os.path.join(evidence, n) for n in ("S1.txt", "S1b.txt")], got[0]["evidence"]
    assert got[1]["evidence"] == [os.path.join(evidence, "S2-receipt.png")], "S2.log isn't evidence of S2"
    assert got[2]["evidence"] == [], "an empty S3.txt is no evidence"


def prints_nothing_without_steps_before_the_merge(base):
    repo = new_repo(base)
    assert steps(repo) == [], "no guide"
    guide, _ = paths(repo)
    save(guide, "# Guide\n\n## Before the merge\n\nNo steps: no staging environment.\n\n## After the merge\n\n### P1. X\n")
    assert steps(repo) == [], "only after-merge steps"


def mark_writes_one_row_per_step_into_the_latest_round(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE)
    save(os.path.join(evidence, "S1.txt"), "paid")
    out = sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user")
    assert out.returncode == 0, out
    text = read(guide)
    assert text.startswith(GUIDE), "the guide above the results is untouched"
    assert f"## Results ({TODAY})\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n" in text, text
    assert f"| S1 | PASS | user | [S1.txt]({evidence}/S1.txt) |" in text, text
    assert sh(repo, STAGING, "mark", "S2", "FAIL", "--by", "agent", "--note", "refund 500 | twice").returncode == 0
    assert sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "user").returncode == 0
    rows = [line for line in read(guide).splitlines() if re.match(r"\| S\d", line)]
    assert rows == [f"| S1 | FAIL | user | [S1.txt]({evidence}/S1.txt) |",
                    "| S2 | FAIL | agent | refund 500 \\| twice; no saved evidence |"], rows
    assert read(guide).count("## Results") == 1, "marks go into one round"


def mark_keeps_a_hand_written_round_and_adds_the_by_column(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE + "\n## Results (2026-10-01)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
                        "| S2 | PASS | 200 on refund: [output](/e/S2.txt) |\n\nS3 was skipped: nothing to undo.\n")
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    text = read(guide)
    assert "| Step | Result | By | Evidence |" in text and "| S2 | PASS |  | 200 on refund: [output](/e/S2.txt) |" in text, text
    assert "| S1 | PASS | user | no saved evidence |" in text, text
    assert text.endswith("\nS3 was skipped: nothing to undo.\n"), "text after the table stays"
    assert f"## Results ({TODAY})" not in text, "the latest round is reused, not a new one started"


def refuses_what_it_cant_record_and_changes_nothing(base):
    repo = new_repo(base)
    guide, _ = paths(repo)
    out = sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user")
    assert out.returncode == 2 and "no staging guide" in out.stderr, ("no guide", out)
    save(guide, GUIDE)
    for argv, why in ((["S9", "PASS", "--by", "user"], "no step S9"), (["P1", "PASS", "--by", "user"], "no step P1"),
                      (["S1", "OK", "--by", "user"], "invalid choice"), (["S1", "PASS", "--by", "boss"], "invalid choice")):
        out = sh(repo, STAGING, "mark", *argv)
        assert out.returncode == 2 and why in out.stderr, (argv, out)
    assert read(guide) == GUIDE


def review_stamp_reads_what_mark_writes(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE)
    for name in ("S1.txt", "S2.txt", "S3.txt"):
        save(os.path.join(evidence, name), "done")
    for step in ("S1", "S2", "S3"):
        sh(repo, STAGING, "mark", step, "PASS", "--by", "user")
    assert review_stamp.staging_phase(guide, evidence, stamped=True) is None, read(guide)
    assert review_stamp.staging_phase(guide, evidence, stamped=False) is not None, "marks alone don't stand for the stamp"
    sh(repo, STAGING, "mark", "S2", "FAIL", "--by", "user")
    assert review_stamp.staging_phase(guide, evidence, stamped=True) == "staging: fix"
    os.remove(os.path.join(evidence, "S2.txt"))
    sh(repo, STAGING, "mark", "S2", "PASS", "--by", "user")
    assert review_stamp.staging_phase(guide, evidence, stamped=True) == "staging: no evidence"
    save(os.path.join(evidence, "S2.txt"), "refunded")
    sh(repo, STAGING, "mark", "S2", "PASS", "--by", "agent", "--note", "the refund shows")
    assert "was PASS by user; the refund shows; [S2.txt]" in read(guide), read(guide)
    assert review_stamp.staging_phase(guide, evidence, stamped=True) is None, "the file link still counts after the note"


def mark_leaves_ships_table_and_keeps_whose_verdict_it_replaced(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    ship = "\n## Results after the deploy (2026-10-02)\n\n| Step | Result | Evidence |\n|---|---|---|\n| P1 | PASS | x |\n"
    save(guide, GUIDE + "\n## Results (2026-10-01)\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n"
                        "| S1 | PASS | user | looked fine |\n" + ship)
    save(os.path.join(evidence, "S1.txt"), "500")
    assert sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "agent", "--note", "S1.txt shows a 500").returncode == 0
    text = read(guide)
    assert text.endswith(ship), "ship's table stays as it was, last"
    assert f"| S1 | FAIL | agent | was PASS by user; S1.txt shows a 500; [S1.txt]({evidence}/S1.txt) |" in text, text
    assert sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "agent").returncode == 0
    assert f"| S1 | FAIL | agent | [S1.txt]({evidence}/S1.txt) |" in read(guide), "the same writer again: nothing to keep"
    save(guide, GUIDE + ship)
    assert sh(repo, STAGING, "mark", "S2", "PASS", "--by", "user").returncode == 0
    text = read(guide)
    assert text.startswith(GUIDE + ship) and "| S2 | PASS | user |" in text.split(ship)[1], "a new round, after ship's"
    assert [s["result"] for s in steps(repo)] == ["", "PASS", ""], steps(repo)


def mark_refuses_a_branch_other_than_the_one_shown(base):
    repo = new_repo(base)
    guide, _ = paths(repo)
    save(guide, GUIDE)
    out = sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user", "--branch", "feature/other")
    assert out.returncode == 2 and "on feature/cart, not feature/other" in out.stderr, out
    assert read(guide) == GUIDE
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user", "--branch", "feature/cart").returncode == 0


def marks_at_the_same_time_all_land(base):
    repo = new_repo(base)
    guide, _ = paths(repo)
    many = GUIDE.replace("## After the merge", "".join(f"### S{n}. Step {n}\n\n" for n in range(4, 13)) + "## After the merge")
    save(guide, many)
    marks = [subprocess.Popen([STAGING, "mark", f"S{n}", "PASS", "--by", "agent"], cwd=repo,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True) for n in range(1, 13)]
    assert all(p.wait() == 0 for p in marks), [p.stderr.read() for p in marks]
    assert [s["result"] for s in steps(repo)] == ["PASS"] * 12, read(guide)
    assert read(guide).startswith(many)
    assert [f for f in os.listdir(os.path.dirname(guide)) if f.startswith(".") and "tmp" in f] == [], "no temp file left"


for test in (lists_the_steps_before_the_merge_with_their_latest_result_and_evidence,
             prints_nothing_without_steps_before_the_merge, mark_writes_one_row_per_step_into_the_latest_round,
             mark_keeps_a_hand_written_round_and_adds_the_by_column, refuses_what_it_cant_record_and_changes_nothing,
             review_stamp_reads_what_mark_writes, mark_leaves_ships_table_and_keeps_whose_verdict_it_replaced,
             mark_refuses_a_branch_other_than_the_one_shown, marks_at_the_same_time_all_land):
    base = tempfile.mkdtemp(prefix="agents-test-staging-")
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
