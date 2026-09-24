#!/usr/bin/env python3
"""repos/_shared/verify_changed.py: linters configured for --cwd only judge files inside it.

Runs against a throwaway repo and a fake phpcs that flags line 1 of every file it is given.
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


RESULTS = []
for test in (files_outside_cwd_are_not_linted, repo_root_cwd_lints_everything):
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
