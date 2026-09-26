#!/usr/bin/env python3
"""Profiles' private terms stay out of the kit: its commits (git hooks) and its pull requests (guard_bash)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.expanduser("~/.agents")
RESULTS = []


def run(cmd, cwd, env, stdin=None):
    return subprocess.run(cmd, cwd=cwd, env=env, input=stdin, capture_output=True, text=True)


def guard(command, cwd, env):
    payload = json.dumps({"tool_input": {"command": command}, "cwd": cwd, "session_id": "test"})
    out = run([sys.executable, os.path.join(KIT, "hooks", "guard_bash.py")], cwd, env, payload).stdout
    return json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"] if out.strip() else ""


def setup(base):
    profiles = os.path.join(base, "profiles")
    os.makedirs(os.path.join(profiles, "work"))
    open(os.path.join(profiles, "work", "private-terms.txt"), "w").write("# an employer's tickets\nacme-\\d+\n")
    env = {**os.environ, "AGENTS_PROFILES_DIR": profiles, "AGENTS_TEST": "1"}
    other = os.path.join(base, "other")
    os.makedirs(other)
    run(["git", "init", "-q"], other, env)
    return env, other


def kit_prs_are_checked(base):
    env, other = setup(base)
    body = os.path.join(base, "body.md")
    open(body, "w").write("Fixes the flow for ACME-7.\n")
    assert "ACME-12" in guard('gh pr edit 3 --title "Fix ACME-12 flow"', KIT, env), "title, from the kit's checkout"
    assert "ACME-7" in guard(f"gh pr edit 3 --body-file {body}", KIT, env), "description file"
    assert "ACME-12" in guard('gh pr create -R alopezari/agents-kit --title=ACME-12 --body x', other, env), "--repo"
    assert "ACME-9" in guard('gh pr edit 3 --title "Fix a|b; ACME-9" && echo done', KIT, env), "operators inside quotes"
    assert guard('gh pr edit 3 --title "Fix the flow"', KIT, env) == "", "clean title"
    assert guard('gh pr edit 3 --title "Fix ACME-12 flow"', other, env) == "", "other repositories may name them"


def kit_commits_are_checked(base):
    env, other = setup(base)
    open(os.path.join(other, "a.txt"), "w").write("seen in ACME-3\n")
    run(["git", "add", "a.txt"], other, env)
    pre_commit = run([os.path.join(KIT, ".githooks", "pre-commit")], other, env)
    assert pre_commit.returncode != 0 and "ACME-3" in pre_commit.stderr, pre_commit
    message = os.path.join(base, "msg")
    open(message, "w").write("Handle acme-44 renames\n")
    commit_msg = run([os.path.join(KIT, ".githooks", "commit-msg"), message], other, env)
    assert commit_msg.returncode == 1 and "acme-44" in commit_msg.stderr, commit_msg
    open(message, "w").write("Handle renames\n")
    assert run([os.path.join(KIT, ".githooks", "commit-msg"), message], other, env).returncode == 0


for test in (kit_prs_are_checked, kit_commits_are_checked):
    base = tempfile.mkdtemp(prefix="agents-test-terms-")
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
