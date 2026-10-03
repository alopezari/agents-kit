#!/usr/bin/env python3
"""bin/staging: the steps before the merge, and the PASS/FAIL row each one gets, as review_stamp reads them."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import datetime
import fcntl
import importlib.machinery
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

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
    assert f"| S1 | PASS | user | [S1.txt](<{evidence}/S1.txt>) |" in text, text
    assert sh(repo, STAGING, "mark", "S2", "FAIL", "--by", "agent", "--note", "refund 500 | twice").returncode == 0
    assert sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "user").returncode == 0
    rows = [line for line in read(guide).splitlines() if re.match(r"\| S\d", line)]
    assert rows == [f"| S1 | FAIL | user | [S1.txt](<{evidence}/S1.txt>) |",
                    "| S2 | FAIL | agent | refund 500 \\| twice; no saved evidence |"], rows
    assert read(guide).count("## Results") == 1, "marks go into one round"
    assert sh(repo, STAGING, "mark", "S3", "PASS", "--by", "user").returncode == 0
    save(guide, read(guide).replace("| S1 | FAIL", "| S1 | FAIL").replace(f"| S1 | FAIL | user | [S1.txt](<{evidence}/S1.txt>) |\n", ""))
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    assert [l.split(" | ")[0] for l in read(guide).splitlines() if re.match(r"\| S\d", l)] == ["| S1", "| S2", "| S3"], \
        "a new row goes in the guide's step order"


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
    assert f"| S1 | FAIL | agent | was PASS by user; S1.txt shows a 500; [S1.txt](<{evidence}/S1.txt>) |" in text, text
    assert sh(repo, STAGING, "mark", "S2", "PASS", "--by", "agent", "--note", "was told to").returncode == 0
    assert sh(repo, STAGING, "mark", "S2", "PASS", "--by", "agent").returncode == 0
    assert "| S2 | PASS | agent | no saved evidence |" in read(guide), "a note isn't taken for the verdict it overrode"
    assert sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "agent").returncode == 0
    assert f"| S1 | FAIL | agent | was PASS by user; [S1.txt](<{evidence}/S1.txt>) |" in read(guide), \
        "the same writer again keeps the verdict it overrode"
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


def mark_changes_only_its_row_of_the_latest_rounds_last_table(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    older = "\n## Results (2026-09-30)\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n| S1 | PASS | user | x |\n"
    first = "\n## Results (2026-10-01)\n\n|Step|Result|By|Evidence|\n|-|-|-|-|\n|S1|FAIL|agent|S1 was 500|\n"
    second = "\nRe-run after the fix:\n\n| Step | Result | By | Evidence |\n|:--|:--|:--|:--|\n| S2  |  PASS | user |  ok  |\n"
    tail = "\n\n\nS3 waits for the deploy.  \n\n\n" + "## Results after the deploy (2026-10-02)\n\n| Step | Result | Evidence |\n|---|---|---|\n| P1 | FAIL | y |\n"
    save(guide, GUIDE + older + first + second + tail)
    save(os.path.join(evidence, "S1.txt"), "200")
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    row = f"| S1 | PASS | user | was FAIL by agent; [S1.txt](<{evidence}/S1.txt>) |"
    assert read(guide) == GUIDE + older + first.replace("|S1|FAIL|agent|S1 was 500|", row) + second + tail, read(guide)
    latest = read(guide).split("## Results (2026-10-01)")[1].split("## Results after")[0].splitlines()
    assert [(r[1], r[3]) for r in review_stamp.result_rows(latest, evidence) if r[0] == "S1"][-1] == ("PASS", True), \
        "review_stamp reads the row mark wrote as S1's"
    assert [(x["result"], x["by"]) for x in steps(repo)] == [("PASS", "user"), ("PASS", "user"), ("", "")], steps(repo)


def mark_reads_bold_headers_and_keeps_columns_it_doesnt_know(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE + "\n## Results (2026-10-01)\n\n| **Step** | **Result** | **Evidence** | Notes |\n|---|---|---|---|\n"
                        "| S2 | **FAIL** | refund 500 | ask ops |\n| S3 | PASS | none needed | keep me |\n"
                        "| S3 | pending | rerun | later |\n")
    assert [(x["result"], x["by"]) for x in steps(repo)][1:] == [("FAIL", ""), ("PASS", "")], steps(repo)  # "pending" is no result
    note = "refund shows\nin the admin | twice"
    assert sh(repo, STAGING, "mark", "S2", "PASS", "--by", "agent", "--note", note).returncode == 0
    lines = read(guide).split("## Results (2026-10-01)\n\n")[1].splitlines()
    assert lines == ["| **Step** | **Result** | By | **Evidence** | Notes |", "|---|---| --- |---|---|",
                     "| S2 | PASS | agent | was FAIL; refund shows in the admin \\| twice; no saved evidence | ask ops |",
                     "| S3 | PASS |  | none needed | keep me |", "| S3 | pending |  | rerun | later |"], lines
    assert read(guide).count("| Step") + read(guide).count("| **Step") == 1, "no second table"


def mark_links_every_evidence_file_of_the_step(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE)
    for name in ("S1.txt", "S1b.txt", "S1-receipt.png", "S10-x.png", "S1.png"):
        save(os.path.join(evidence, name), "x")
    os.makedirs(os.path.join(evidence, "S1c.txt"))
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    links = ", ".join(f"[{n}](<{evidence}/{n}>)" for n in ("S1-receipt.png", "S1.txt", "S1b.txt"))
    assert f"| S1 | PASS | user | {links} |" in read(guide), read(guide)


def an_interrupted_mark_leaves_the_earlier_guide(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE)
    os.chmod(guide, 0o640)
    loader = importlib.machinery.SourceFileLoader("staging_tool", STAGING)
    tool = types.ModuleType(loader.name)
    tool.__file__ = STAGING
    loader.exec_module(tool)
    replace = tool.os.replace
    tool.os.replace = lambda *_: (_ for _ in ()).throw(OSError("disk full"))
    try:
        tool.mark_locked(guide, evidence, "S1", "PASS", "agent", "")
        raise AssertionError("the failed rename should surface")
    except OSError:
        pass
    finally:
        tool.os.replace = replace
    left = [n for n in os.listdir(os.path.dirname(guide)) if n.endswith(".tmp")]
    assert read(guide) == GUIDE and left == [], left
    assert tool.mark_locked(guide, evidence, "S1", "PASS", "agent", "") == 0
    assert oct(os.stat(guide).st_mode & 0o777) == oct(0o640), "the guide keeps its mode"


def a_mark_waits_for_the_lock(base):
    repo = new_repo(base)
    guide, _ = paths(repo)
    save(guide, GUIDE)
    with open(os.path.join(os.path.dirname(guide), "." + os.path.basename(guide) + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = subprocess.Popen([STAGING, "mark", "S1", "PASS", "--by", "agent"], cwd=repo, stdout=subprocess.DEVNULL)
        try:
            time.sleep(0.5)
            assert waiting.poll() is None and read(guide) == GUIDE, "it writes only once the lock is free"
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    assert waiting.wait(timeout=10) == 0 and "| S1 | PASS | agent |" in read(guide)
    with open(os.path.join(os.path.dirname(guide), "." + os.path.basename(guide) + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        waiting = subprocess.Popen([STAGING, "mark", "S2", "PASS", "--by", "user", "--branch", "feature/cart"], cwd=repo,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(0.5)
            sh(repo, "git", "switch", "-q", "-c", "feature/other")
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
    assert waiting.wait(timeout=10) == 2 and "not feature/cart" in waiting.stderr.read(), "the checkout moved while it waited"
    assert "| S2 |" not in read(guide)


def marks_alone_dont_pass_the_staging_gate(base):
    repo = new_repo(base)
    sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "start")
    guide, evidence = paths(repo)
    save(guide, GUIDE)
    for step in ("S1", "S2", "S3"):
        save(os.path.join(evidence, step + ".txt"), "done")
        assert sh(repo, STAGING, "mark", step, "PASS", "--by", "user").returncode == 0
    gate = sh(repo, sys.executable, os.path.join(KIT, "hooks", "review_stamp.py"), "staging")
    assert gate.returncode != 0 and "never stamped" in gate.stdout, gate
    sh(repo, "git", "switch", "-q", "--detach")
    out = sh(repo, STAGING, "mark", "S1", "FAIL", "--by", "user", "--branch", "feature/cart")
    assert out.returncode == 2 and "on a detached HEAD, not feature/cart" in out.stderr, out


def a_step_without_a_title_and_a_branch_with_a_pipe(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "feature|cart")
    guide, evidence = paths(repo)
    save(guide, "# Guide\n\n## Before the merge\n\n### S1\n\n- **Why:** checkout works.\n\n## After the merge\n")
    save(os.path.join(evidence, "S1.txt"), "paid")
    assert steps(repo)[0]["title"] == "", "the next line isn't the title"
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    assert evidence.replace("|", "\\|") in read(guide), read(guide)
    assert [r[1:4:2] for r in review_stamp.result_rows(read(guide).splitlines(), evidence)] == [("PASS", True)], read(guide)


def mark_keeps_one_row_per_step_and_says_why_it_cant_write(base):
    repo = new_repo(base)
    guide, evidence = paths(repo)
    save(guide, GUIDE + "\n## Results (2026-10-01)\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n| S1 | FAIL | agent | x |\n"
                        "\nRe-run:\n\n| Step | Result | By | Evidence |\n|---|---|---|---|\n| S2 | PASS | user | y |\n| **S3** | PASS | user | z |\n")
    assert sh(repo, STAGING, "mark", "S1", "PASS", "--by", "user").returncode == 0
    assert [l for l in read(guide).splitlines() if l.startswith("| S1")] == ["| S1 | PASS | user | was FAIL by agent; no saved evidence |"]
    assert read(guide).index("| S1 | PASS") < read(guide).index("Re-run:"), "replaced where it was, not added to the last table"
    assert steps(repo)[2]["result"] == "", "**S3** is no step to review_stamp either"
    lock = os.path.join(os.path.dirname(guide), "." + os.path.basename(guide) + ".lock")
    os.remove(lock)
    os.makedirs(lock)
    out = sh(repo, STAGING, "mark", "S2", "FAIL", "--by", "user")
    assert out.returncode == 1 and out.stderr.startswith("staging: couldn't write the staging guide") and "Traceback" not in out.stderr, out


for test in (lists_the_steps_before_the_merge_with_their_latest_result_and_evidence,
             prints_nothing_without_steps_before_the_merge, mark_writes_one_row_per_step_into_the_latest_round,
             mark_keeps_a_hand_written_round_and_adds_the_by_column, refuses_what_it_cant_record_and_changes_nothing,
             review_stamp_reads_what_mark_writes, mark_leaves_ships_table_and_keeps_whose_verdict_it_replaced,
             mark_refuses_a_branch_other_than_the_one_shown, marks_at_the_same_time_all_land,
             mark_changes_only_its_row_of_the_latest_rounds_last_table, mark_reads_bold_headers_and_keeps_columns_it_doesnt_know,
             mark_links_every_evidence_file_of_the_step, an_interrupted_mark_leaves_the_earlier_guide, a_mark_waits_for_the_lock,
             marks_alone_dont_pass_the_staging_gate, a_step_without_a_title_and_a_branch_with_a_pipe,
             mark_keeps_one_row_per_step_and_says_why_it_cant_write):
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
