#!/usr/bin/env python3
"""bin/pr-validation: the pull request's validation section, from the branch's reports."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
# A HOME of our own: pr-validation reads the user's phase switches, and the real ones would change its output.
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-pr-validation-home-")
os.makedirs(os.path.expanduser("~/.agents"))
for entry in set(os.listdir(KIT)) - {"approvals", "logs"}:
    os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
REPORTS = os.path.join(KIT, "bin", "reports")
PR_VALIDATION = os.path.join(KIT, "bin", "pr-validation")
HOME = os.path.expanduser("~")
RESULTS = []


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def new_repo(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    sh(repo, "git", "init", "-q", "-b", "feature/cart")
    return repo


def write(repo, kind, text):
    with open(sh(repo, REPORTS, "path", kind).stdout.strip(), "w") as fh:
        fh.write(text)


def builds_the_section_from_every_report(base):
    repo = new_repo(base)
    evidence = sh(repo, REPORTS, "path", "evidence").stdout.strip()
    write(repo, "verify", "# Verify: PASS\n\n2026-10-01 · verify in shop\n\nran: tests/test_cart.py\n"
                          "ran: tests/test_cart.py with Python 3.14.7\nran: phpunit --group cart\n")
    write(repo, "review", "Self-review (risk: standard; lenses: correctness, tests; cross-model: codex)\n\n"
                          "Spec: table here\n"
                          "Fixed:    a coupon applied twice on refresh (Codex) — cart.py:40 — test: test_coupon_once.\n"
                          "Fixed:    the total ignored tax — cart.py:52.\n"
                          "Rejected: a race on checkout — handled at cart.py:80.\n"
                          "Open: none.\n")
    write(repo, "validation", "Validation — cart\n\n"
                              "| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                              f"| A1 | Coupon applied once, \\| even on refresh | + | PASS | [output]({evidence}/E1.txt) |\n"
                              f"| A2 | A tampered total is rejected, see [the log]({evidence}/E2.txt) and {HOME}/notes/e2.txt | − | **FAIL** | E2.txt |\n"
                              "\n| # | Check | Result | Evidence |\n|---|---|---|---|\n"
                              "| B1 | Admin list loads | NOT RUN | |\n\n"
                              "Not run: B1 needs the payment sandbox.\n")
    write(repo, "staging-guide", "# Staging guide\n\n## Before the merge\n\n### S1 Pay\n\n### S2 Refund\n\n"
                                 "## Results (2026-09-30)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
                                 "| S1 | FAIL | s1.txt |\n| S2 | PASS | s2.txt |\n\n"
                                 "## Results (2026-10-01)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
                                 "| S1 | PASS | s1.txt |\n| S2 | PASS | s2.txt |\n")
    out = sh(repo, PR_VALIDATION)
    assert out.returncode == 0, out
    expected = """## Validation

- Verify: PASS, 2 checks: tests/test_cart.py, phpunit --group cart (also under Python 3.14.7).
- Self-review (risk: standard; lenses: correctness, tests; cross-model: codex): 2 fixed, 1 rejected, 0 open.
  - Fixed: a coupon applied twice on refresh (Codex).
  - Fixed: the total ignored tax.
  - Rejected: a race on checkout.
- Validation, 3 checks: 1 PASS, 1 FAIL, 1 NOT RUN.
  - A1 PASS: Coupon applied once, | even on refresh
  - A2 FAIL: A tampered total is rejected, see the log and ~/notes/e2.txt
  - B1 NOT RUN: Admin list loads
  - Not run: B1 needs the payment sandbox.
- Staging (results of 2026-10-01): S1 PASS, S2 PASS.
"""
    assert out.stdout == expected, out.stdout


def counts_open_findings_and_skips_absent_reports(base):
    repo = new_repo(base)
    write(repo, "verify", "# Verify: FAIL\n\nran: tests/test_cart.py\n")
    write(repo, "review", "Self-review (risk: low; lenses: correctness)\n"
                          "Open:     the refund total may round twice — cart.py:90 — a test with 0.005 would settle it.\n")
    out = sh(repo, PR_VALIDATION)
    assert out.returncode == 0, out
    assert out.stdout == ("## Validation\n\n- Verify: FAIL, 1 check: tests/test_cart.py.\n"
                          "- Self-review (risk: low; lenses: correctness): 0 fixed, 0 rejected, 1 open.\n"
                          "  - Open: the refund total may round twice.\n"), out.stdout


def keeps_qualified_verdicts_and_bounded_sections(base):
    repo = new_repo(base)
    write(repo, "verify", "# Verify: PASS, but nothing was checked\n\nran: nothing to check: no changed files\n")
    write(repo, "review", "Self-review (risk: low; lenses: correctness)\nFixed:\nRejected: none.\n")
    write(repo, "validation", f"| # | Check | Result | Evidence |\n|---|---|---|---|\n"
                              f"| A1 | Saved to [the log]({HOME}/work(copy)/acme/log.txt) and [the trace][t] | PASS | a1.txt |\n"
                              "\nNot run:\n## Details\n")
    write(repo, "staging-guide", "# Staging guide\n\n## Before the merge\n\n### S1 Pay\n\n"
                                 "## Results (2026-09-30)\n\n| Step | Result | Evidence |\n|---|---|---|\n| S1 | PASS | s1.txt |\n\n"
                                 "## Results after the deploy (2026-10-02)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
                                 "| P1 | FAIL | p1.txt |\n")
    out = sh(repo, PR_VALIDATION)
    assert out.stdout == ("## Validation\n\n- Verify: PASS, but nothing was checked.\n"
                          "- Self-review (risk: low; lenses: correctness): 0 fixed, 0 rejected, 0 open.\n"
                          "- Validation, 1 check: 1 PASS.\n  - A1 PASS: Saved to the log and the trace\n"
                          "- Staging (results of 2026-09-30): S1 PASS.\n"), out.stdout
    with open(sh(repo, REPORTS, "path", "staging-guide").stdout.strip(), "a") as fh:
        fh.write("\n## Results (2026-10-01)")
    assert sh(repo, PR_VALIDATION).stdout.endswith("- Staging (results of 2026-10-01): no step results.\n"), \
        "a latest round at the end of the file, still empty, isn't the earlier PASS"


def a_guide_with_only_steps_after_the_merge_claims_no_pending_staging(base):
    repo = new_repo(base)
    write(repo, "verify", "# Verify: PASS\n\nran: tests/test_cart.py\n")
    write(repo, "review", "Self-review (risk: low; lenses: correctness)\nFixed:\nRejected: none.\n")
    write(repo, "staging-guide", "# Checks after the merge\n\n## Before the merge\n\nNothing here.\n\n"
                                 "## After the merge\n\n### P1. Load the job\n\n### P2. One full run\n")
    out = sh(repo, PR_VALIDATION)
    assert out.stdout.endswith("- Staging: none before the merge; after it: P1, P2.\n"), out.stdout


def a_missing_verify_or_review_fails_naming_it(base):
    repo = new_repo(base)
    write(repo, "verify", "# Verify: PASS\n\nran: tests/test_cart.py\n")
    out = sh(repo, PR_VALIDATION)
    review = sh(repo, REPORTS, "path", "review").stdout.strip()
    assert out.returncode == 1 and review in out.stderr and out.stdout == "", ("no review report", out)


def names_the_phases_the_user_switched_off(base):
    try:
        repo = new_repo(base)
        sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")

        def switch(line):
            done = subprocess.run(["python3", os.path.join(KIT, "hooks", "prompt_approvals.py")], cwd=repo,
                                  input=json.dumps({"prompt": line, "session_id": "pr-validation", "cwd": repo}),
                                  capture_output=True, text=True)
            assert "Switched " in done.stdout, done.stdout + done.stderr

        def section():
            return subprocess.run([PR_VALIDATION], cwd=repo, capture_output=True, text=True)

        write(repo, "verify", "# Verify: PASS\n\nran: tests/test_cart.py\n")
        assert section().returncode == 1, "the self-review report is missing and its phase is on"
        switch("phase off self-review")
        switch("phase off validate global")
        switch("phase off second-model repo")
        out = section()
        assert out.returncode == 0, out.stderr
        for line in ("- Verify: ", "- Self-review: skipped by the user (branch).", "- Validate: skipped by the user (global).",
                     "- Staging: skipped by the user (validate off).", "- Second-model: skipped by the user (repo)."):
            assert out.stdout.count(line) == 1, (line, out.stdout)
        write(repo, "validation", "| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n| A1 | old | + | PASS | a |\n")
        os.remove(sh(repo, REPORTS, "path", "verify").stdout.strip())
        assert section().returncode == 1, "verify's report is missing and its phase is on"
        switch("phase off verify")
        out = section()
        assert out.returncode == 0 and "- Verify: skipped by the user (branch)." in out.stdout, out.stdout + out.stderr
        assert "A1" not in out.stdout and "1 PASS" not in out.stdout, f"a report from before the switch isn't shown: {out.stdout}"
        switch("phase off claude-review global")
        assert "Claude-review" not in section().stdout, "Codex reviewed this Claude Code change: nothing was skipped"
        write(repo, "review", "Fixed:     x — a.py:1\nNot run:   cross-model, codex-review switched off by the user — /tmp/x\n")
        switch("phase on self-review")
        out = section().stdout
        assert "  - Not run: cross-model, codex-review switched off by the user\n" in out and "/tmp/x" not in out, out
        switch("phase on validate global")
        assert "Validate: skipped" not in section().stdout, "switched back on, the phase isn't skipped"

    finally:  # the other tests share this HOME
        shutil.rmtree(os.path.join(os.path.expanduser("~/.agents"), "approvals"), ignore_errors=True)

for test in (names_the_phases_the_user_switched_off, builds_the_section_from_every_report, counts_open_findings_and_skips_absent_reports,
             keeps_qualified_verdicts_and_bounded_sections,
             a_guide_with_only_steps_after_the_merge_claims_no_pending_staging,
             a_missing_verify_or_review_fails_naming_it):
    base = tempfile.mkdtemp(prefix="agents-test-pr-validation-")
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
shutil.rmtree(os.environ["HOME"], ignore_errors=True)
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
