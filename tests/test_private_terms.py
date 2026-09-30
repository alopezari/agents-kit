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
    open(os.path.join(profiles, "work", "private-terms.txt"), "w").write("# an employer's tickets\nacme-\\d+\nsecret project\n")
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
    assert "ACME-4" in guard('gh pr edit 3 -t fine && gh pr edit 3 -t ACME-4', KIT, env), "every gh command"
    kit_pr = "https://github.com/alopezari/agents-kit/pull/3"
    assert "ACME-4" in guard(f"gh pr edit {kit_pr} -t ACME-4", other, env), "a PR URL"
    assert "ACME-4" in guard("GH_REPO=alopezari/agents-kit gh pr edit 3 -t ACME-4", other, env), "GH_REPO"
    for wrapped in ["env GH_REPO=alopezari/agents-kit gh pr edit 3 -t ACME-4", "command gh pr edit 3 -R alopezari/agents-kit -t ACME-4",
                    "nohup gh pr edit 3 -R alopezari/agents-kit -t ACME-4"]:
        assert "ACME-4" in guard(wrapped, other, env), f"a wrapper in front: {wrapped}"
    assert "ACME-4" in guard("env -i PATH=/bin gh pr edit 3 -t ACME-4", KIT, {**env, "GH_REPO": "someone/other"}), \
        "env -i drops the inherited GH_REPO, so gh acts on the kit"
    assert "ACME-4" in guard("env --unset=GH_TOKEN gh pr edit 3 -t ACME-4", other, {**env, "GH_REPO": "alopezari/agents-kit"}), \
        "unsetting another variable keeps the inherited GH_REPO"
    assert "can't be read" in guard("gh pr edit 3 -F - <<< body", KIT, env), "a body from stdin can't be checked"
    assert "can't be read" in guard("printf x > new.md && gh pr edit 3 -F new.md", KIT, env), "a file made later"
    assert "shell expansion" in guard('gh pr edit 3 -t "$TITLE"', KIT, env), "text the shell produces"
    assert guard("gh pr edit 3 -R ghe.example/alopezari/agents-kit -t ACME-4", other, env) == "", "another host"
    assert guard("gh pr edit 3 -R someone-alopezari/agents-kit -t ACME-4", other, env) == "", "another owner's repo"
    assert "ACME-4" in guard("gh pr edit 3 -t abc#ACME-4", KIT, env), "a # inside a word"
    assert "ACME-4" in guard("gh pr edit 3 -tACME-4", KIT, env), "a short flag with its value attached"
    assert "ACME-4" in guard("gh pr edit 3 -t ACME-4\ngh pr edit 3 -R someone/other -t fine", KIT, env), "newlines"
    assert "ACME-4" in guard(f"cd /tmp && gh pr edit 3 -t a && cd {KIT} && gh pr edit 3 -t ACME-4", other, env), "cd"
    assert "ACME-4" in guard("export GH_REPO=alopezari/agents-kit && gh pr edit 3 -t ACME-4", other, env), "export"
    assert "ACME-4" in guard("gh pr edit 3 -t ACME-4", other, {**env, "GH_REPO": "alopezari/agents-kit"}), "inherited"
    assert guard("GH_HOST=ghe.example gh pr edit 3 -R alopezari/agents-kit -t ACME-4", other, env) == "", "GH_HOST"
    open(os.path.join(other, "-"), "w").write("fine\n")
    assert "can't be read" in guard("gh pr edit 3 -R alopezari/agents-kit -F -", other, env), "a file named -"
    open(os.path.join(base, "clean.md"), "w").write("fine\n")
    assert "can't be read" in guard(f"cd {base} && printf ACME-4 > clean.md && gh pr edit 3 -R alopezari/agents-kit"
                                    " -F clean.md", other, env), "a body file rewritten in the same command"
    assert "ACME-4" in guard('gh pr edit 3 -R "$REPO" -t ACME-4', other, env), "a repository the shell expands"
    assert "ACME-4" in guard("cd tests && cd .. && gh pr edit 3 -t ACME-4", KIT, env), "chained cd"
    assert "ACME-4" in guard("GH_REPO=someone/other gh pr view 3; gh pr edit 3 -t ACME-4", KIT, env), "one-off GH_REPO"
    sys.path.insert(0, os.path.join(KIT, "hooks"))
    import guard_bash
    assert guard_bash.pr_checkout(f"cd /tmp && gh pr edit 3 -t a && cd {KIT} && gh pr create --fill", other) == os.path.realpath(KIT), \
        "the review gate still finds the checkout gh pr create runs in"
    assert "ACME-4" in guard("eval 'gh pr edit 3 -t ACME-4'", KIT, env), "eval"
    assert "ACME-4" in guard("bash -lc 'gh pr edit 3 --title \"Fix ACME-4 flow\"'", KIT, env), "sh -c, a title with spaces"
    assert "ACME-4" in guard("sh -c \"gh pr edit 3 -R alopezari/agents-kit -t ACME-4\"", other, env), "sh -c with --repo"
    assert guard("bash -lc 'gh pr edit 3 -R someone/other -t ACME-4'", KIT, env) == "", "sh -c on another repository"
    assert "ACME-4" in guard("bash -lc 'gh pr edit 3 --title \"hello; ACME-4\"'", KIT, env), "sh -c, an operator in the title"
    assert "Secret Project" in guard("eval 'gh pr edit 3 -t \"Secret Project\"'", KIT, env), "eval, a term of two words"
    assert "ACME-4" in guard("bash -lc 'gh pr edit 3 --repo=\"alopezari/agents-kit\" -t ACME-4'", other, env), "--repo=\"...\""
    assert "ACME-4" in guard(f"bash -lc 'cd {KIT} && gh pr edit 3 -t ACME-4'", other, env), "sh -c that cds into the kit"
    assert guard("bash -lc 'cd /tmp && gh pr edit 3 -t ACME-4'", KIT, env) == "", "sh -c that cds out of the kit"
    assert guard('echo "x; cd /tmp" && gh pr edit 3 -t ACME-4', KIT, env) != "", "a cd inside quotes moves nothing"
    assert "can't be read" in guard("gh pr edit 3 -F 'a\x00b'", KIT, env), "a path open() rejects"
    assert "ACME-4" in guard("sh -c 'cd no-such-dir; gh pr edit 3 -t ACME-4'", KIT, env), "a failed cd leaves gh in the kit"
    assert "ACME-4" in guard(f"bash -lc 'cd \"{KIT}\" && gh pr edit 3 -t ACME-4'", other, env), "sh -c, a quoted cd"
    assert "ACME-4" in guard("bash -lc 'gh pr edit 3 --title \"fine\nACME-4\"'", KIT, env), "sh -c, a title of two lines"
    assert guard_bash.pr_checkout("cd /tmp; cd no-such-dir; gh pr create --fill", KIT) == os.path.realpath("/tmp"), \
        "a failed cd after another leaves the review gate in the first one's folder"
    assert "ACME-4" in guard("bash -lc 'gh pr edit 3 --title \"fine\nACME-4\" -R alopezari/agents-kit'", other, env), \
        "sh -c, flags after a title of two lines"
    open(os.path.join(base, "body file.md"), "w").write("Fixes ACME-8.\n")
    open(os.path.join(base, "body"), "w").write("fine\n")
    assert "ACME-8" in guard(f"cd {base} && sh -c 'gh pr edit 3 -R alopezari/agents-kit -F \"body file.md\"'", other, env), \
        "sh -c, a body file with a space"
    assert "ACME-4" in guard("sh -c 'echo \"cd /tmp\"; gh pr edit 3 -t ACME-4'", KIT, env), "sh -c, a cd that is only text"
    assert "ACME-4" in guard("export GH_REPO=alopezari/agents-kit && sh -c 'gh pr edit 3 -t ACME-4'", other, env), \
        "sh -c after export GH_REPO"
    assert "ACME-4" in guard("GH_REPO=alopezari/agents-kit sh -c 'gh pr edit 3 -t ACME-4'", other, env), \
        "GH_REPO for sh -c only"
    assert "ACME-4" in guard("eval 'gh pr edit 3 -t' ' ACME-4'", KIT, env), "eval joins its arguments"
    assert "ACME-4" in guard("sh -c 'gh pr edit 3 -t 'ACME-4", KIT, env), "sh -c, a script of quoted and bare pieces"
    assert "ACME-4" in guard("sh -c $'gh pr edit 3 -t ACME-4'", KIT, env), "sh -c $'...'"
    assert "ACME-4" in guard("# don't\nsh -c 'gh pr edit 3 -t ACME-4'", KIT, env), "an apostrophe in a comment"
    assert "ACME-4" in guard("sh -c 'gh pr edit 3 -t \"it'\\''s ACME-4\"'", KIT, env), "sh -c, an escaped quote in the script"
    locked = os.path.join(base, "locked")
    os.makedirs(locked, mode=0o000)
    assert "ACME-4" in guard(f"cd {locked}; gh pr edit 3 -t ACME-4", KIT, env), "a folder cd can't enter"
    os.chmod(locked, 0o700)
    assert guard('gh pr edit 3 --title "Fix the flow"', KIT, env) == "", "clean title"
    assert guard('gh pr edit 3 --title "Fix ACME-12 flow"', other, env) == "", "other repositories may name them"


def kit_commits_are_checked(base):
    env, other = setup(base)
    open(os.path.join(other, "a.bin"), "wb").write(b"\0\xff binary ACME-5\n")
    run(["git", "add", "a.bin"], other, env)
    staged = run([sys.executable, os.path.join(KIT, "hooks", "private_terms.py"), "staged"], other, env)
    assert staged.returncode == 1 and "ACME-5" in staged.stderr, ("binary files too", staged)
    run(["git", "reset", "-q"], other, env)
    open(os.path.join(other, "acme-6.txt"), "w").write("fine\n")
    run(["git", "add", "acme-6.txt"], other, env)
    staged = run([sys.executable, os.path.join(KIT, "hooks", "private_terms.py"), "staged"], other, env)
    assert staged.returncode == 1 and "acme-6" in staged.stderr, ("file names too", staged)

    kit = os.path.join(base, "kit")
    run(["git", "worktree", "add", "-q", "--detach", kit], KIT, env)
    try:
        open(os.path.join(kit, "tests", "note.txt"), "w").write("seen in ACME-3\n")
        run(["git", "add", "tests/note.txt"], kit, env)
        pre_commit = run([os.path.join(KIT, ".githooks", "pre-commit")], kit, env)
        assert pre_commit.returncode == 1 and "ACME-3" in pre_commit.stderr, pre_commit
    finally:
        run(["git", "worktree", "remove", "--force", kit], KIT, env)

    message = os.path.join(base, "msg")
    open(message, "w").write("Handle acme-44 renames\n")
    commit_msg = run([os.path.join(KIT, ".githooks", "commit-msg"), message], other, env)
    assert commit_msg.returncode == 1 and "acme-44" in commit_msg.stderr, commit_msg
    open(message, "w").write("Handle renames\n")
    assert run([os.path.join(KIT, ".githooks", "commit-msg"), message], other, env).returncode == 0


def kit_is_recognised_when_it_is_a_worktree(base):
    # A verify testing a branch points ~/.agents at a worktree, whose .git is a file: PRs from the main checkout
    # went unchecked.
    sys.path.insert(0, os.path.join(KIT, "hooks"))
    import guard_bash
    main, worktree, other = (os.path.join(base, name) for name in ("kit", "kit-worktree", "other"))
    for repo in (main, other):
        os.makedirs(repo)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
                       cwd=repo, check=True)
    subprocess.run(["git", "worktree", "add", "-q", "-b", "feature", worktree], cwd=main, check=True)
    guard_bash.KIT = os.path.realpath(worktree)
    assert guard_bash.targets_kit(None, None, main) and guard_bash.targets_kit(None, None, worktree)
    assert not guard_bash.targets_kit(None, None, other)
    guard_bash.KIT = os.path.realpath(os.path.join(main, "not-a-repo"))
    os.makedirs(guard_bash.KIT)
    assert not guard_bash.targets_kit(None, None, main), "a KIT inside another repository isn't that repository"


for test in (kit_prs_are_checked, kit_commits_are_checked, kit_is_recognised_when_it_is_a_worktree):
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
