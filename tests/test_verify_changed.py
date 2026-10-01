#!/usr/bin/env python3
"""repos/_shared/verify_changed.py: linters configured for --cwd only judge files inside it, a linter whose
config excludes every changed file isn't a failure, and the red check's copy keeps export-ignored test files.

Runs against throwaway repos with fake phpcs, phpstan and phpunit.
"""
import os
import shutil
import subprocess
import sys
import tempfile

SCRIPT = os.path.expanduser("~/.agents/repos/_shared/verify_changed.py")
FAKE_PHPCS = """#!/usr/bin/env python3
import json, sys
files = [a for a in sys.argv[1:] if not a.startswith("-")]
print(json.dumps({"files": {f: {"messages": [{"line": 1, "type": "ERROR", "source": "Fake.Rule", "message": "flagged"}]}
                            for f in files}}))
"""

# PHPStan 1.x when its config excludes every path it is given: exit 0, nothing on stdout.
FAKE_PHPSTAN_NOTHING_IN_SCOPE = """#!/bin/sh
echo ' ! [NOTE] No files found to analyse.' >&2
"""
# A test that needs a helper from tests/ (like a base TestCase) and fails while src/Price.php lacks the fix.
FAKE_PHPUNIT = """#!/usr/bin/env python3
import os, sys
junit = sys.argv[sys.argv.index("--log-junit") + 1]
if not os.path.exists("tests/Helper.php"):
    sys.exit("Class Helper not found")
fixed = "fixed" in open("src/Price.php").read()
failure = "" if fixed else "<failure>rounds wrong</failure>"
open(junit, "w").write(f'<testsuites><testcase class="PriceTest" name="test_rounds">{failure}</testcase></testsuites>')
"""


def executable(path, text):
    open(path, "w").write(text)
    os.chmod(path, 0o755)
    return path


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def run(base, cwd_arg):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "plugin", "src"))
    os.makedirs(os.path.join(repo, "ci"))
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    git(repo, "checkout", "-q", "-b", "feature")
    open(os.path.join(repo, "plugin", "src", "Builder.php"), "w").write("<?php\n")
    open(os.path.join(repo, "ci", "fixture.php"), "w").write("<?php\n")
    phpcs = os.path.join(base, "phpcs")
    open(phpcs, "w").write(FAKE_PHPCS)
    os.chmod(phpcs, 0o755)
    return subprocess.run(["python3", SCRIPT, "--cwd", cwd_arg, "--phpcs", phpcs], cwd=repo,
                          capture_output=True, text=True).stdout


def files_outside_cwd_are_not_linted(base):
    out = run(base, "plugin")
    assert "plugin/src/Builder.php:1 [phpcs" in out, out
    assert "ci/fixture.php" not in out, "a CI fixture was judged by the plugin's phpcs rules:\n" + out


def repo_root_cwd_lints_everything(base):
    out = run(base, ".")
    assert "plugin/src/Builder.php:1 [phpcs" in out and "ci/fixture.php:1 [phpcs" in out, out


def phpstan_with_nothing_in_scope_is_not_a_failure(base):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "tests"))
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    open(os.path.join(repo, "tests", "PriceTest.php"), "w").write("<?php\n")
    phpstan = executable(os.path.join(base, "phpstan"), FAKE_PHPSTAN_NOTHING_IN_SCOPE)
    proc = subprocess.run(["python3", SCRIPT, "--phpstan", phpstan], cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0 and "did not run" not in proc.stdout, proc.stdout
    assert "phpstan: none of the 1 changed files is in its configured paths" in proc.stdout, proc.stdout


def red_check_keeps_export_ignored_tests(base):
    repo = os.path.join(base, "repo")
    os.makedirs(os.path.join(repo, "src"))
    os.makedirs(os.path.join(repo, "tests"))
    open(os.path.join(repo, ".gitattributes"), "w").write("/tests export-ignore\n")
    open(os.path.join(repo, "src", "Price.php"), "w").write("<?php\n")
    open(os.path.join(repo, "tests", "Helper.php"), "w").write("<?php\n")
    git(repo, "init", "-q", "-b", "trunk")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    git(repo, "checkout", "-q", "-b", "feature")
    open(os.path.join(repo, "src", "Price.php"), "w").write("<?php // fixed\n")
    open(os.path.join(repo, "tests", "PriceTest.php"), "w").write("<?php\n")
    phpunit = executable(os.path.join(base, "phpunit"), FAKE_PHPUNIT)
    proc = subprocess.run(["python3", SCRIPT, "--phpunit", phpunit, "--red-check"], cwd=repo, capture_output=True, text=True)
    assert "red check did not run" not in proc.stdout, "the pristine copy lost tests/Helper.php:\n" + proc.stdout
    assert "ran: red check: 1 changed test files fail before the change" in proc.stdout, proc.stdout


RESULTS = []
for test in (files_outside_cwd_are_not_linted, repo_root_cwd_lints_everything, phpstan_with_nothing_in_scope_is_not_a_failure,
             red_check_keeps_export_ignored_tests):
    base = tempfile.mkdtemp(prefix="agents-test-verify-changed-")
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
