#!/usr/bin/env python3
"""monitors/health.sh: a failing suite is reported by the tests that failed, with their detail, and a quiet week
writes nothing to stderr."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
HEALTH = os.path.join(KIT, "monitors", "health.sh")
RESULTS = []


def script(path, body):
    open(path, "w").write(f"#!/bin/sh\n{body}\n")
    os.chmod(path, 0o755)


def health(base, suite_output, suite_status):
    """health.sh under a HOME of its own, whose kit has no repo overlay, no log and a suite that prints suite_output:
    (exit status, report, stderr)."""
    home, fakes, tmp = os.path.join(base, "home"), os.path.join(base, "fakes"), os.path.join(base, "tmp")
    kit = os.path.join(home, ".agents")
    for folder in (os.path.join(kit, "tests"), os.path.join(kit, "hooks"), os.path.join(home, ".claude"),
                   os.path.join(home, ".codex"), fakes, tmp):
        os.makedirs(folder)
    open(os.path.join(base, "suite.txt"), "w").write(suite_output)
    script(os.path.join(kit, "tests", "run.sh"), f'cat "{base}/suite.txt"\nexit {suite_status}')
    open(os.path.join(kit, "hooks", "stop_checks.py"), "w").write("")  # leftover-overrides: none
    for program, body in (("osascript", "exit 0"), ("launchctl", "exit 0"), ("claude", "echo 2.1.0"), ("codex", "echo 0.50.0")):
        script(os.path.join(fakes, program), body)
    open(os.path.join(home, ".claude", "settings.json"), "w").write("{}\n")
    open(os.path.join(home, ".codex", "config.toml"), "w").write("[features]\nfast_mode = false\n")
    env = {**os.environ, "HOME": home, "PATH": f"{fakes}:{os.environ['PATH']}", "TMPDIR": tmp}
    done = subprocess.run(["bash", HEALTH, "--quiet"], capture_output=True, text=True, env=env, timeout=120)
    reports = glob.glob(os.path.join(kit, "research", "health", "*.md"))
    assert len(reports) == 1, (reports, done.stdout, done.stderr)
    return done.returncode, open(reports[0]).read(), done.stderr


def section(report, title):
    """The lines under a report's `## <title>`, without the code fence."""
    body = report.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]
    return [line for line in body.splitlines() if line and line != "```"]


def the_failing_tests_are_named_not_the_summary_line(base):
    suite = ("\n== hooks\nok   passes\nFAIL first_failure\n     AssertionError: x\nok   also_passes\nFAIL second_failure\n"
             "\n== install on a new machine\nok   installs\nFAIL third failure, a check's description\n"
             "\n== real hook log untouched\nok   no test logged\n\nFAILURES ABOVE\n")
    status, report, stderr = health(base, suite, 1)
    assert status == 1, report
    assert section(report, "Needs attention") == [
        "- Regression suite failed: FAIL first_failure;FAIL second_failure;FAIL third failure, a check's description;"], report
    assert stderr == "", stderr


def warnings_dont_take_the_failing_tests_places(base):
    warnings = "".join(f"  warn stale link {n}\n" for n in range(1, 7))
    failures = "".join(f"FAIL failure_{n}\n" for n in range(1, 7))
    suite = (f"\n== install doctor\n{warnings}FAIL doctor exited 1 without naming a failing test\n"
             f"\n== hooks\n{failures}\nFAILURES ABOVE\n")
    status, report, stderr = health(base, suite, 1)
    assert status == 1, report
    assert section(report, "Needs attention") == [
        "- Regression suite failed: FAIL doctor exited 1 without naming a failing test;"
        + "".join(f"FAIL failure_{n};" for n in range(1, 5)) + "".join(f"  warn stale link {n};" for n in range(1, 6))], report
    assert stderr == "", stderr


def a_failing_block_keeps_its_traceback_and_detail(base):
    detail = "".join(f"detail {n}\n" for n in range(1, 61))
    suite = ("\n== hooks\nok   passes_one\nTraceback (most recent call last):\n  File \"test_hooks.py\", line 10, in <module>\n"
             "ValueError: broken fixture\nFAIL a_failing_test\nexpected 2 lines, got 3\nok   passes_two\n"
             f"\n== install on a new machine\nFAIL a_long_failure\n{detail}FAIL a_late_failure\nits detail\n"
             "\n== flow phase\nok   phase_one\n2/2 passed\n\nFAILURES ABOVE\n")
    status, report, stderr = health(base, suite, 1)
    assert status == 1, report
    kept = section(report, "Regression suite")
    assert kept == ["== hooks", "Traceback (most recent call last):", '  File "test_hooks.py", line 10, in <module>',
                    "ValueError: broken fixture", "FAIL a_failing_test", "expected 2 lines, got 3",
                    "== install on a new machine", "FAIL a_long_failure", *[f"detail {n}" for n in range(1, 40)],
                    "FAIL a_late_failure",
                    "== flow phase", "ok   phase_one", "2/2 passed", "FAILURES ABOVE"], kept
    assert stderr == "", stderr


def a_runner_that_names_no_failure_gives_its_status_and_last_lines(base):
    suite = "".join(f"setup line {n}\n" for n in range(1, 25)) + "run.sh: no temp directory\n"
    status, report, stderr = health(base, suite, 2)
    assert status == 1, report
    assert section(report, "Needs attention") == [
        "- Regression suite failed: exited 2 without naming a failing test; its last line: run.sh: no temp directory;"], report
    assert section(report, "Regression suite") == [*(f"setup line {n}" for n in range(6, 25)), "run.sh: no temp directory"], report
    assert stderr == "", stderr


def a_healthy_week_writes_nothing_to_stderr(base):
    suite = "\n== hooks\nok   passes\n3/3 passed\nsome chatter\n\n== real hook log untouched\nok   no test logged\n\nALL PASSED\n"
    status, report, stderr = health(base, suite, 0)
    assert status == 0, report
    assert section(report, "Needs attention") == ["Nothing."], report
    assert section(report, "Regression suite") == ["== hooks", "ok   passes", "3/3 passed", "== real hook log untouched",
                                                   "ok   no test logged", "ALL PASSED"], report
    assert stderr == "", stderr


for test in kit_home.selected((the_failing_tests_are_named_not_the_summary_line, warnings_dont_take_the_failing_tests_places,
                               a_failing_block_keeps_its_traceback_and_detail,
                               a_runner_that_names_no_failure_gives_its_status_and_last_lines,
                               a_healthy_week_writes_nothing_to_stderr)):
    base = tempfile.mkdtemp(prefix="agents-test-health-")
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
