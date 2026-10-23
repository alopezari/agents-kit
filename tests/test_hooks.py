#!/usr/bin/env python3
"""Regression tests for the kit's hooks. Each test builds throwaway git repos in a temp dir.

Run: python3 ~/.agents/tests/test_hooks.py          every test, each in its own process (exit 1 on any failure)
     python3 ~/.agents/tests/test_hooks.py <name>...  only those, one after another in this process
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import concurrent.futures
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback

# A HOME of our own: the tests add repo overlays and write hook logs, and must never touch the real ones.
KIT = os.path.realpath(os.path.expanduser("~/.agents"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-hooks-home-")
os.environ.pop("EVIDENCE_DIR", None)  # set when a guide's step runs this suite: the tests' evidence would land there
os.environ.pop("AGENTS_LOG_DIR", None)  # the tests read the hook log in their own HOME
os.environ.pop("AGENTS_PROFILES_DIR", None)  # set by a caller, the user's after-turn scripts would run on every stop
os.environ.pop("AGENTS_AFTER_TURN", None)  # inherited from an after-turn's session, no after-turn test could start one
os.makedirs(os.path.expanduser("~/.agents/repos"))
# Not profiles either: the Stop hook would start the user's own after-turn scripts.
for entry in set(os.listdir(KIT)) - {"logs", "repos", "approvals", "profiles"}:
    os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
# Only the kit's shared verify code: a personal overlay linked in would be run, or overwritten, by a test's own.
os.symlink(os.path.join(KIT, "repos", "_shared"), os.path.expanduser("~/.agents/repos/_shared"))
H = os.path.join(KIT, "hooks") + "/"
RESULTS = []
STATE = tempfile.mkdtemp(prefix="agents-state-")  # keeps the real checkouts registry out of the tests
RUN = f"t{os.getpid()}{int(time.time())}-"  # unique session ids: hook state is kept per session


def run_hook(script, payload, cwd=None, env=None):
    out = subprocess.run(["python3", H + script], input=json.dumps(payload), capture_output=True, text=True,
                         cwd=cwd, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **(env or {})}).stdout
    return json.loads(out) if out.strip() else None


def child_cpu():
    """CPU seconds of every child process reaped so far; the difference around one hook call is its speed without the
    wait a loaded runner adds."""
    times = os.times()
    return times.children_user + times.children_system


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def new_repo(base, name="repo", branch="trunk"):
    path = os.path.join(base, name)
    os.makedirs(path)
    git(path, "init", "-q", "-b", branch)
    open(os.path.join(path, "app.py"), "w").write("x = 1\n")
    git(path, "add", "-A")
    git(path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return path


def write_stamp(cwd, kind):
    """Write a stamp as the skills do: validate first saves a report whose check names its evidence."""
    if kind == "validate":
        reports = os.path.expanduser("~/.agents/bin/reports")
        path = subprocess.run([reports, "path", "validation"], cwd=cwd, capture_output=True, text=True).stdout.strip()
        evidence = subprocess.run([reports, "path", "evidence"], cwd=cwd, capture_output=True, text=True).stdout.strip()
        assert os.path.isdir(evidence), f"no evidence directory from {reports}: {evidence!r}"
        with open(path, "w") as fh:
            fh.write("| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n| A1 | app runs | + | PASS | a1.txt |\n")
        with open(os.path.join(evidence, "a1.txt"), "w") as fh:
            fh.write("$ python3 app.py\nexit 0\n")
    return subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", kind], cwd=cwd, capture_output=True, text=True)


def guard(command, cwd="/tmp"):
    d = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": "test"})
    return "deny" if d else "allow"


def stop(session, cwd, edited):
    for f in edited:
        run_hook("post_edit.py", {"session_id": session, "cwd": cwd, "tool_input": {"file_path": f}})
    d = run_hook("stop_checks.py", {"session_id": session, "cwd": cwd})
    return d or {}


def test(fn):
    base = tempfile.mkdtemp(prefix="agents-test-")
    try:
        fn(base)
        RESULTS.append((fn.__name__, None))
    except Exception:  # noqa: BLE001 - report every failure, keep going
        RESULTS.append((fn.__name__, traceback.format_exc(limit=2)))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- guard_bash ------------------------------------------------------------------------------
def the_agent_stays_out_of_the_approvals(base):
    kit = os.path.expanduser("~/.agents")
    os.makedirs(os.path.join(kit, "approvals", "turn"), exist_ok=True)
    for cmd, cwd in [("echo '{}' > ~/.agents/approvals/phases/global.json", "/tmp"),
                     ("cd ~/.agents && echo '{}' > approvals/phases/global.json", "/tmp"),
                     ("cd ~/.agents && touch approvals/linear", "/tmp"), ("cp /tmp/x approvals/linear", kit),
                     ("touch ~/.agents/approvals/session/s1/linear/ENG-1", "/tmp"),
                     ("mkdir -p approvals/session/s1/linear", kit),
                     ("mv x ./approvals/turn/s1", kit), ("tee approvals/linear < /dev/null", kit),
                     ("cd repos && touch ../approvals/linear", kit), ("mkdir -p approvals/turn/s1", kit),
                     ("rm -f approvals", kit), ("touch linear", os.path.join(kit, "approvals")),
                     ("git commit -m x && touch ~/.agents/approvals/linear", "/tmp"),
                     ("python3 -c \"open('/Users/me/.agents/approvals/linear', 'w')\"", "/tmp"),
                     ("bash -c 'cd ~/.agents && touch approvals/linear'", "/tmp"),
                     ("python3 <<'EOF'\nopen('/Users/me/.agents/approvals/linear', 'w')\nEOF", "/tmp"),
                     ("cat f\ntouch ~/.agents/approvals/x", "/tmp"), ("ls ~/.agents\nrm ~/.agents/approvals/x", "/tmp"),
                     ("cd \"$HOME/.agents\" && touch approvals/x", "/tmp"), ("cd ${HOME}/.agents && touch approvals/x", "/tmp"),
                     ("cd ~/.agents/repos && cd .. && touch approvals/x", "/tmp"), ("pushd ~/.agents && touch approvals/x", "/tmp"),
                     ("cd ~/.agents && dd if=/dev/zero of=approvals/x", "/tmp"), ("cat /dev/null > approvals/linear", kit),
                     ("grep x hooks > approvals/linear", kit), ("echo x > $HOME/.agents/approvals/linear", "/tmp"),
                     ("cp x --target-directory=approvals", kit),
                     ("output=\"$(tee ~/.agents/approvals/linear < /tmp/input)\"", "/tmp"),
                     ("cd ~/.agents && python3 -c \"open('approvals/linear', 'w').close()\"", "/tmp"),
                     ("touch \"$HOME/.agents/approvals/notes for user\"", "/tmp"),
                     ("echo hi > \"$HOME/.agents/approvals/notes for user\"", "/tmp"),
                     ("eval -- 'touch ~/.agents/approvals/x'", "/tmp"),
                     ("cd ~/.agents/approvals && unlink linear", "/tmp"),
                     ("cd ~/.agents/approvals && sed -i '' 's|a|b|' linear", "/tmp"),
                     ("cd ~/.agents/approvals && find . -delete", "/tmp"),
                     ("cd ~/.agents/approvals && python3 -c \"import os; os.remove('linear')\"", "/tmp"),
                     ("git log -1\nbash -lc 'touch ~/.agents/approvals/x'", "/tmp"),
                     ("cat <(rm ~/.agents/approvals/linear)", "/tmp"),
                     ("python3 -c \"file = '/Users/me/.agents/approvals/linear'; open(file, 'w')\"", "/tmp"),
                     ("node --eval \"require('fs').writeFileSync('/Users/me/.agents/approvals/x', '')\"", "/tmp"),
                     ("dd if=/dev/null of=\"~/.agents/approvals/my file\"", "/tmp"),
                     ("touch \"$PWD/.agents/approvals/a b\"", "/tmp"),
                     ("python3 - <<'EOF'\ntest = '/Users/me/.agents/approvals/linear'\nopen(test, 'w')\nEOF", "/tmp")]:
        assert guard(cmd, cwd) == "deny", f"should deny from {cwd}: {cmd}"
    try:
        for cmd, cwd in [(c, kit) for c in [
                "grep -rn approvals hooks/", "git commit -m 'Keep ~/.agents/approvals out of reach'",
                "git commit -m \"$(cat <<'EOF'\nGuard ~/.agents/approvals\nEOF\n)\"", "echo 'see ~/.agents/approvals'",
                "ls hooks", "rg -n 'approvals/turn' hooks",
                "skills/kit-review/bundle.sh main Correctness 'keeps the agent out of ~/.agents/approvals'",
                "git log --grep approvals", "echo see approvals", "git commit -m approvals", "python3 -m pytest -k approvals",
                "git grep -n approvals", "cd ~/.agents/approvals && ls", "cat < ~/.agents/approvals/linear",
                "wc -l < ~/.agents/approvals/linear", "command cat ~/.agents/approvals/linear"]] + [("ls -la", os.path.join(kit, "approvals"))]:
            assert guard(cmd, cwd) == "allow", f"should allow from {cwd}: {cmd}"
    finally:  # the other tests share this HOME
        shutil.rmtree(os.path.join(kit, "approvals"), ignore_errors=True)


def only_the_user_grants_an_approval_in_a_dialog(base):
    for cmd in ["~/.agents/bin/approve grant s1 sql.drop-table", "bin/approve grant s1 linear",
                '"/Users/me/.agents/bin/approve" grant s1 linear', "bin/approve  'grant' s1 linear",
                "bash -c '~/.agents/bin/approve grant s1 linear'", "cd /x && python3 ~/.agents/bin/approve grant s1 x",
                "~/.agents/bin/evidence A1 ~/.agents/bin/approve grant s1 linear", "(bin/approve grant s1 x)",
                "bin/approve \\\n  grant s1 linear", "bin/approve \\\n  'grant' s1 linear",
                "bash -c 'bin/approve \\\n grant s1 linear'", "bin/approve > /dev/null grant s1 linear",
                "bin/approve 2>/dev/null grant s1 x", "bin/approve >&2 grant s1 x", "bin/approve &>/dev/null grant s1 x",
                "bin/approve >|/dev/null grant s1 x", "bin/approve &>>log grant s1 x", "bin/approve g\\\nrant s1 x",
                "bin/approve once s1 linear", "bash -c 'bin/approve once s1 x'"]:
        assert guard(cmd) == "deny", f"should deny: {cmd}"
    for cmd in ["~/.agents/bin/approve needed < payload.json", "~/.agents/bin/approve revoke s1 sql.drop-table",
                "git log --grep 'grant'", "echo approved grants", "bin/auto-approve grant s1 x",
                "bin/approve revoke s1 x \\\n  && echo grant", "bin/approve revoke s1 x > /dev/null 2>&1",
                "grep -rn 'approve grant' hooks/", "rg -c 'bin/approve once' tests", "echo 'run bin/approve once s1 x'",
                "git commit -m \"$(cat <<'EOF'\nDocument bin/approve grant\nEOF\n)\"", "gh pr create --title 'approve grant docs' --body x",
                "cat > spec.md <<'EOF'\nThe guard stops `bin/approve grant|once`.\nEOF",
                "printf -- '- Stops `bin/approve grant` and $(bin/approve once)\\n' > changelog.md"]:
        assert guard(cmd) == "allow", f"should allow: {cmd}"
    for cmd in ["eval 'bin/approve grant s1 x'", "x=$(bin/approve grant s1 x)", "bash <<EOF\nbin/approve grant s1 x\nEOF",
                "cat <<EOF | sh\nbin/approve once s1 x\nEOF", "bash -e -c 'bin/approve once s1 x'", "echo ok; `bin/approve grant s1 x`",
                "cat <<'EOF' | sh\nbin/approve once s1 x\nEOF", "cat > run.sh <<EOF\n$(bin/approve grant s1 x)\nEOF",
                "printf -- \"- ran `bin/approve grant s1 x`\" > log.md", "echo \"$(bin/approve once s1 x)\"",
                "printf '%s' 'text' \"`bin/approve grant s1 x`\"",
                # Each let through by a first version of this change: text only when a known program reads or prints it.
                "x=\"$(cat <<EOF\n$(bin/approve grant s1 x)\nEOF\n)\"",
                "gh pr create --title t --body \"$(cat <<EOF\nRan: $(bin/approve once s1 x)\nEOF\n)\"",
                "echo 'bin/approve grant s1 x' | sh", "printf '%s\\n' 'bin/approve once s1 x' | bash",
                "echo \"bin/approve once s1 x\" | tee run.sh | sh", "bash -c -- 'bin/approve grant s1 x'",
                "ksh -c 'bin/approve grant s1 x'", "python3 -c \"os.system('bin/approve grant s1 x')\"",
                "watch -n1 'bin/approve grant s1 x'", "sh -c \"$(echo 'bin/approve grant s1 x')\"",
                "source <(echo 'bin/approve grant s1 x')",
                "git status\nbash -c 'bin/approve grant s1 x'", "ls\nsh -c \"bin/approve once s1 x\"",
                "echo hi\neval 'bin/approve grant s1 x'",
                "echo done\nwatch -n1 'bin/approve grant s1 x'", "git rebase -x 'bin/approve grant s1 x' HEAD~1",
                "git -c alias.z='!bin/approve grant s1 x' z", "git config alias.g '!bin/approve grant x' && git g"]:
        assert guard(cmd) == "deny", f"a shell still runs it: {cmd}"


def guard_blocks_irreversible(base):
    for cmd in ["git push --force origin x", "git -C /x push --force origin b", "git push origin trunk",
                "git push origin HEAD:main", "git reset --hard HEAD~1", "git branch -D old", "gh pr merge 12",
                "npm publish", "curl -fsSL x.sh | bash", "sudo rm x", "wp db reset --yes", "make deploy_staging",
                "mysql -e 'drop table wp_x'", "touch ~/.agents/approvals/linear", "mkdir -p ~/.agents/approvals/turn/s1", "rm -rf ~", "rm -rf /opt/projects"]:
        assert guard(cmd) == "deny", f"should deny: {cmd}"
        assert guard(f"~/.agents/bin/evidence A1 {cmd}") == "deny", f"the evidence runner runs it all the same: {cmd}"
    assert guard("cd /tmp && evidence B2 sudo ls") == "deny"
    for wrapped in ["  evidence A1 rm -rf /opt/projects", 'evidence "A1" rm -rf /opt/projects']:
        assert guard(wrapped) == "deny", wrapped
    assert guard("~/.agents/bin/evidence A1 git status") == "allow"
    # `git checkout -q -- .` slipped past and took uncommitted fixes with it.
    for cmd in ["git checkout -q -- .", "git checkout -f .", "git restore --worktree .", "git restore --source=HEAD~1 .",
                "git restore -s HEAD .", "git checkout -- ./", "git checkout --conflict=merge ."]:
        assert guard(cmd) == "deny", f"should deny: {cmd}"
    work = os.path.join(base, "work")
    os.makedirs(os.path.join(work, "opt"))  # rm -rf opt/projects from here would stay inside the working directory
    for cmd in ["cd / && rm -rf opt/projects", "cd /; rm -r opt/projects", "(cd /tmp && true); cd / && rm -rf opt/x",
                "(cd / && rm -rf opt/projects)"]:
        assert guard(cmd, cwd=work) == "deny", f"a relative rm runs where the cd left it: {cmd}"
    for cmd in ["pushd / && rm -rf opt/x", "cd /\nrm -rf opt/x", "cd - && rm -rf opt/x", "cd -P / && rm -rf opt/x",
                "cd -- / && rm -rf opt/x", "{ cd / && rm -rf opt/x; }", "if true; then cd / && rm -rf opt/x; fi",
                "git restore --staged -Wq ."]:
        assert guard(cmd, cwd=work) == "deny", f"every way of moving counts: {cmd!r}"
    # From /usr, ../x is /x: the subshell's cd into lib must not make it /usr/x.
    assert guard("(cd lib) && rm -rf ../x", cwd="/usr") == "deny"
    # cds the tracking can't follow (a pipeline, a skipped one, popd) must not make a delete look safer than from cwd
    for cmd in ["cd /tmp | cat; rm -rf ../tmp/x", "false && cd /tmp; rm -rf ../tmp/x", "pushd /tmp; popd; rm -rf ../tmp/x"]:
        assert guard(cmd, cwd="/usr/lib") == "deny", f"never less safe than judging from cwd: {cmd}"
    started = child_cpu()
    assert guard(";" * 60000 + "x/" * 30000) == "allow" and child_cpu() - started < 3, "a long line must not stall the guard"
    started = child_cpu()
    assert guard("echo " + "1" * 64000) == "allow" and child_cpu() - started < 3, "a run of digits must not stall the guard"
    # An edit chained before a blocked step was lost without a word: the agent took it as done.
    d = run_hook("guard_bash.py", {"tool_input": {"command": "sed -i '' s/a/b/ notes.md && git push --force origin x"},
                                   "cwd": "/tmp", "session_id": "test"})
    assert "Nothing in this command ran" in d["hookSpecificOutput"]["permissionDecisionReason"], d


def guard_allows_routine(base):
    for cmd in ["git status", "git push -u origin feature/x", "git push --force-with-lease origin feature/x",
                "git branch -d old", "rm -rf /tmp/foo", "gh pr create --help", "make phpunit", "npm run build",
                "echo 'it would truncate the list'"]:
        assert guard(cmd) == "allow", f"should allow: {cmd}"
    assert guard("git restore --staged .") == "allow", "unstaging keeps the work"
    work = os.path.join(base, "work")
    os.makedirs(os.path.join(work, "sub", "build"))
    assert guard("cd sub && rm -rf build", cwd=work) == "allow"
    assert guard("cd lib && make && cd .. && rm -rf lib/build", cwd="/usr") == "allow", "cd .. comes back, not up"
    assert guard("cd -; cd /usr; rm -rf lib/build", cwd="/usr") == "allow", "an absolute cd ends the unknown"
    assert guard("(cd / && ls) && rm -rf lib/build", cwd="/usr") == "allow", "a subshell's cd ends with it"
    assert guard("git restore --staged -- .") == "allow" and guard("git restore -Sq .") == "allow"


def guard_resolves_rm_variables(base):
    def guard_env(command, tmpdir):
        env = {k: v for k, v in os.environ.items() if k != "TMPDIR"}
        env.update({"AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE}, **({"TMPDIR": tmpdir} if tmpdir is not None else {}))
        out = subprocess.run(["python3", H + "guard_bash.py"], capture_output=True, text=True, env=env,
                             input=json.dumps({"tool_input": {"command": command}, "cwd": "/usr/lib", "session_id": "t"}))
        return "deny" if out.stdout.strip() else "allow"
    # From /usr/lib no temp path is inside the working directory: an allow here comes from the resolved variable.
    for cmd in ['T=$(mktemp -d); rm -rf "$T"', 'T="$(mktemp -d)" && rm -rf "$T/sub" $T',
                'T=$(mktemp -d "${TMPDIR:-/tmp}/x.XXXXXX"); rm -rf "$T"', "T=$(mktemp -d /tmp/x.XXXX); rm -r $T",
                "T=$(mktemp -d -t ci); rm -rf $T", "S=/private/tmp/x; E=$S/run; rm -rf \"$E\"",
                "export T=/tmp/a; rm -rf $T", "local T=/tmp/a\nrm -rf ${T}", 'T=$(mktemp -d); cd "$T" && rm -rf sub',
                'true && T=/tmp/x && rm -rf "$T/s"']:
        assert guard_env(cmd, "/private/var/folders/ab/T/") == "allow", f"a temp path set in the command: {cmd!r}"
    for cmd in ['rm -rf "$TMPDIR"/x', 'rm -rf "${TMPDIR:-/tmp}/x"', "rm -rf $TMPDIR/x"]:
        assert guard_env(cmd, "/private/var/folders/ab/T/") == "allow", f"the hook's own TMPDIR: {cmd}"
    assert guard_env('rm -rf "${TMPDIR:-/tmp}/x"', None) == "allow", "the default applies when TMPDIR is unset"
    assert guard_env('rm -rf "$TMPDIR"/x', None) == "deny", "an unset TMPDIR leaves /x"
    assert guard_env('rm -rf "${TMPDIR:-/tmp}/x"', "") == "allow", "the default applies when TMPDIR is empty"
    assert guard_env('rm -rf "$TMPDIR"/x', "") == "deny", "an empty TMPDIR leaves /x"
    for cmd in ['T=/usr/lib; rm -rf "$T"', 'T=$HOME; rm -rf "$T"', 'T=$(mktemp -d /usr/x.XXXX); rm -rf "$T"',
                'T=$(mktemp -d -p /usr); rm -rf "$T"', 'T=$(mktemp -d); T=/; rm -rf "$T"',
                'for d in /tmp/a /usr; do rm -rf "$d"; done', 'rm -rf "$T"', 'T=$(cat list); rm -rf "$T"',
                "T=/tmp/a$X; rm -rf $T", 'rm -rf "$T"; T=/tmp/a', "T=/tmp/a true; rm -rf $T/sub",
                'cd /; T=opt; rm -rf "$T"', 'T=$(mktemp -d); read T; rm -rf "$T"',
                'false && T=/tmp/x; rm -rf "$T/sub"', '(T=/tmp/x); rm -rf "$T/sub"', "T=/tmp/x | true; rm -rf $T/sub",
                'false || T=/tmp/x && rm -rf "$T/sub"', 'T=/tmp/x & rm -rf "$T/sub"',
                'for TMPDIR in /usr; do rm -rf "$TMPDIR"; done', 'read TMPDIR; rm -rf "$TMPDIR"',
                'T="/tmp/a /usr"; rm -rf $T', 'T=/tmp/x; T+=/../../usr; rm -rf "$T"', 'T=/tmp/x; unset T; rm -rf "$T"/usr',
                'if [ -n "$X" ]; then\n  T=/tmp/x\nfi\nrm -rf "$T"/usr', 'X=$(\nT=/tmp/x\n)\nrm -rf "$T"/usr',
                "grep 'a; T=/tmp/x; b' f; rm -rf \"$T/usr\"", 'export TMPDIR=/usr; T=$(mktemp -d); rm -rf "$T"',
                'T=$(mktemp -dp /usr x.XXXX); rm -rf "$T"/..', 'T=$(mktemp -d --tmpdir=/usr); rm -rf "$T"', 'case "$X" in\n  yes)\n    T=/tmp/x;;\nesac\nrm -rf "$T/usr"',
                'false && \\\n  T=/tmp/x; rm -rf "$T/usr"', "T='/tmp/a /usr'; rm -rf $T", "T='/tmp/*'; rm -rf $T",
                'git commit -m "subject\n\nbody for $USER" && rm -rf /usr/share/doc',
                'python3 -c "\nprint(1)\n" && rm -rf /opt', "grep -e 'a\nb' f && rm -rf '/usr/share'",
                'grep -e "a\nb; T=/tmp/x; c" f; rm -rf "$T/usr"']:
        assert guard_env(cmd, "/private/var/folders/ab/T/") == "deny", f"not a temp path for sure: {cmd!r}"
    started = child_cpu()
    assert guard_env("a=" + "b=" * 40 + ") && rm -rf /usr/x", None) == "deny" and child_cpu() - started < 3, \
        "a=b=b=… must split one way, or the guard runs out of time and lets the command through"


def guard_reads_code_not_text(base):
    for cmd in ["cat > /tmp/spec.md <<'EOF'\nrun git push --force origin x\nEOF",
                "cat > \"$(echo /tmp/spec.md)\" <<'EOF'\nthen git branch -D old\nEOF",
                "python3 - <<'EOF'\np = 'notes.md'\ns = open(p).read().replace('a', 'git branch -D old')\n"
                "open(p, 'w').write(s)\nEOF",
                "node - <<'EOF'\nconsole.log('npm publish')\nEOF",
                'grep -n "git reset --hard" hooks/guard_bash.py', 'grep -n "DROP\\|sudo" tests/test_hooks.py',
                "rg -c 'DROP TABLE' schema.sql", "cat > /tmp/n.md <<'EOF'\nnever DROP TABLE here\nEOF",
                "cat > /tmp/n.md <<'EOF'\nrm -rf $UNSET/x\nEOF", "python3 -c 'print(\"git branch -D old\")'",
                "python3 - <<'EOF'\nprint('DROP TABLE x')\nEOF", "git log --format='%s' | grep 'DROP TABLE'",
                "echo 'git push --force origin x' > /tmp/n.md", "node -e 'console.log(\"DROP TABLE x\")'"]:
        assert guard(cmd) == "allow", f"only text: {cmd!r}"
    for cmd in ["bash -c 'git push --force origin x'", "cat <<'EOF' | sh\ngit reset --hard\nEOF",
                "bash <<'EOF'\ngit branch -D old\nEOF", "eval \"git branch -D old\"",
                "python3 - <<'EOF'\nimport os\nos.system('git push --force origin x')\nEOF",
                "python3 - <<'EOF'\nimport subprocess\nsubprocess.run('git branch -D old', shell=True)\nEOF",
                "python3 -c \"import os; os.system('git branch -D old')\"", 'grep -n x "$(git reset --hard)"',
                "x=$(cat <<'EOF'\ngit branch -D old\nEOF\n)", "cat <<'EOF' | sh\nrm -rf $UNSET/x\nEOF",
                'psql -c "DROP TABLE runs"', "mysql -e 'drop table x'",
                "psql -Atc \"select format('DROP DATABASE %I;', datname) from pg_database\"",
                "python3 - <<'EOF'\ncur.execute('DROP TABLE x')\nEOF", "sqlite3 db <<'EOF'\nDROP TABLE x;\nEOF",
                "echo 'git push --force origin x' | sh", "printf 'git branch -D old' |bash",
                'python3 run.py "DROP TABLE x"', "node run.js 'git branch -D old'",
                "git -c 'alias.wipe=!git reset --hard' wipe", "python3 -c \"# <<'EOF'\"\ngit branch -D old",
                "python3 -c 'import subprocess, sys; subprocess.run(sys.argv[1], shell=True)' 'git reset --hard'",
                "echo 'git branch -D old' \\\n  | sh", "python3 -c \"print('git branch -D old')\" | sh",
                "perl -e 'system \"git branch -D old\"'", "ruby -e '`git branch -D old`'",
                "php -r 'shell_exec(\"git reset --hard\");'", "git submodule foreach 'git reset --hard'",
                "git filter-branch --tree-filter 'git clean -fdx' HEAD", "node -e 'require(\"pg\").query(\"DROP TABLE x\")'",
                "php -r 'mysqli_query($c, \"DROP TABLE x\");'", "cat > /tmp/x.sh <<'EOF'\ngit branch -D old\nEOF\nbash /tmp/x.sh",
                "ruby -e 'Kernel.system(\"git branch -D old\")'", "cat > /tmp/x.sh <<'EOF'\ngit branch -D old\nEOF\n/bin/sh /tmp/x.sh",
                "cat > /tmp/x.sh <<'EOF'\ngit branch -D old\nEOF\ncommand bash /tmp/x.sh",
                "git submodule --quiet foreach 'git reset --hard'", "python3 -c 'db.query (\"DROP TABLE x\")'",
                "python3 -c 'import os; os.popen(\"git branch -D old\")'",
                "node -e 'require(\"child_process\").execSync(\"git branch -D old\")'",
                "php -r 'passthru(\"git reset --hard\");'", "php -r 'proc_open(\"git reset --hard\", [], $p);'",
                "python3 -c 'import pty; pty.spawn(\"git reset --hard\")'", "echo 'DROP TABLE x' | psql"]:
        assert guard(cmd) == "deny", f"text that runs: {cmd!r}"

    def reason(command, cwd="/tmp"):
        said = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": "test"})
        return said["hookSpecificOutput"]["permissionDecisionReason"] if said else ""
    text_hint = "reads that text as a command"
    assert text_hint in reason("python3 - <<'EOF'\nimport os\nos.system('git branch -D old')\nEOF")
    assert reason("git branch -D old") and text_hint not in reason("git branch -D old")

    approvals = os.path.join("~", ".agents", "approvals")
    for cmd in [f"python3 - <<'EOF'\np = 'hooks/x.py'\ns = open(p).read().replace('a', '''    d = \"{approvals}/turn\"''')\n"
                "open(p, 'w').write(s)\nEOF",
                f'S=/private/tmp/x; mkdir -p "$S/home/.agents/approvals/turn"',
                'true && S=/private/tmp/x && mkdir -p "$S/home/.agents/approvals/turn"',
                f"cat > \"$(echo /tmp/spec.md)\" <<'EOF'\nnothing under {approvals} changes\nEOF"]:
        assert guard(cmd) == "allow", f"code that edits a mention, or a test HOME: {cmd!r}"
    for cmd in [f"python3 - <<'EOF'\nimport os\nopen(os.path.expanduser('{approvals}/linear'), 'w').write('x')\nEOF",
                'mkdir -p "$X/.agents/approvals/turn"', f"touch {approvals}/linear",
                'mkdir -p "$S/.agents/approvals/turn"; S=/private/tmp/x',
                "bash <<'EOF'\ncd ~/.agents\ntouch approvals/linear\nEOF",
                "node - <<'EOF'\n// don't\nrequire('fs').writeFileSync(\"/home/u/.agents/approvals/linear\", 'x')\nEOF",
                "node - <<'EOF'\nrequire('fs').writeFileSync(`/home/u/.agents/approvals/linear`, 'x')\nEOF",
                "ruby - <<'EOF'\nFile.write(%q(/home/u/.agents/approvals/linear), 'x')\nEOF"]:
        assert guard(cmd) == "deny", f"a write into the approvals: {cmd!r}"

    repo = new_repo(base)
    git(repo, "switch", "-qc", "feat")
    open(os.path.join(repo, "b.py"), "w").write("b = 1\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b")
    for cmd in ["gh pr create --help", "gh pr create -h", "cat > \"$(echo /tmp/spec.md)\" <<'EOF'\nthen gh pr create\nEOF"]:
        assert not reason(cmd, repo), f"no PR is opened: {cmd!r}"
    for cmd in ["gh pr create --base trunk --title t", "bash -c 'gh pr create --base trunk --title t'"]:
        assert reason(cmd, repo), f"opens a PR without a review: {cmd!r}"

    started = child_cpu()
    long = "S=/tmp/a; T=$(mktemp -d \"$S/x.XXXX\"); grep -n 'DROP|sudo' f; " * 300 + \
        "python3 - <<'EOF'\n" + "s = 'git push --force'\n" * 2000 + "EOF\n" + "cat <<'EOF'\n" + "x\n" * 2000
    assert guard(long) == "allow" and child_cpu() - started < 3, "a long mixed command must not stall the guard"
    started = child_cpu()
    assert guard(long + "EOF\ngit branch -D old") == "deny" and child_cpu() - started < 3, "the whole long command is read"
    # Each line once re-read every blank line after it: a closed 16,000-line body took 26 s.
    heredoc, script = "cat <<'EOF'\n{}EOF\nls", "python3 - <<'EOF'\n{}EOF\nls"
    for shape, body in ((heredoc, "x\n"), (heredoc, "\n"), (heredoc, "  \n"), (script, "s = 'git push --force'\n"),
                        ("{}ls", "!\n"), ("{}ls", "then\n")):
        cost = {}
        for lines in (2000, 8000):
            started = child_cpu()
            assert guard(shape.format(body * lines)) == "allow"
            cost[lines] = child_cpu() - started
        assert cost[8000] < 1.5 and cost[8000] < 6 * cost[2000], f"cost must grow linearly in {body!r}: {cost}"
    blank = "\n\n  \n\t\n"
    for after in ("sudo ls", "git branch -D old"):
        assert reason("cat <<'EOF'\nx\nEOF" + blank + after) == reason(after) != "", f"blank lines hide nothing: {after}"
    prose = "cat > /tmp/o.md <<'EOF'\ngit branch -D old\nEOF\n"
    assert reason(prose + blank + "bash /tmp/s.sh") == reason(prose + "bash /tmp/s.sh") != "", \
        "a script a shell runs after blank lines may be the heredoc, so it is still read as commands"
    spawns = "cat > /tmp/s.py <<'EOF'\nimport os\nos.system('git branch -D old')\nEOF\n"
    assert reason(spawns + blank + "python3 /tmp/s.py") == reason(spawns + "python3 /tmp/s.py") != "", \
        "a script python3 runs after blank lines is still read"
    assert guard("cat > /tmp/s.py <<'EOF'\ns = 'git branch -D old'\nEOF\n" + blank + "python3 /tmp/s.py") == "allow", \
        "and read as Python"


def guard_command_starts_stay_on_their_line(base):
    # The gh pr, cd and GH_REPO readers only meet a long command in a kit checkout or a push, so the shape is checked here.
    source = open(H + "guard_bash.py").read()
    crossing = re.compile(r"\[(?!\^)[^\[\]]*\\n[^\[\]]*\][^\[\]\n\"]{0,30}?\\s[*+]")
    found = [f"guard_bash.py:{source[:m.start()].count(chr(10)) + 1}: {m.group(0)}" for m in crossing.finditer(source)]
    assert not found, "whitespace after a class holding \\n must be [^\\S\\n]:\n" + "\n".join(found)
    assert crossing.search(r'r"(?:^|[;&|\n]\s*)(cd)"') and not crossing.search(r'r"(?:^|[;&|\n])[^\S\n]*cd"')


def guard_reads_through_wrappers_and_scripts(base):
    for cmd in ["env X=1 sudo ls", "env sudo ls", "X=1 sudo ls", "time sudo ls", "command sudo ls", "nohup sudo ls",
                "env -i PATH=/bin sudo ls", "if true; then env sudo ls; fi", "(X=1 sudo ls)",
                "env rm -rf /usr", "env -i rm -rf /usr", "nohup rm -rf /usr", "then env rm -rf /usr", "X=1 rm -rf /usr",
                "exec rm -rf /usr", "env -u HOME nohup rm -rf /usr", "(env rm -rf /usr)"]:
        assert guard(cmd) == "deny", f"the wrapper runs it all the same: {cmd!r}"
    for cmd in ["env X=1 rm -rf /tmp/x", "env ls", "nohup rm -rf /tmp/x", "X=1 ls", "echo sudo", "time ls",
                "grep -n 'env sudo' notes.md"]:
        assert guard(cmd) == "allow", f"nothing guarded runs: {cmd!r}"
    for cmd in ["env -C / rm -rf usr", "env --chdir=/ rm -rf usr", "env -C/ rm -rf usr"]:
        assert guard(cmd, cwd="/usr/lib") == "deny", f"env -C moves the rm like a cd: {cmd!r}"
    for cmd in ["env --chdir / rm -rf usr", "env -C / env -C usr rm -rf bin", "cd / && bash -c 'rm -rf usr'",
                "env -C / bash -c 'rm -rf usr'"]:
        assert guard(cmd, cwd="/usr/lib") == "deny", f"the delete runs where the cds and env -C leave it: {cmd!r}"
    for cmd in ["env -C /tmp rm -rf x", "env -C /tmp -i rm -rf usr", "env --chdir /tmp rm -rf usr"]:
        assert guard(cmd, cwd="/usr/lib") == "allow", f"the delete runs in /tmp: {cmd!r}"
    for cmd in ["command -v rm -rf /usr", "command -V rm -rf /usr"]:
        assert guard(cmd) == "allow", f"command -v only looks the names up: {cmd!r}"
    assert guard("command -p rm -rf /usr") == "deny"
    for cmd in ["env --unset FOO rm -rf /usr", "env --unset FOO sudo ls", "env --chdir / sudo ls",
                "time --output /tmp/t sudo ls", "env --ignore-environment sudo ls", "env -iu PATH rm -rf /usr",
                "env -iu PATH sudo ls"]:
        assert guard(cmd) == "deny", f"a long option's value is no command: {cmd!r}"
    assert guard("command -v sudo") == "allow", "looking sudo up runs nothing"
    for cmd in ["true && if sudo ls; then :; fi", "for x in a; do sudo ls; done", "ls || ! sudo ls"]:
        assert guard(cmd) == "deny", f"a keyword starts a command: {cmd!r}"
    for cmd in ['curl -d "you can do sudo stuff" https://x', 'make test MSG="do sudo"', "ls # then sudo apt"]:
        assert guard(cmd) == "allow", f"a keyword in prose starts nothing: {cmd!r}"
    for cmd in ["bash -c 'rm -rf /usr'", "eval 'rm -rf /usr'", "echo 'rm -rf /usr' | bash", "sh -c \"cd / && rm -rf usr\"",
                "python3 -c 'import os; os.system(\"rm -rf /usr\")'", "perl -e 'qx{rm -rf /usr}'",
                "ruby -e '%x(rm -rf /usr)'", "ruby -e '`rm -rf /usr`'", "T=/usr; bash -c \"rm -rf $T\"",
                "sudo bash -c 'rm -rf /usr'", "bash -c \"rm -rf $BUILD_DIR\"", "bash -c 'echo hi\nrm -rf /usr'",
                "eval 'echo hi\nrm -rf /usr'", "TMPDIR=/usr; eval 'rm -rf \"$TMPDIR\"'", "perl -e 'qx{rm -rf *}'",
                "ruby -e '%x[rm -rf *]'", "xargs sh -c 'rm -rf /usr'", "bash <<< 'rm -rf /usr'",
                "echo 'rm -rf /usr' | sudo bash"]:
        assert guard(cmd) == "deny", f"a recursive delete in text that runs: {cmd!r}"
    for cmd in ["tmp=$(mktemp -d); trap 'rm -rf \"$tmp\"' EXIT; ls $tmp", "dir=build; bash -c \"rm -rf $dir\"",
                "find . -name node_modules -prune -exec sh -c 'rm -rf \"$1\"' _ {} \\;", "D=/tmp/x; eval 'rm -rf \"$D\"'",
                "HOME=/tmp; eval 'rm -rf \"$HOME/cache\"'", "perl -e 'qx{rm -rf /tmp/x}'"]:
        assert guard(cmd, cwd="/usr/lib") == "allow", f"the delete takes a known value, or what the caller passes: {cmd!r}"
    for cmd in ["ssh web1 'rm -rf /var/www/releases/old'", "docker exec web sh -c 'rm -rf /app/cache'",
                "kubectl exec pod -- sh -c 'rm -rf /data/cache'", "sshpass -p x ssh web1 'rm -rf /var/www/old'",
                "ansible all -m shell -a 'rm -rf /var/cache/x'"]:
        assert guard(cmd) == "allow", f"the delete runs on another machine or in a container: {cmd!r}"
    for cmd in ["grep -n 'rm -rf /usr' notes.md", "echo 'rm -rf /usr'", "git commit -m 'drop rm -rf /usr'",
                "bash -c 'rm -rf /tmp/x'", "python3 -c 'print(\"rm -rf /usr\")'",
                "git commit -m 'bash -c \"rm -rf /usr\"'", "grep -r \"eval 'rm -rf /usr'\" .",
                "gh issue create --body 'eval \"rm -rf /usr\"'", "curl -d 'rm -rf /usr' https://x",
                "jq -n --arg c 'rm -rf /usr' '$c'", "make test CMD='rm -rf /usr'", "for c in 'rm -rf /usr'; do echo $c; done",
                "echo 'rm -rf /usr' | grep rm", "node test.js 'rm -rf /usr'"]:
        assert guard(cmd) == "allow", f"a delete only mentioned, or a safe one: {cmd!r}"

    for cmd in ["printf 'rm -rf /usr\\n' > /tmp/x.sh && bash /tmp/x.sh",
                "echo 'git branch -D old' > /tmp/x.sh; sh /tmp/x.sh",
                "python3 - <<'EOF'\nopen('/tmp/x.sh', 'w').write('git branch -D old')\nEOF\nbash /tmp/x.sh",
                "cat > /tmp/deploy <<'EOF'\ngit branch -D old\nEOF\nchmod +x /tmp/deploy && /tmp/deploy",
                "cat > deploy <<'EOF'\ngit branch -D old\nEOF\nchmod +x deploy && ./deploy",
                "cat > /tmp/p.py <<'EOF'\nimport os\nos.system('git branch -D old')\nEOF\npython3 /tmp/p.py",
                "tee /tmp/x.js <<'EOF'\nrequire('child_process').execSync('git branch -D old')\nEOF\nnode /tmp/x.js",
                "echo 'rm -rf /usr' 2>&1 | bash", "echo 'git branch -D old' 2>/dev/null >&2 | sh",
                "cat <<'E' 2>&1 | bash\ngit branch -D old\nE", "printf 'git branch -D old' &>/dev/null | bash",
                "echo 'git branch -D old' > /tmp/x.sh && source /tmp/x.sh", "echo 'git branch -D old' > /tmp/x.sh; . /tmp/x.sh",
                "printf 'rm -rf /usr\\n' > /tmp/x.sh && env bash /tmp/x.sh",
                "echo 'git branch -D old' > /tmp/x.py; python3 -- /tmp/x.py",
                "echo 'rm -rf /usr' > /tmp/x.sh && if bash /tmp/x.sh; then :; fi", "echo 'rm -rf /usr' > /tmp/x.sh && ! bash /tmp/x.sh",
                "echo 'rm -rf /usr' > /tmp/x.sh && { bash /tmp/x.sh; }",
                "cat > /tmp/p.py <<'EOF'\nimport os\nos.system('rm -rf /usr')\nEOF\n.venv/bin/python /tmp/p.py"]:
        assert guard(cmd) == "deny", f"text written, then run: {cmd!r}"
    for cmd in ["cat > notes.md <<'EOF'\ngit branch -D old\nEOF\ngit add notes.md",
                "echo 'rm -rf /usr' 2>&1", "echo 'git branch -D old' >&2", "echo 'rm -rf /usr' &> /tmp/log",
                "printf 'git branch -D old' > /tmp/n.md && cat /tmp/n.md",
                "cat > /tmp/n.md <<'EOF'\ngit branch -D old\nEOF\npython3 -c 'print(1)'",
                "cat > /tmp/n.md <<'EOF'\ngit branch -D old\nEOF\npython3 report.py",
                "echo 'git branch -D old' > notes.md && node build.js",
                "echo 'git branch -D old' > /tmp/x.sh; bash /tmp/y.sh", "echo 'git branch -D old' > /tmp/x; /tmp/y",
                "python3 <<'P'\nr = (total/count)\nprint(\"then sudo apt install x\")\nP",
                "python3 <<'P'\nx = (a/b)\nprint(\"git branch -D old\")\nP",
                "python3 - <<'E'\nimport git\nprint(\"git branch -D old\")\nE\n/usr/bin/git status",
                "python3 - <<'E'\nimport pytest\nprint(\"git branch -D old\")\nE\npython3 -m pytest"]:
        assert guard(cmd) == "allow", f"text that nothing runs: {cmd!r}"
    for cmd in ["cat > /tmp/v.sh <<'EOF'\ntrap 'rm -rf /nonexistent-x' EXIT\nEOF\nbash -c 'echo hi'",
                "python3 - <<'PY'\np = '/tmp/s.sh'\nnew = \"\"\"\n  rm -rf \"$TMP\"\n\"\"\"\nPY\nbash -n /tmp/s.sh",
                "cat > /tmp/m.py <<'EOF'\nexec(src, ns)\nNAME = 'DROP DATABASE'\nEOF\npython3 /tmp/m.py",
                "python3 - <<'PY'\nwrappers = ('env', 'exec')\nNAME = 'DROP DATABASE'\nPY"]:
        assert guard(cmd) == "allow", f"a script written and not run, or run without starting a process: {cmd!r}"
    for cmd in ["cat > /tmp/t.py <<'PY'\nimport sys\nPY\ncat > /tmp/c1.txt <<'EOF'\nbin/approve grant linear\nEOF\n"
                "python3 /tmp/t.py /tmp/c1.txt",
                "cat > /tmp/b.txt <<'EOF'\nduckdb my.db 'DROP TABLE t'\nEOF\npython3 /tmp/pr.py /tmp/b.txt",
                "cat > /tmp/r.sh <<EOF\nT=\\$(mktemp -d); trap 'rm -rf \"\\$T\"' EXIT\nEOF\nbash /tmp/r.sh"]:
        assert guard(cmd) == "allow", f"a heredoc written to a file nothing runs is text: {cmd!r}"
    for cmd in ["python3 - <<'EOF'\nopen('tests/t.py', 'w').write(\"CASES = ['git branch -D old']\")\nEOF\npython3 tests/t.py",
                "python3 - <<'EOF'\nopen('tests/t.py', 'w').write(\"NAME = 'DROP DATABASE'\")\nEOF\nHOME=/tmp/h python3 tests/t.py"]:
        assert guard(cmd) == "allow", f"a script that edits a file python3 then runs writes Python, not shell: {cmd!r}"
    for cmd in ["python3 - <<'EOF'\np = 'tests/run.sh'\ns = open(p).read()\ns = s.replace('# end', 'git branch -D old')\n"
                "open(p, 'w').write(s)\nEOF\nbash tests/run.sh",
                "python3 - <<'EOF'\nfrom pathlib import Path\np = Path('deploy.sh')\np.write_text(p.read_text() + 'git branch -D old')\nEOF\n"
                "bash deploy.sh"]:
        assert guard(cmd) == "allow", f"an edit of a script it then runs is the script's own code, as with an editor: {cmd!r}"
    for cmd in ["cat > /tmp/p.py <<'EOF'\nC = [\"bash -c 'x'\", 'git branch -D old']\nprint(C)\nEOF\npython3 /tmp/p.py",
                "python3 - <<'EOF'\nC = ['grep -qx \"D\" f', 'git branch -D old']\nEOF",
                "python3 - <<'EOF'\nNOTE = 'Open3 is Ruby; git branch -D old'\nEOF",
                "perl - <<'EOF'\nmy @c = ('grep -qx \"D\" f', 'git branch -D old');\nEOF"]:
        assert guard(cmd) == "allow", f"an interpreter script's strings are text: {cmd!r}"
    for cmd in ["cat > /tmp/x.sh <<'EOF'\nbash -c 'rm -rf /usr'\nEOF\nbash /tmp/x.sh",
                "ruby - <<'EOF'\nOpen3.capture2('git branch -D old')\nEOF",
                "perl - <<'EOF'\nqx{git branch -D old};\nEOF"]:
        assert guard(cmd) == "deny", f"a script that runs its strings: {cmd!r}"
    for cmd in ["cat > /tmp/x.sql <<'EOF'\nDROP TABLE t;\nEOF\npsql -f /tmp/x.sql",
                "cat > /tmp/x.sh <<'EOF'\nbin/approve grant linear\nEOF\ncat > /tmp/n.txt <<'EOF'\nhi\nEOF\nbash /tmp/x.sh",
                "cat > /tmp/r.sh <<EOF\nT=/usr; trap 'rm -rf \"\\$T\"' EXIT\nEOF\nbash /tmp/r.sh"]:
        assert guard(cmd) == "deny", f"a heredoc written to a file the command runs is code: {cmd!r}"
    for cmd in ["cat > /tmp/t.py <<'EOF'\nprint('then sudo apt install x')\nEOF\npython3 /tmp/t.py",
                "S=/tmp/notes.sh; python3 - \"$S\" <<'PY'\n# edits notes.sh\nnew = '''\n  rm -rf \"$TMP\"\n'''\nPY"]:
        assert guard(cmd, cwd="/tmp") == "allow", f"a Python script's strings are text unless it starts a process: {cmd!r}"

    for cmd in ["python3 -c 'import asyncio; asyncio.create_subprocess_shell(\"git branch -D old\")'",
                "python3 -c 'asyncio.create_subprocess_exec(*\"git reset --hard\".split())'",
                "ruby -e 'Open3.capture2(\"git branch -D old\")'", "ruby -e 'spawn(\"git branch -D old\")'",
                "ruby -e '%x(git branch -D old)'", "perl -e 'qx{git branch -D old}'",
                "perl -e 'exec \"git branch -D old\"'", "ruby -e 'system \"git branch -D old\"'",
                "python3 -c 'duckdb.sql(\"DROP TABLE x\")'", "node -e 'knex.raw(\"DROP TABLE x\")'",
                "node -e 'prisma.$executeRaw(\"DROP TABLE x\")'", "node -e 'prisma.$queryRawUnsafe(\"DROP TABLE x\")'",
                "node -e 'prisma.$queryRaw(\"DROP TABLE x\")'", "node -e 'db.raw(\"DROP TABLE x\")'",
                "duckdb -c 'DROP TABLE x'", "ruby -e 'Open3.popen3(\"git branch -D old\")'"]:
        assert guard(cmd) == "deny", f"a process or SQL call: {cmd!r}"
    for cmd in ["python3 -c 'print(\"git branch -D old\")'", "ruby -e 'puts \"git branch -D old\"'",
                "perl -e 'print \"git branch -D old\"'", "node -e 'console.log(\"DROP TABLE x\")'",
                "python3 -c 'x = \"DROP TABLE x\"'"]:
        assert guard(cmd) == "allow", f"text a program only prints: {cmd!r}"


def guard_mcp_linear(base):
    os.makedirs(os.path.join(base, "profiles", "work"))
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["subtool"],
                             "writes": {"linear": ["create-issue"]}}]},
              open(os.path.join(base, "profiles", "work", "mcp-writes.json"), "w"))
    gateway = "mcp__plugin_gateway__execute"
    cases = [("mcp__linear__get_issue", {}, None), ("mcp__linear__save_comment", {}, "deny"),
             (gateway, {"provider": "linear", "subtool": "issue"}, None),
             (gateway, {"provider": "linear", "subtool": "create-issue"}, "deny")]
    for tool, inp, want in cases:
        got = run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": inp, "cwd": "/tmp", "session_id": "test"},
                       env={"AGENTS_PROFILES_DIR": os.path.join(base, "profiles")})
        got = "deny" if got else None
        assert got == want, f"{tool} {inp}: {got} != {want}"


def a_block_says_when_no_approval_dialog_can_show(base):
    def block(payload=(), **harness):
        env = {"HOME": base, "AGENTS_HARNESS": "", "CLAUDECODE": "", "CODEX_THREAD_ID": "", "AGENTS_KIT_MOD": "", **harness}
        subprocess_env = {k: v for k, v in {**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env}.items() if v}
        done = subprocess.run(["python3", H + "guard_mcp.py"], capture_output=True, text=True, env=subprocess_env,
                              input=json.dumps({"tool_name": "mcp__linear__save_issue", "tool_input": {}, "cwd": "/tmp",
                                                "session_id": "d1", "hook_event_name": "PreToolUse", **dict(payload)}))
        return json.loads(done.stdout)

    unmarked = block(CLAUDECODE="1")
    reason = unmarked["hookSpecificOutput"]["permissionDecisionReason"]
    assert unmarked["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "approval dialog unavailable" in reason and "kit mod" in reason, reason
    assert "approval dialog unavailable" in unmarked.get("systemMessage", ""), "the user sees it, not only the agent"
    assert "name linear in your next message" in unmarked["systemMessage"], "and how to allow it without the dialog"
    for marked in [block(CLAUDECODE="1", AGENTS_KIT_MOD="1"), block(CODEX_THREAD_ID="th-1", CLAUDECODE="1"), block(),
                   block({"turn_id": "turn-1"}, CLAUDECODE="1")]:
        assert "systemMessage" not in marked and "dialog" not in marked["hookSpecificOutput"]["permissionDecisionReason"], \
            "the mod loaded, or Codex (no dialog there to miss, even started from Claude Code), or no harness known"


def asking_for_a_service_approves_its_writes_for_that_turn(base):
    os.makedirs(os.path.join(base, "profiles", "work"))
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["subtool"],
                             "writes": {"tracker": ["create-issue"]}}]},
              open(os.path.join(base, "profiles", "work", "mcp-writes.json"), "w"))
    env = {"HOME": base, "AGENTS_PROFILES_DIR": os.path.join(base, "profiles")}

    def prompt(text, session="s1"):
        run_hook("prompt_approvals.py", {"prompt": text, "session_id": session, "cwd": "/tmp"}, env=env)

    def write(tool="mcp__linear__save_issue", inp=None, session="s1"):
        got = run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": inp or {}, "cwd": "/tmp", "session_id": session},
                       env=env)
        return "deny" if got else "allow"

    stale = os.path.join(base, ".agents", "approvals", "turn", "gone")
    os.makedirs(stale)
    os.utime(stale, (time.time() - 2 * 86400,) * 2)
    prompt("Crea una tarea en Linear para esto")
    assert write() == "allow", "the user asked for Linear in this message"
    assert write(session="s2") == "deny", "another session's message approves nothing here"
    assert not os.path.exists(stale), "approvals of sessions long gone are cleared"
    prompt("now fix the failing test")
    assert write() == "deny", "the approval lasts until the user's next message"
    prompt("this scales linearly")
    assert write() == "deny", "only the service's own name counts"
    gateway = ("mcp__plugin_gateway__execute", {"provider": "tracker", "subtool": "create-issue"})
    assert write(*gateway) == "deny"
    prompt("open an issue in the Tracker")
    assert write(*gateway) == "allow", "services a profile declares too"
    assert write() == "deny", "and only the one named"
    turn = ("mcp__plugin_gateway__execute", {"provider": "turn", "subtool": "create-issue"})
    rules = json.load(open(os.path.join(base, "profiles", "work", "mcp-writes.json")))
    rules["gateways"][0]["writes"].update({"turn": ["create-issue"], "wiki-": ["create-issue"]})
    json.dump(rules, open(os.path.join(base, "profiles", "work", "mcp-writes.json"), "w"))
    prompt("open an issue in the Tracker")
    assert write(*turn) == "deny", "a service named turn isn't approved by the folder of turn approvals"
    prompt("use wiki- for it")
    assert write("mcp__plugin_gateway__execute", {"provider": "wiki-", "subtool": "create-issue"}) == "allow", \
        "a name that ends in a symbol"


def an_answer_to_the_agents_offer_approves_the_service_it_named(base):
    os.makedirs(os.path.join(base, "profiles", "work"))
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["subtool"],
                             "writes": {"linear": ["create-issue"], "tracker": ["create-issue"]}}]},
              open(os.path.join(base, "profiles", "work", "mcp-writes.json"), "w"))
    env = {"HOME": base, "AGENTS_PROFILES_DIR": os.path.join(base, "profiles")}
    offer = ("Tengo el borrador listo: título «Flaky checkout test», equipo WCCOM, proyecto Checkout Reliability.\n\n"
             "¿Quieres que la cree en Linear?")
    answer = ("Si, crea la tarea en el equipo WCCOM, ponla en todo, asignamela a mi, prioridad media y metela en el "
              "proyecto que dijiste")

    def entry(kind, content, **extra):
        return {"type": kind, "message": {"role": kind, "content": content}, "isSidechain": False,
                "timestamp": "2026-10-20T08:00:00Z", **extra}

    def said(text):
        return entry("assistant", [{"type": "text", "text": text}])

    history = [entry("user", "look at the flaky checkout test"), entry("assistant", [{"type": "thinking", "thinking": "…"}]),
               entry("assistant", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}]),
               entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}])]

    def transcript(*entries, padding=0, big_first=0):
        path = os.path.join(base, "t.jsonl")
        with open(path, "w") as fh:
            for _ in range(padding):
                fh.write(json.dumps(said("y" * 1000)) + "\n")
            if big_first:  # a tool result longer than the tail, which the tail cuts into
                fh.write(json.dumps(entry("user", [{"type": "tool_result", "tool_use_id": "t1", "content": "r" * big_first}])) + "\n")
            for line in entries:
                fh.write((line if isinstance(line, str) else json.dumps(line)) + "\n")
        return path

    def prompt(text, path, session="c1", codex=False):
        payload = {"prompt": text, "session_id": session, "cwd": "/tmp", "hook_event_name": "UserPromptSubmit"}
        if path is not None:
            payload["transcript_path"] = path
        if codex:
            payload["turn_id"] = "turn-2"
        done = subprocess.run(["python3", H + "prompt_approvals.py"], input=json.dumps(payload), capture_output=True,
                              text=True, timeout=10, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env})
        assert done.returncode == 0 and "Traceback" not in done.stderr, done.stderr

    def write(service="linear", session="c1"):
        got = run_hook("guard_mcp.py", {"tool_name": "mcp__plugin_gateway__execute", "cwd": "/tmp", "session_id": session,
                                        "tool_input": {"provider": service, "subtool": "create-issue"}}, env=env)
        return "deny" if got else "allow"

    def approves(text, *entries, padding=0, big_first=0, codex=False):
        prompt(text, transcript(*entries, padding=padding, big_first=big_first), codex=codex)
        return write()

    proposal = [*history, said("Voy a revisar el test."), said(offer)]
    assert approves(answer, *proposal) == "allow", "the user's yes to the agent's offer to create it in Linear"
    assert write("tracker") == "deny", "only the service the offer named"
    assert approves(answer, *proposal, entry("user", answer + "\n")) == "allow", "the prompt already in the transcript"
    prompt("now fix the failing test", transcript(*proposal, entry("user", answer), said("Creada: WCCOM-12.")))
    assert write() == "deny", "until the user's next message: a create still needs a new approval"
    for reply in ["sí", "Sí.", "dale", "go ahead", "yes please", "créala", "En el equipo WCCOM, créala",
                  "Sí, créala, pero no urgente", "ok, create it", "vale, gracias", "perfecto, adelante", "sure, do it"]:
        assert approves(reply, *proposal) == "allow", f"{reply!r} accepts the offer"
    for reply in ["no", "No, espera", "no la crees todavía", "wait", "not yet", "Sí, pero espera a que lo revise",
                  "¿En qué proyecto?", "mejor no", "arregla el test primero", "Sí, ¿pero no era otro equipo?",
                  "Sí, pero ahora no", "Si, pero no", "claro que no", "vale, mejor no", "yes but not now",
                  "ok never mind", "vale, déjalo", "sí, más tarde", "show me first", "Sí, pero enséñamela antes",
                  "please", "por favor", "please fix the test first", "Si quieres, primero arregla el test",
                  "make it shorter", "haz el cambio en el test", "si va bien", "Si puedes, mira el CI", "pon el título en inglés",
                  "open the test file", "ok, but use the Tracker instead"]:
        assert approves(reply, *proposal) == "deny", f"{reply!r} doesn't accept it"
    assert approves("sí", *history, said("He revisado el test: falla por un timeout.")) == "deny", "no proposal"
    assert approves("sí", entry("user", "hola")) == "deny", "no agent message at all"
    assert approves("sí", *history, said("La tarea de Linear WCCOM-3 trata de esto. El fix está en el test.")) == "deny", \
        "names Linear but proposes nothing"
    assert approves("sí", *history, said("La tarea de Linear WCCOM-3 trata de esto. ¿Quieres que cree un test?")) == \
        "deny", "an offer to write whose question doesn't name the service"
    for question in ["¿Quieres que busque la tarea en Linear?", "¿Abro la tarea en Linear para revisarla?",
                     "Should I open the issue in Linear to check it?", "¿Quieres que no la cree en Linear?",
                     "¿Creo que está en Linear, la busco?", "¿La creo en Linear o en GitHub?",
                     "Did you already create it in Linear?", "I don't want to create it in Linear yet, ok?",
                     'Él preguntó: "¿La creo en Linear?"', "¿Archivo de Linear, lo reviso?",
                     "Como Linear está caído, ¿la creo en el Tracker?", "¿Hay algo que quieras que cambie antes, o la creo en Linear?"]:
        assert approves("sí", *history, said(question)) == "deny", f"{question!r} offers no write"
    for question, reply in [("¿La actualizo en Linear?", "sí"), ("¿La borro en Linear?", "bórrala"),
                            ("**¿La creo en Linear?**", "sí"), ("Should I create the issue in Linear?", "yes"),
                            ("¿Puedo crearla en Linear con estos datos?", "adelante"), ("¿La creamos en Linear?", "venga"), ("En Linear, ¿la creo?", "sí"),
                            ("Want me to file an issue in Linear for this?", "yes, go ahead"),
                            ("**Should I create an issue in Linear?**", "yes"), ("Should I create\nan issue in Linear?", "yes"),
                            ("Borrador listo:\n¿La creo\nen Linear?", "sí"), ("Draft ready:\n- Should I file it in Linear?", "ok")]:
        assert approves(reply, *history, said(question)) == "allow", f"{reply!r} to {question!r}"
    assert approves("sí", *history, said(offer + "\n\nTambién puedo dejarlo para luego, lo que prefieras.")) == "deny", \
        "the question isn't the last thing the agent said"
    assert approves("sí", *proposal, entry("assistant", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {}}])) \
        == "deny", "the agent went on to a tool call after asking"
    assert approves("sí", *proposal, entry("user", "otra cosa: mira el CI")) == "deny", \
        "an older message of the user's sits between"
    assert approves("sí", *proposal, {**said("Hecho."), "isSidechain": True}) == "allow", \
        "a subagent's text isn't the agent's last message"
    assert approves("sí", *history, {**said("¿Quieres que la cree en Linear?"), "isSidechain": True}) == "deny", \
        "and a subagent's offer approves nothing"
    assert approves("sí", *proposal, {"type": "attachment", "attachment": {}}, {"type": "system", "content": "x"}) == \
        "allow", "entries that aren't messages are skipped"
    assert approves("Do you want me to file it?", *history, said("Want me to file an issue in Linear for this?")) == "deny"
    assert approves("sí", *proposal, padding=2500) == "allow", "a recent exchange after megabytes of history"
    assert approves("sí", *proposal, big_first=300_000) == "allow", "a long tool result before the run is left out"
    assert approves("sí", said(offer), big_first=300_000) == "deny", "the line the tail cuts into could be more of the message"
    assert approves("sí", *history, said("z" * 300_000), said(offer)) == "deny", "a run of the agent's text longer than the tail"
    assert approves("sí", *history, said("z" * 300_000 + " ¿Quieres que la cree en Linear?")) == "deny", \
        "a last message longer than the tail read isn't read"
    started = time.monotonic()
    assert approves("sí", *history, said("palabra " * 25_000 + "¿Quieres que la cree en Linear?")) == "allow"
    assert time.monotonic() - started < 3, "a long sentence is read in linear time, well within the hook's timeout"
    codex = [{"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": offer}}]
    assert approves("sí", *codex, codex=True) == "allow", "a Codex rollout"
    assert approves("sí", *codex, {"type": "event_msg", "payload": {"type": "user_message", "message": "sí"}},
                    codex=True) == "allow", "a Codex rollout that already holds the prompt"
    assert approves("sí", *codex, {"type": "event_msg", "payload": {"type": "turn_aborted", "turn_id": "turn-1b"}},
                    codex=True) == "deny", "a Codex turn the user interrupted after the offer"
    assert approves("sí", *codex, {"type": "event_msg", "payload": {"type": "user_message", "message": "mira el CI"}},
                    codex=True) == "deny", "an older Codex message of the user's sits between"
    prompt("sí", None)
    assert write() == "deny", "no transcript"
    prompt("sí", os.path.join(base, "missing.jsonl"))
    assert write() == "deny", "a transcript that isn't there"
    for broken in [["{not json", json.dumps(said(offer))[:-5]], [*proposal, json.dumps("a string")],
                   [*proposal, json.dumps([1, 2])], [*proposal, "[" * 100_000 + "]" * 100_000],
                   [*proposal, {"type": "assistant", "message": {"content": {"text": "x"}}}],
                   [*proposal, {"type": "user", "message": None}], [*proposal, {"type": "assistant", "message": []}],
                   [*proposal, {"type": "user", "message": {"content": []}}], [*proposal, {"type": "event_msg", "payload": "x"}]]:
        prompt("sí", transcript(*broken))
        assert write() == "deny", f"a corrupt transcript: {str(broken[-1])[:40]}"
    with open(os.path.join(base, "bin.jsonl"), "wb") as fh:
        fh.write(os.urandom(4096))
    prompt("sí", os.path.join(base, "bin.jsonl"))
    assert write() == "deny", "a binary transcript"
    with open(os.path.join(base, "latin.jsonl"), "wb") as fh:
        fh.write(json.dumps(said(offer), ensure_ascii=False).encode().replace("¿".encode(), b"\xbf") + b"\n")
    prompt("sí", os.path.join(base, "latin.jsonl"))
    assert write() == "deny", "a transcript that isn't UTF-8"
    prompt("sí, y abre otra en el Tracker", transcript(*proposal, json.dumps([1, 2])))
    assert write("tracker") == "allow", "the literal name still approves, whatever the transcript"
    prompt("sí, y abre otra en el Tracker", transcript(*proposal))
    assert (write(), write("tracker")) == ("allow", "allow"), "both rules in one message"


def a_write_to_an_object_the_user_approved_is_approved_for_the_session(base):
    profiles = os.path.join(base, "profiles")
    os.makedirs(os.path.join(profiles, "work"))
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["subtool", "tool"],
                             "writes": {"linear": ["add-comment", "attach-url", "create-issue", "update-issue"],
                                        "tracker": ["update-issue"]},
                             "objects": {"linear": {"fields": ["params.id", "params.issueId"],
                                                    "created": {"create-issue": "identifier"}}}}],
               "direct": {"notes": "^(create|update)_"},
               "direct_objects": {"notes": {"fields": ["id"], "created": {"create_note": "id"}}}},
              open(os.path.join(profiles, "work", "mcp-writes.json"), "w"))
    env = {"HOME": base, "AGENTS_PROFILES_DIR": profiles}
    approvals = os.path.join(base, ".agents", "approvals")

    def prompt(text, session="o1"):
        run_hook("prompt_approvals.py", {"prompt": text, "session_id": session, "cwd": "/tmp"}, env=env)

    calls = iter(range(1000))

    def call(operation, params, session="o1", response="", service="linear", tool="mcp__plugin_gateway__execute",
             during=lambda: None):
        """The harness's PreToolUse, then, only when it let the call through, `during` (what happens while the tool
        runs) and its PostToolUse with the response."""
        tool_input = {"provider": service, "tool": operation, "params": params} if "gateway" in tool else params
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": "/tmp", "session_id": session,
                   "tool_use_id": f"toolu_{next(calls)}"}
        denied = run_hook("guard_mcp.py", {**payload, "hook_event_name": "PreToolUse"}, env=env)
        if denied:
            return denied["hookSpecificOutput"]["permissionDecisionReason"]
        during()
        assert run_hook("guard_mcp.py", {**payload, "hook_event_name": "PostToolUse", "tool_response": response},
                        env=env) is None, "recording says nothing to the agent"
        return "allow"

    def record(session, service, object_id):
        return os.path.join(approvals, "session", session, service, object_id)

    # The 2026-10-19 session: the user asked for a Linear task, then, in their next message, for more in it.
    created = json.dumps({"identifier": "ENG-1130", "title": "Investigate", "url": "https://linear.app/acme/issue/ENG-1130/x",
                          "tool": "create-issue", "subtool": "create-issue"})
    prompt("create a Linear task in the ENG team for this")
    assert call("create-issue", {"team": "ENG", "title": "Investigate"}, response=created) == "allow"
    assert call("update-issue", {"id": "ENG-1130", "status": "Todo"}) == "allow"
    assert call("attach-url", {"issueId": "ENG-1130", "url": "https://github.com/o/r/pull/1"},
                response=json.dumps({"id": "cf9fe0bb-643a-4c26-90d1-8070bce57447", "tool": "attach-url"})) == "allow"
    prompt("Asegúrate de que la tarea cubre tanto los woo e2e tests como woo api tests como activation")
    assert call("update-issue", {"id": "ENG-1130", "description": "e2e, api, activation"}) == "allow", \
        "a follow-up edit to the task the user had created"
    assert call("attach-url", {"issueId": "ENG-1130", "url": "https://github.com/o/r/pull/2"}) == "allow"
    assert call("add-comment", {"issueId": "ENG-1130", "body": "done"}) == "allow"
    assert call("update-issue", {"id": "ENG-1130", "issueId": ""}) == "allow", "an empty field names no object"
    assert call("update-issue", {"id": "eng-1130"}) != "allow", "ids match exactly, even on a case-insensitive disk"
    other = call("update-issue", {"id": "ENG-1131", "status": "Done"})
    assert "didn't ask for linear" in other and "approved writing to earlier in this session" in other, other
    assert call("update-issue", {"id": "cf9fe0bb-643a-4c26-90d1-8070bce57447"}) != "allow", \
        "attach-url's response id is the attachment's, never recorded"
    assert call("create-issue", {"team": "ENG", "title": "Another"}) != "allow", "a new object needs the user"
    assert call("create-issue", {"id": "ENG-1130", "team": "ENG"}) != "allow", "a create never rides on a record"
    assert call("update-issue", {"id": "ENG-1130"}, session="o2") != "allow", "another session's record"
    assert call("update-issue", {"id": "ENG-1130"}, session="") != "allow", "no session, no record"
    for params in [{"status": "Done"}, {"id": ""}, {"id": "../o1/linear/ENG-1130"}, {"id": ["ENG-1130"]}]:
        assert call("update-issue", params) != "allow", f"no plain object id: {params}"
    assert call("update-issue", {"id": "ENG-1130", "issueId": "ENG-1131"}) != "allow", "every id it names must be recorded"
    assert call("update-issue", {"id": "ENG-1130", "issueId": "../ENG-1130"}) != "allow", "and be a plain id"
    wrote = run_hook("guard_files.py", {"tool_name": "Write", "cwd": "/tmp", "session_id": "o1",
                                        "tool_input": {"file_path": record("o1", "linear", "ENG-1131"), "content": ""}}, env=env)
    assert wrote and "approvals come from the user" in wrote["hookSpecificOutput"]["permissionDecisionReason"]

    # The window: 24 h from the last write the user approved, not from follow-ups.
    day = 24 * 3600
    os.utime(record("o1", "linear", "ENG-1130"), (time.time() - day + 3600,) * 2)
    prompt("one more thing")
    assert call("update-issue", {"id": "ENG-1130"}) == "allow"
    assert time.time() - os.path.getmtime(record("o1", "linear", "ENG-1130")) > day - 3700, "a follow-up doesn't extend it"
    assert call("update-issue", {"id": "ENG-1130"}, during=lambda: os.remove(record("o1", "linear", "ENG-1130"))) == "allow"
    assert not os.path.exists(record("o1", "linear", "ENG-1130")), "nor brings back a record swept while it ran"
    open(record("o1", "linear", "ENG-1130"), "w").write("ENG-1130")
    prompt("in Linear, also set the priority")
    assert call("update-issue", {"id": "ENG-1130", "priority": 1}) == "allow"
    assert time.time() - os.path.getmtime(record("o1", "linear", "ENG-1130")) < 60, "an approved write restarts it"
    os.utime(record("o1", "linear", "ENG-1130"), (time.time() - day - 60,) * 2)
    prompt("and close it")
    assert call("update-issue", {"id": "ENG-1130", "status": "Done"}) != "allow", "an expired record"
    gone = record("gone", "linear", "ENG-1")
    os.makedirs(os.path.dirname(gone))
    open(gone, "w").close()
    os.utime(gone, (time.time() - day - 60,) * 2)
    assert not os.path.exists(record("o1", "linear", "ENG-1130")), "the last message swept the expired record"
    os.makedirs(os.path.dirname(record("o1", "linear", "ENG-7")))
    open(record("o1", "linear", "ENG-7"), "w").close()
    prompt("anything")
    assert not os.path.exists(os.path.join(approvals, "session", "gone")), "expired records go, with their folders"
    assert os.path.exists(record("o1", "linear", "ENG-7")), "a user message keeps the session's records"

    # The dialog's one-call approval, revoked while the call runs (the mod's order), and the manual file once it expired.
    approve = lambda *args: subprocess.run([os.path.join(KIT, "bin", "approve"), *args], capture_output=True,  # noqa: E731
                                           env={**os.environ, **env, "AGENTS_TEST": "1"}).returncode
    prompt("go on", session="o3")
    revoke = lambda: approve("revoke", "o3", "linear")  # noqa: E731
    assert approve("once", "o3", "linear") == 0
    blocks = [{"type": "text", "text": json.dumps({"identifier": "ENG-1200", "tool": "create-issue"})}]
    assert call("create-issue", {"team": "ENG"}, session="o3", response=blocks, during=revoke) == "allow"
    assert call("update-issue", {"id": "ENG-1200"}, session="o3") == "allow", "the object the dialog approved creating"
    assert call("create-issue", {"team": "ENG"}, session="o3") != "allow"
    os.utime(record("o3", "linear", "ENG-1200"), (time.time() - day + 60,) * 2)
    assert approve("once", "o3", "linear") == 0
    assert call("update-issue", {"id": "ENG-1200", "priority": 2}, session="o3", during=revoke) == "allow"
    assert time.time() - os.path.getmtime(record("o3", "linear", "ENG-1200")) < 60, "a write the dialog approved restarts it"
    prompt("go on", session="o4")
    manual = os.path.join(approvals, "linear")
    open(manual, "w").close()
    mcp_result = {"content": [{"type": "text", "text": json.dumps({"identifier": "ENG-1300"})}]}
    assert call("create-issue", {"team": "ENG"}, session="o4", response=mcp_result) == "allow"
    assert call("create-issue", {"team": "ENG"}, session="", response=json.dumps({"identifier": "ENG-1400"})) == "allow"
    os.utime(manual, (time.time() - 3600,) * 2)
    assert call("update-issue", {"id": "ENG-1400"}, session="") != "allow", "calls with no session share no records"
    assert call("update-issue", {"id": "ENG-1300"}, session="o4") == "allow", "after the manual approval expired"
    assert call("update-issue", {"id": "ENG-1301"}, session="o4") != "allow"
    open(manual, "w").close()
    assert call("create-issue", {"team": "ENG"}, session="o4", response=json.dumps({"identifier": "../ENG-1"})) == "allow"
    assert not os.path.exists(os.path.join(approvals, "session", "o4", "ENG-1")), "a returned id is a plain id too"
    open(os.path.join(approvals, "session", "o8"), "w").close()  # the store can't be written
    assert call("update-issue", {"id": "ENG-1500"}, session="o8") == "allow", "a failed record never blocks the write"
    os.utime(manual, (time.time() - 3600,) * 2)
    assert call("update-issue", {"id": "ENG-1500"}, session="o8") != "allow", "it only leaves the follow-up to ask"
    os.remove(manual)

    # A direct server's rules, and a service whose rules name no object fields.
    prompt("save it in notes", session="o5")
    assert call("create_note", {"title": "x"}, session="o5", response={"id": "n-1"}, tool="mcp__notes__create_note") == "allow"
    prompt("tidy it", session="o5")
    assert call("update_note", {"id": "n-1"}, session="o5", tool="mcp__notes__update_note") == "allow"
    assert call("update_note", {"id": "n-2"}, session="o5", tool="mcp__notes__update_note") != "allow"
    prompt("add a second one in notes", session="o5")
    both = {"structuredContent": {"title": "y"}, "content": [{"type": "text", "text": json.dumps({"id": "n-3"})}]}
    assert call("create_note", {"title": "y"}, session="o5", response=both, tool="mcp__notes__create_note") == "allow"
    prompt("tidy that one too", session="o5")
    assert call("update_note", {"id": "n-3"}, session="o5", tool="mcp__notes__update_note") == "allow"
    prompt("in the tracker", session="o6")
    assert call("update-issue", {"id": "T-1"}, session="o6", service="tracker") == "allow"
    prompt("again", session="o6")
    unchanged = call("update-issue", {"id": "T-1"}, session="o6", service="tracker")
    assert "didn't ask for tracker. Show them" in unchanged, f"no object rules: as before: {unchanged}"


DATABASE_STATEMENTS = {"DROP DATABASE": "psql -c 'DROP DATABASE arm_test_1'", "DROP TABLE": "psql -c 'DROP TABLE runs'",
                       "DROP SCHEMA": "psql -c 'DROP SCHEMA s'", "TRUNCATE TABLE": "psql -c 'TRUNCATE TABLE runs'"}


def database_approval_hooks(base):
    """`prompt(text)` sends a user message through prompt_approvals.py; `guard_in(command)` runs guard_bash.py on a
    command and returns "allow" or the denial."""
    env = {**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, "HOME": base,
           "AGENTS_PROFILES_DIR": os.path.join(base, "profiles")}

    def hook(script, payload):
        done = subprocess.run(["python3", H + script], input=json.dumps(payload), capture_output=True, text=True,
                              env=env)
        assert done.returncode == 0 and not done.stderr, f"{script} failed: {done.stderr}"
        return json.loads(done.stdout) if done.stdout.strip() else None

    def prompt(text, session="s1"):
        hook("prompt_approvals.py", {"prompt": text, "session_id": session, "cwd": "/tmp"})

    def guard_in(command, session="s1", cwd="/tmp"):
        got = hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": session})
        if not got:
            return "allow"
        assert got["hookSpecificOutput"]["permissionDecision"] == "deny", got
        return got["hookSpecificOutput"]["permissionDecisionReason"]

    return prompt, guard_in


def an_allow_line_approves_a_database_statement_for_that_turn(base):
    prompt, guard_in = database_approval_hooks(base)
    statements = DATABASE_STATEMENTS
    denial = guard_in(statements["DROP DATABASE"])
    assert "destroys database data" in denial and "`allow DROP DATABASE`" in denial, "the denial says how to allow it"
    for name, command in statements.items():
        prompt(f"Allow {name.lower()}  \nSí, adelante.")
        assert guard_in(command) == "allow", f"an opening allow line approves {name}, in any case"
        others = [c for n, c in statements.items() if n != name]
        assert all(guard_in(c) != "allow" for c in others), f"allowing {name} approves no other statement"
    prompt("allow TRUNCATE TABLE\nallow DROP SCHEMA\nboth on the scratch copy")
    assert guard_in("psql -c 'TRUNCATE TABLE runs; DROP SCHEMA s'") == "allow", "several opening allow lines"
    prompt("allow DROP DATABASE\r\nthanks")
    assert guard_in("PGPASSWORD=s3cret psql -c 'drop  database x'") == "allow", \
        "a CRLF line approves; the command in any case and spacing"
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        logged = [json.loads(line) for line in fh if '"allow-approved"' in line]
    assert [e["detail"] for e in logged][-1:] == ["sql.drop-database"], "an approved statement leaves a trace"
    assert not any("s3cret" in e["detail"] for e in logged), "the trace holds the approval, not the command's secrets"
    assert guard_in(statements["DROP DATABASE"], session="s2") != "allow", "another session's message approves nothing"
    assert guard_in("psql -c 'DROP DATABASE x; TRUNCATE TABLE runs'") != "allow", "every statement in the command"
    assert guard_in("psql -c 'DROP DATABASE x' && wp db reset --yes") != "allow", "other rules still hold"
    prompt("now fix the failing test")
    assert guard_in(statements["DROP DATABASE"]) != "allow", "the approval lasts until the user's next message"
    # Each of these named the statement; a mention can't tell a request from a question, a refusal or a paste.
    for text in ["DROP DATABASE", "Sí, borra las bases de prueba: DROP DATABASE", "why did you run DROP DATABASE?",
                 "don't allow DROP DATABASE", "allow DROP DATABASE later, not now", "> allow DROP DATABASE",
                 "-- dump\nDROP DATABASE IF EXISTS shop;\nCREATE DATABASE shop;", f"why was this blocked? {denial}",
                 "Explain this, don't run it:\n```\nallow DROP DATABASE\n```", "an example:\n    allow DROP DATABASE",
                 "Explain this:\n~~~sql\nallow DROP DATABASE\n~~~", "```text\nallow DROP DATABASE",
                 "allow DROP\nDATABASE", "ok\nallow DROP DATABASE", "    allow DROP DATABASE\n", "\nallow DROP DATABASE"]:
        prompt("allow DROP DATABASE")
        prompt(text)
        assert guard_in(statements["DROP DATABASE"]) != "allow", f"grants nothing and revokes the last one: {text!r}"
    approvals = os.path.join(base, ".agents", "approvals")
    open(os.path.join(approvals, "sql.drop-table"), "w").close()
    assert guard_in(statements["DROP TABLE"]) == "allow", "the user's own approval file works as for services"
    os.utime(os.path.join(approvals, "sql.drop-table"), (time.time() - 3600,) * 2)
    assert guard_in(statements["DROP TABLE"]) != "allow", "and expires as theirs do"


def a_plain_request_approves_a_database_statement_for_that_turn(base):
    prompt, guard_in = database_approval_hooks(base)
    assert "in plain words" in guard_in(DATABASE_STATEMENTS["DROP DATABASE"]), "the denial says a plain request works"

    def allowed():
        return {name for name, command in DATABASE_STATEMENTS.items() if guard_in(command) == "allow"}

    for text, grants in [("borra las bases de prueba", {"DROP DATABASE"}),
                         ("Sí, borra las bases de prueba", {"DROP DATABASE"}),
                         ("Si por favor, borra las bases de prueba", {"DROP DATABASE"}),
                         ("Perfecto.\nBorra la base de datos de prueba", {"DROP DATABASE"}),
                         ("¡Sí! Borra las bases de prueba", {"DROP DATABASE"}),
                         ("Vale\nborra las bases de prueba", {"DROP DATABASE"}),
                         ("drop the test databases please", {"DROP DATABASE"}),
                         ("Go ahead and delete the arm_test_* databases", {"DROP DATABASE"}),
                         ("Borra también las tablas de prueba", {"DROP TABLE"}),
                         ("vacía las tablas de prueba", {"TRUNCATE TABLE"}),
                         ("Vacia las tablas", {"TRUNCATE TABLE"}),
                         ("Gracias.\n\nBorra las bases de prueba. Gracias!", {"DROP DATABASE"}),
                         ("Truncate the run_logs table", {"TRUNCATE TABLE"}),
                         ("Drop the test databases, and empty the old test tables!", {"DROP DATABASE", "TRUNCATE TABLE"}),
                         ("allow DROP SCHEMA\nwipe all the test tables", {"DROP SCHEMA", "DROP TABLE"})]:
        prompt(text)
        assert allowed() == grants, f"{text!r} approves {sorted(grants)}, got {sorted(allowed())}"
    for text in ["can you drop the test databases?", "Don't drop the database",
                 "Borra las bases de prueba.\n\nY luego revisa el CI"]:
        prompt("allow DROP DATABASE\nallow DROP TABLE\nallow DROP SCHEMA\nallow TRUNCATE TABLE")
        prompt(text)
        assert not allowed(), f"grants nothing and revokes the last ones: {text!r}, got {sorted(allowed())}"

    # Only an imperative request opening the message grants: a verb or object word anywhere else is a question, a
    # refusal, a report, a paste, or a word for something else.
    negatives = [
        "¿puedes borrar las bases de prueba si te doy permiso?", "borra las bases de prueba?",
        "Could you drop the test databases", "Could you, please, drop the test databases",
        "Is it safe to drop the database", "Which scripts drop the tables", "Quién elimina la tabla runs",
        "Why did you delete the tables", "I didn’t ask you to drop the database", "you shouldn't drop the database",
        "I asked you to drop the test databases", "no borres las bases", "Do not (drop the test databases)",
        "Do not run any of these:\n- drop the test databases\n- truncate the runs table",
        "Things to avoid: drop the test databases", "Never run destructive stuff, e.g. drop the test databases",
        "Avoid these:\n1. drop the test databases", "Ya borre las bases de prueba", "Ya borré las bases de prueba",
        "you deleted the tables", "ERROR:  cannot drop the currently open database",
        "Failed to drop the table runs: permission denied", "I'll drop the test databases myself",
        "Let me drop the test databases myself", "Maybe drop the test databases",
        "Write a script to drop the test databases", "Our teardown hooks drop the test databases after each run",
        "El teardown borra la tabla runs al final y por eso falla", "Mira por qué el test elimina la base de datos",
        "Quieres que borre la tabla", 'Juan said "drop the test databases" in the ticket',
        "Ana dijo: borra las bases de prueba", '"drop the test databases"', "“borra las bases de prueba”",
        "Rather than drop the table, truncate it", "Drop the test databases, no, wait, keep them",
        "Borra las bases de prueba. Bueno, mejor no.", "After the migration passes, drop the old tables",
        "En cuanto termines, borra las tablas viejas", "Si te doy permiso, borra las bases de prueba",
        "Borra las bases de prueba si, como dijiste, ya no sirven.",
        "Borra las bases solo si, por ejemplo, pasan las pruebas", "drop the test databases later",
        "drop the test databases next week", "Drop the test databases after I confirm",
        "Drop the test databases provided the backup succeeds", "borra los datos de la tabla runs",
        "delete all rows from the table", "drop the rows of the table", "Delete the table rows for user 5",
        "Delete the table headers", "Wipe the table contents", "drop the table of contents",
        "remove the database password from the logs", "Delete the database backup file", "Delete the database.yml file",
        "Remove the db connection pool", "Delete the schema.json file", "Elimina el esquema de validación del formulario",
        "Remove the table from the settings page and use a list", "Drop all the tables except users",
        "Remove the base class", "Elimina la clase base y usa composición", "Borra la imagen base del Dockerfile",
        "vacía las bases de prueba", "the empty table shows no rows", "the drop table button is broken",
        "> borra las bases de prueba", "    drop the test databases", "  drop the test databases",
        "\tdrop the test databases", "```\nborra las bases de prueba\n```", "~~~\nborra las bases de prueba\n~~~",
        "Mira esto:\n```\nborra las bases de prueba", "drop table runs;", "DROP the test databases",
        "Run `drop the test databases` again", "drop the test databases if exists",
        "Drop the test databases. Is that safe?", "Borra las bases de prueba. ¿Es seguro?",
        "Drop the old tables. But only after the migration passes.", "Borra las bases de prueba. Solo si pasan los tests.",
        "Drop the test databases. Tomorrow.", "Borra las bases de prueba. Olvídalo.", "Drop the test databases. Nevermind.",
        "Drop the test databases\n\nActually no, wait, keep them.", "Drop the test databases\nDont do it yet",
        "# drop the test databases\nfor db in dbs:\n    cur.execute(q)", "// drop the test databases\nteardown();",
        "~~drop the test databases~~", "[ ] drop the test databases", '"""\nDrop the test databases.\n"""',
        "Drop the legacy tables\nThis issue is assigned to me, write a spec for it.", "Remove the Zod schema",
        "remove the old schemas", "Borra el esquema", "Delete the word database", "Elimina la palabra tabla",
        "Drop the test databases\nif the backup succeeds", "Drop the test databases\n  Wait, do not do that.",
        '"Yes. Drop the test databases. Thanks."', "delete /the/test/database", "Drop the test databases. Scratch that.",
        "Delete the file database", "Delete the environment variable database", "Elimina el término tabla",
        "Drop the test databases. But...", "Borra las bases de prueba, pero...", "Borra las bases de prueba, si",
        "Drop the test databases and", "Borra las bases de prueba...", "Dale: drop the test databases\nYou: thanks",
        "You: empty the old test tables", "Delete the env-var database", "Drop the test databases. .",
        "Drop the test databases, thank", "Drop the test databases, por", "Borra las bases de prueba y ¡",
        "Delete the _word_ database", "Delete the **environment variable** database", "Borra las bases de prueba por",
        "drop the test databases and, thanks", "drop the test databases..", "Remove the old base", "Empty result tables", "Remove the user settings page table",
        "Delete the data in tables", "Borra la imagen base", "(drop the test databases)", "`drop the test databases`",
        "Drop the test databases. I don't want that.","Sí, borra las bases de prueba: DROP DATABASE"]
    code = (f"import json, sys; sys.path.insert(0, {H!r}); import guard_bash; "
            "print(json.dumps([guard_bash.database_approval_names(t) for t in json.load(sys.stdin)]))")
    done = subprocess.run(["python3", "-c", code], input=json.dumps(negatives), capture_output=True, text=True,
                          env={**os.environ, "HOME": base})
    assert done.returncode == 0 and not done.stderr, done.stderr
    granted = {t: g for t, g in zip(negatives, json.loads(done.stdout)) if g}
    assert not granted, f"grant nothing: {granted}"


def a_message_asking_to_merge_approves_gh_pr_merge_for_that_turn(base):
    prompt, guard_in = database_approval_hooks(base)
    merge = "gh pr merge 12 --squash"
    denial = guard_in(merge)
    assert "`allow gh pr merge`" in denial and "plain words" in denial, "the denial says how to allow it"
    for text in ["Vale ya esta listo, mergealo, instalalo y dime si queda algo pendiente", "allow gh pr merge",
                 "allow gh pr merge\nand then install it", "Mergéalo", "Sí, mergea el PR", "haz el merge",
                 "Perfecto. Haz merge y luego instálalo.", "si pasa el CI, mergealo", "merge it", "Merge this please",
                 "OK, merge the PR", "Go ahead and merge", "merge #12", "Merge PR 12 and install it", "mergeadlo",
                 "Mergeala ya", "Great, thanks! Merge it.", "merge it once CI passes", "Mergéalo y después instálalo",
                 "Merge it. Don't install it.", "merge it into main", "Steps:\n- check\n\nmerge it"]:
        prompt("now fix the failing test")
        assert guard_in(merge) != "allow", "a message that doesn't ask revokes the last one"
        prompt(text)
        assert guard_in(merge) == "allow", f"{text!r} asks for the merge"
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        logged = [json.loads(line) for line in fh if '"allow-approved"' in line]
    assert [e["detail"] for e in logged][-1:] == ["git.merge"], "an approved merge leaves a trace"
    for admin in ["gh pr merge 12 --admin --squash", "gh pr merge 12 \\\n  --admin", 'gh pr merge 12 "--admin"']:
        assert guard_in(admin) != "allow", f"--admin skips the checks the user may count on: {admin!r}"
    assert guard_in(merge, session="s2") != "allow", "another session's message approves nothing"
    assert guard_in("gh pr merge 12 && git push --force origin x") != "allow", "other rules still hold"
    for text in ["Merged.", "Merged. Ahora que?", "¿lo mergeo?", "Lo mergeo?", "should I merge it?",
                 "can you merge it?", "Can you merge it", "no lo mergees todavía", "No lo mergees", "don't merge it",
                 "do not merge it", "merge it later, not now", "Merge it. Actually, wait.", "Mergealo. Bueno, espera.",
                 "the reviewer wrote 'merge it'", 'Ana dijo "mergealo"', "> merge it", "    merge it", "```\nmerge it\n```",
                 "ok\nallow gh pr merge", "after the merge, install it", "the merge failed", "I'll merge it myself",
                 "Lo mergeo yo", "why did you merge it?", "merge conflicts in hooks/x.py", "Don't merge it yet",
                 "merge it tomorrow", "No mergees nada", "the merge is mine", f"why was this blocked? {denial}",
                 "merge it?", "¿mergealo?", "Merge it, actually wait.", "Merge it, but not yet.", "Merge it. No.",
                 "Merge it. Do not merge it.", "Do not, under any circumstances, merge it", "Merge it - kidding",
                 "The reviewer wrote: merge it", "TODO: merge it", "The reviewer wrote:\nmerge it",
                 "The reviewer wrote 'Looks good, merge it, thanks'", 'He said "Looks good. Merge it. Ship it."',
                 "Merge it is the next step.", "Merge the PR failed because of conflicts.", "merge it after I review",
                 "merge it once I say so", "merge it when I tell you", "Merge this branch into feature-x locally",
                 "Next step: merge.", "Merge it, after I review", "Merge it, when I say so", "Merge it, later",
                 "Merge it when CI passes and I approve", "merge it and let me check it first", "Merge it. Stop.",
                 "Mergealo, pero despues de que lo revise", "Merge it. Abort.", "Merge it. Hold off.", "merge it, right?",
                 "Merge it. Don't.", "Merge it. Do not.", "Merge it. Note: do not merge it."]:
        prompt("allow gh pr merge")
        prompt(text)
        assert guard_in(merge) != "allow", f"grants nothing and revokes the last one: {text!r}"
    prompt("allow gh pr merge")
    for rewrite in ["git push -uf origin feature && gh pr merge 12", "git push --force-with-lease origin x && gh pr merge 12"]:
        assert guard_in(rewrite) != "allow", f"a merge approval doesn't carry a force push: {rewrite!r}"
    prompt("allow gh pr merge\nallow DROP TABLE")
    assert guard_in("gh pr merge 12 && psql -c 'DROP TABLE t'") == "allow", "each grant lifts its own rule"
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        logged = [json.loads(line)["detail"] for line in fh if '"allow-approved"' in line]
    assert sorted(logged[-1].split(", ")) == ["git.merge", "sql.drop-table"], f"the trace names every grant: {logged[-1:]}"


def a_message_asking_to_push_approves_that_protected_branch(base):
    prompt, guard_in = database_approval_hooks(base)
    push = "git push origin main"
    denial = guard_in(push)
    assert "`allow git push main`" in denial, "the denial says how to allow it"
    for text in ["allow git push main", "pushea a main", "Vale, haz push a main", "push to main", "Push it to main"]:
        prompt("now fix the failing test")
        prompt(text)
        for command in [push, "git push -u origin main", "git push origin HEAD:main", "git push upstream main"]:
            assert guard_in(command) == "allow", f"{text!r} approves {command!r}"
        assert guard_in("git push origin production") != "allow", f"{text!r} names main only"
        for rewrite in ["git push --force origin main", "git push -uf origin main", "git push -fu origin main",
                        "git push --force-with-lease origin main", "git push --force-if-includes origin main",
                        "git push origin --delete main", "git push -d origin main", "git push --mirror origin main",
                        'git push origin main "--force"', "git push origin main && git push origin \\\n  production",
                        'git push origin main && git push origin "production"',
                        "git push origin main HEAD:refs/heads/production"]:
            assert guard_in(rewrite) != "allow", f"{text!r} asks for a push, not {rewrite!r}"
    prompt("allow git push production")
    assert guard_in("git push origin production") == "allow" and guard_in(push) != "allow", "each branch its own"
    for text in ["allow git push release/1.2", "push to release/1.2", "Push to release/1.2."]:
        prompt(text)
        assert guard_in("git push origin release/1.2") == "allow", f"{text!r} names release/1.2"
        assert guard_in("git push origin release/12") != "allow", f"{text!r} keeps the dot"
    for text in ["¿pusheo a main?", "don't push to main", "no pushees a main", "pushed to main", "push to main later",
                 "> push to main", "push to main-feature", "push to main/experiment", "push to main, not now",
                 "Push to main, after I check", "Push to main. Hmm, no.", "Push to main. Don't.",
                 "Push to main failed. Look into it.", "Push to main is what broke prod."]:
        prompt("allow git push main")
        prompt(text)
        assert guard_in(push) != "allow", f"grants nothing and revokes the last one: {text!r}"


def a_first_push_to_an_empty_remote_needs_no_approval(base):
    _, guard_in = database_approval_hooks(base)

    def git(*args, cwd=base):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    empty, used, work = (os.path.join(base, n) for n in ("empty.git", "used.git", "work"))
    git("init", "-q", "--bare", empty)
    git("init", "-q", "--bare", used)
    git("init", "-q", "-b", "main", work)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x", cwd=work)
    git("push", "-q", used, "main", cwd=work)
    git("remote", "add", "origin", empty, cwd=work)
    git("remote", "add", "used", used, cwd=work)
    git("remote", "add", "dead", "git://127.0.0.1:1/x.git", cwd=work)
    other = os.path.join(base, "other.git")
    git("init", "-q", "--bare", other)
    git("push", "-q", other, "main:gh-pages", cwd=work)
    git("remote", "add", "other", other, cwd=work)
    stalled = socket.create_server(("127.0.0.1", 0))  # accepts the connection and never answers
    git("remote", "add", "stalled", f"git://127.0.0.1:{stalled.getsockname()[1]}/x.git", cwd=work)
    for command in ["git push -u origin main", "git push origin main", "git push --set-upstream origin HEAD:main",
                    f"git push {empty} main", f"cd {work} && git push -u origin main"]:
        assert guard_in(command, cwd=work if "cd " not in command else "/tmp") == "allow", \
            f"nothing to review against: {command}"
    for command in ["git push -u used main", "git push dead main", "git push nowhere main",
                    "git push origin main && git push used main", "git push origin main; git push origin develop",
                    "git push --force origin main", "git push main", "git push other main",
                    "git push origin main production", "git push -o ci.skip origin main", "git push origin main -o ci.skip",
                    "gh pr merge 1 && git push -u origin main", f"git -c remote.origin.pushurl={used} push origin main",
                    f"git remote set-url --push origin {used} && git push origin main",
                    f"GIT_DIR={used} git push origin main"]:
        assert guard_in(command, cwd=work) != "allow", f"stays denied: {command}"
    start = time.monotonic()
    assert guard_in("git push stalled main", cwd=work) != "allow", "a remote that never answers fails closed"
    assert time.monotonic() - start < 6, f"within the hook's budget: {time.monotonic() - start:.1f}s"
    stalled.close()
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        assert any('"allow-approved"' in line and '"empty-remote"' in line for line in fh), "the exemption is logged"
    for n in range(1, 6):
        git("init", "-q", "--bare", f"{empty}{n}")
        git("remote", "set-url", "--add", "--push", "origin", f"{empty}{n}", cwd=work)
        expected = "allow" if n <= 4 else "deny"
        assert (guard_in("git push origin main", cwd=work) == "allow") == (expected == "allow"), \
            f"{n} empty push URLs: {expected}"
    git("config", "--unset-all", "remote.origin.pushurl", cwd=work)
    assert guard_in("git push -u origin main", cwd="/tmp") != "allow", "outside the repository the remote is unknown"
    git("remote", "set-url", "--push", "origin", used, cwd=work)
    assert guard_in("git push origin main", cwd=work) != "allow", "the push URL decides, not the empty fetch URL"


def guard_mcp_logs_browser_mcp(base):
    log = os.path.expanduser("~/.agents/logs/hooks.jsonl")
    for tool in ("mcp__playwright-headless__browser_navigate", "mcp__claude-in-chrome__navigate", "mcp__linear__get_issue"):
        got = run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": {}, "cwd": "/tmp", "session_id": "test"})
        assert got is None, f"{tool} must never be blocked: {got}"
    logged = [json.loads(l)["detail"] for l in open(log) if '"browser-mcp"' in l and '"session": "test"' in l]
    assert logged[-2:] == ["mcp__playwright-headless__browser_navigate", "mcp__claude-in-chrome__navigate"], logged[-3:]


def a_broken_profile_blocks_mcp_calls_instead_of_letting_them_through(base):
    profile = os.path.join(base, "profiles", "work", "mcp-writes.json")
    os.makedirs(os.path.dirname(profile))
    env = {"HOME": base, "AGENTS_PROFILES_DIR": os.path.join(base, "profiles")}

    log = os.path.join(base, ".agents", "logs", "hooks.jsonl")

    def call(tool, event="PreToolUse", tool_input=None):
        return run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": tool_input or {}, "cwd": "/tmp",
                                         "session_id": "s1", "hook_event_name": event}, env=env)

    def errors():
        return [json.loads(l)["detail"] for l in open(log) if '"guard_mcp"' in l and '"error"' in l]

    def denied_because(got, *said):
        reason = got and got["hookSpecificOutput"]["permissionDecisionReason"]
        return got and got["hookSpecificOutput"]["permissionDecision"] == "deny" and all(s in reason for s in said)

    run_hook("prompt_approvals.py", {"prompt": "Crea una tarea en Linear", "session_id": "s1", "cwd": "/tmp"}, env=env)
    assert call("mcp__linear__save_issue") is None, "approved while the profile still loads"
    gateway = {"service_field": "provider", "operation_fields": ["tool"], "writes": {"tracker": ["create-issue"]}}
    for broken in ('{"direct": {"tracker": "("}}', '{"direct": ', json.dumps({"gateways": [{**gateway, "tool": "("}]}),
                   '{"gateways": [{"tool": "gateway__execute$"}]}'):
        open(profile, "w").write(broken)
        logged = len(errors())
        for tool in ("mcp__linear__save_issue", "mcp__tracker__get_issue"):
            got = call(tool)
            assert denied_because(got, "mcp-writes.json", "hooks.jsonl"), (broken, tool, got)
        assert len(errors()) == logged + 2 and "mcp-writes.json" in errors()[-1], (broken, errors()[-1:])
        assert call("mcp__playwright-headless__browser_navigate") is None, "browser calls are never blocked"
        assert call("mcp__linear__save_issue", "PostToolUse") is None, "after the call there is nothing to block"
    # Rules that load, and a call that makes judging it raise: a tool input that isn't an object.
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["tool"],
                             "writes": {"tracker": ["create-issue"]}}]}, open(profile, "w"))
    got = call("mcp__plugin_gateway__execute", tool_input="create-issue")
    assert denied_because(got, "the guard failed", "AttributeError"), got
    assert call("mcp__plugin_gateway__execute", "PostToolUse", tool_input="create-issue") is None
    assert "recording mcp__plugin_gateway__execute: AttributeError" in errors()[-1], errors()[-1:]
    done = subprocess.run(["python3", H + "guard_mcp.py"], input="not json", capture_output=True, text=True,
                          env={**os.environ, **env})
    assert done.returncode == 0 and not done.stdout, done
    turn = os.path.join(base, ".agents", "approvals", "turn", "s1")
    assert os.path.isdir(turn), "the grant is there before the next message"
    run_hook("prompt_approvals.py", {"prompt": "now fix the failing test", "session_id": "s1", "cwd": "/tmp"}, env=env)
    assert not os.path.exists(turn), "a broken profile doesn't keep the last turn's approvals"


def the_hook_log_keeps_secrets_out(base):
    log = os.path.expanduser("~/.agents/logs/hooks.jsonl")
    ghp, slack, anthropic, openai = "ghp_" + "A1b2C3d4" * 5, "xoxb-1234567890-abcdefABCDEF", "sk-ant-api03-" + "Zz9" * 10, \
        "sk-proj-" + "Qq8" * 10
    cases = [  # the command, the values it must not log, and a part of it that must stay
        ("PGPASSWORD=hunter2pw psql -h db -c 'select 1'", ["hunter2pw"], "psql -h db"),
        ("GH_TOKEN=tok3nvalue gh api user", ["tok3nvalue"], "gh api user"),
        ("export AWS_SECRET_ACCESS_KEY='wJalrXUtnFEMI/K7MDENG'", ["wJalrXUtnFEMI"], "export AWS_SECRET_ACCESS_KEY="),
        ("curl --token=abcdef123456 https://api.example.com", ["abcdef123456"], "https://api.example.com"),
        ('wp user create bob b@x.test --password "correct horse"', ["correct", "horse"], "wp user create bob"),
        ("tool --api-key k3yk3yk3y --secret=s3cr3tval run", ["k3yk3yk3y", "s3cr3tval"], " run"),
        ("mysql -uroot -pR00tpass wordpress", ["R00tpass"], "-uroot"),
        ("curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abc' https://x.test", ["eyJhbGciOiJIUzI1NiJ9"], "https://x.test"),
        ("curl -H 'Authorization: n0sch3mes3cret' https://x.test", ["n0sch3mes3cret"], "https://x.test"),
        ("curl -H 'Authorization: Negotiate dGhpcy1pcy1hLXNlY3JldA==' https://x.test", ["dGhpcy1pcy1h"], "https://x.test"),
        ("mysql -p0nepass --host=db -ptw0pass wordpress", ["0nepass", "tw0pass"], "--host=db"),
        ("git clone https://bob:pa55w0rd@example.com/r.git", ["pa55w0rd"], "example.com/r.git"),
        (f"echo {ghp} {slack} {anthropic} {openai} AKIAIOSFODNN7EXAMPLE",
         [ghp[:10], slack[:10], anthropic[:12], openai[:12], "AKIAIOSFODNN7"], "echo "),
        # The shell guard cuts the command at 200 characters: these secrets start before the cut and end after it.
        ("echo " + "a" * 186 + " " + ghp, [ghp[:6]], "a" * 50),
        ("echo " + "b" * 170 + " https://bob:pa55w0rdpa55w0rd@example.com", ["pa55w"], "b" * 50),
    ]
    for command, secrets, kept in cases:
        assert guard(f"{command}; git reset --hard") == "deny", command
        line = [l for l in open(log) if '"guard_bash"' in l and '"deny"' in l][-1]
        detail = json.loads(line)["detail"]
        for secret in secrets:
            assert secret not in line, (secret, line)
        assert "[redact" in detail and kept in detail, detail  # "[redacte" where the cut falls in it
        if len(command) < 150:
            assert "git reset --hard" in detail, ("the rule's command is kept", detail)
    for innocent in ["tar -pxf a.tar", "ls --token-file x", "grep -r KEYS docs"]:
        assert guard(f"{innocent}; git reset --hard") == "deny"
        assert innocent in json.loads([l for l in open(log) if '"deny"' in l][-1])["detail"], innocent
    # Every hook's detail is redacted, not only the shell guard's: one that logs a payload's text unredacted.
    logs = os.path.join(base, "direct-log")
    secret = "x" * 480 + " GH_TOKEN=" + "t0k3n" * 20 + " " + "y" * 300  # past the scan, the token inside it
    subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import hooklog; "
                    "hooklog.log('test', 'error', {}, sys.argv[2])", H, secret],
                   env={**os.environ, "AGENTS_LOG_DIR": logs}, check=True)
    for secret in ("x" * 495 + " ghp_" + "A1b2C3d4" * 5 + " " + "y" * 300,  # cut at 500, its start matches no pattern
                   "https://bob:" + "passphrase" * 80 + "@example.com"):  # one word longer than the scan
        subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import hooklog; "
                        "hooklog.log('test', 'error', {}, sys.argv[2])", H, secret],
                       env={**os.environ, "AGENTS_LOG_DIR": logs}, check=True)
    first, cut, word = [json.loads(l)["detail"] for l in open(os.path.join(logs, "hooks.jsonl"))]
    assert "passphrase" not in word, word[:40]
    assert first == "x" * 480 + " GH_TOKEN=[redacted]", f"redacted whole, its name kept: {first[470:]}"
    assert "ghp_" not in cut and "A1b" not in cut, cut[490:]


def a_turn_approval_that_cant_be_removed_is_moved_aside(base):
    if os.geteuid() == 0:
        return  # root ignores the read-only modes this test relies on
    env = {"HOME": base, "AGENTS_PROFILES_DIR": os.path.join(base, "profiles")}
    turns = os.path.join(base, ".agents", "approvals", "turn")

    def prompt(text):
        return subprocess.run(["python3", H + "prompt_approvals.py"], capture_output=True, text=True,
                              input=json.dumps({"prompt": text, "session_id": "s1", "cwd": "/tmp"}),
                              env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env})

    def write():
        got = run_hook("guard_mcp.py", {"tool_name": "mcp__linear__save_issue", "tool_input": {}, "cwd": "/tmp",
                                        "session_id": "s1"}, env=env)
        return got["hookSpecificOutput"]["permissionDecision"] if got else "allow"

    stuck = os.path.join(turns, "gone", "x")
    os.makedirs(stuck)
    os.utime(os.path.dirname(stuck), (time.time() - 2 * 86400,) * 2)
    os.chmod(os.path.dirname(stuck), 0o555)
    session = os.path.join(turns, "s1")
    try:
        assert prompt("Crea una tarea en Linear").returncode == 0 and write() == "allow"
        os.chmod(session, 0o555)
        done = prompt("now fix the failing test")
        assert done.returncode == 0, done
        assert not os.path.exists(session) and write() == "deny", "the last turn's approval no longer counts"
        assert [n for n in os.listdir(turns) if n.startswith("s1.")], os.listdir(turns)
        assert prompt("Crea otra tarea en Linear").returncode == 0 and write() == "allow", "this message's own count"
        logged = [json.loads(l) for l in open(os.path.join(base, ".agents", "logs", "hooks.jsonl"))]
        assert any(l["decision"] == "clear-failed" and "gone" in l["detail"] for l in logged), \
            "a long-gone session's folder that can't be removed is logged"
        os.chmod(session, 0o555)
        os.chmod(turns, 0o555)
        done = prompt("now fix the failing test")
        assert done.returncode == 2 and session in done.stderr, done
        fix = re.search(r"`(chmod [^`]+)`", done.stderr).group(1)
        assert subprocess.run(["bash", "-c", fix]).returncode == 0, fix
        assert prompt("now fix the failing test").returncode == 0 and write() == "deny", "the hint clears it"
        assert prompt("Crea otra tarea en Linear").returncode == 0 and write() == "allow"
        os.chmod(turns, 0o555)  # the approval file goes, the folder can't: nothing is left to block for
        assert prompt("now fix the failing test").returncode == 0 and write() == "deny"
        assert "clear-emptied" in open(os.path.join(base, ".agents", "logs", "hooks.jsonl")).read()
        done = prompt("Crea otra tarea en Linear")  # the folder that stayed takes this message's approvals
        assert done.returncode == 0 and write() == "allow", done.stderr
    finally:
        for folder, dirs, _ in os.walk(turns):
            os.chmod(folder, 0o755)


# --- stamps and the PR gate ---------------------------------------------------------------------
def pr_gate_review_and_validation(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    assert guard("gh pr create --fill", repo) == "deny"
    assert guard(f"cd {repo} && GH_HOST=x gh pr create --fill", repo) == "deny"
    def denial(command):
        return run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo,
                                          "session_id": "test"})["hookSpecificOutput"]["permissionDecisionReason"]
    for chained in ("python3 ~/.agents/hooks/review_stamp.py write && gh pr create --fill",
                    "python3 ~/.agents/hooks/review_stamp.py \\\n  write && gh pr create --fill"):
        said = denial(chained)
        assert "before anything in it runs" in said and "a command of its own" in said, \
            f"a stamp written in the same command can't count, and the message must say how to order it: {said}"
    assert "a command of its own" not in denial('echo "review_stamp.py write"; gh pr create --fill'), "quoted text writes nothing"
    assert guard("cd no-such-dir; gh pr create --fill", repo) == "deny", "a failed cd can't crash the guard open"
    assert guard("sh -c $'gh pr create --fill'", repo) == "deny", "sh -c $'...'"
    assert guard("""python3 -c 'print("gh pr create")'""", repo) == "allow", "phrase inside a string is not a PR"
    assert guard('grep -n "gh pr create" hooks/guard_bash.py', repo) == "allow"
    assert guard('grep -n "guard_bash\\|gh pr create" bin/docs', repo) == "allow", "a | inside a pattern starts no command"
    assert guard("cat > notes.md <<'EOF'\nRun gh pr create\n| gh pr create --fill\nEOF", repo) == "allow", "heredoc text"
    assert guard("cat > notes.md <<'EOF'\ntext\nEOF\ngh pr create --fill", repo) == "deny", "a real one after a heredoc"
    assert denial("cat > notes.md <<'EOF'\ntext\nEOF\n\n  \n\ngh pr create --fill") == \
        denial("cat > notes.md <<'EOF'\ntext\nEOF\ngh pr create --fill"), "blank lines before it change nothing"
    runs = ['GH_HOST="x" gh pr create --fill', 'echo "$(gh pr create --fill)"', "cat <<EOF\n$(gh pr create --fill)\nEOF",
            "# <<EOF\ngh pr create --fill\nEOF", "echo $((1<<2)); gh pr create --fill", 'printf \\" | gh pr create --fill',
            "echo `gh pr create --fill`", "echo \"$(printf %s ')' ; gh pr create --fill)\"",
            "cat <<\\EOF\nbody\nEOF\ngh pr create --fill", "cat <<\\EOF\nit's\nEOF\ngh pr create --fill", "echo $'it\\'s' ; gh pr create --fill", 'GH_REPO="alopezari/"agents-kit gh pr create --fill',
            "printf %s foo\\ #bar; gh pr create --fill", "x=1; arr[x<<2]=value\ngh pr create --fill",
            "(( $(gh pr create --fill) ))", "echo 'a' `gh pr create --fill`", "cat <<- EOF\n\t`gh pr create --fill`\n\tEOF",
            "cat <<EOF\nx\nEOF\ncat <<'X'\n`y`\nX\ngh pr create --fill", "echo \"a\\\"$(gh pr create --fill)\"",
            "cat <<'EOF'x\n`true`\nEOFx\ngh pr create --fill", "cat <<EOF'x'\nEOF\nEOFx\ngh pr create --fill",
            "echo \"$\\\n(gh pr create --fill)\"", "echo $\\\n(gh pr create --fill)",
            "eval 'gh pr create --fill'", "bash -lc 'gh pr create --fill'", "sh -c \"cd x && gh pr create --fill\"",
            "command gh pr create --fill", "env GH_HOST=x gh pr create --fill", "env -i PATH=/bin gh pr create --fill",
            "nohup gh pr create --fill", "time gh pr create --fill", "exec gh pr create --fill", "command -- gh pr create --fill",
            "env -u GH_TOKEN gh pr create --fill", "exec -a gh gh pr create --fill", "time -f %E gh pr create --fill",
            "exec -aSlip gh pr create --fill", "env -uGH_TOKEN gh pr create --fill", "gh pr new --fill",
            "env X=1 gh pr new --fill"]
    for command in runs:
        assert guard(command, repo) == "deny", f"the shell runs gh here: {command!r}"
    inert = ["cat <<EOF\n EOF\ngh pr create --fill\nEOF", "cat <<'END-MARK'\ngh pr create --fill\nEND-MARK",
             "echo 'a | gh pr create'", "cat <<-EOF\n\tgh pr create\n\tEOF",
             "python3 - <<'PY'\ns = '`x`|gh pr create $(y)'\nPY", "python3 - <<'PY'\ns = \"bash -lc 'gh pr create'\"\nPY",
             "grep -n 'eval `gh pr create`' notes.md","grep -n '`gh pr create`' notes.md",
             'echo "\\`gh pr create\\`"', "command -v gh pr create"]
    for command in inert:
        assert guard(command, repo) == "allow", f"nothing runs gh here: {command!r}"
    long_env = "env " + " ".join(f'V{i}="a"' for i in range(22)) + " true"
    started = child_cpu()
    assert guard(long_env, repo) == "allow" and child_cpu() - started < 3, "a long env line must not stall the guard"
    write_stamp(repo, "review")
    assert guard("gh pr create --fill", repo) == "deny", "behavior change needs validation too"
    assert guard(f"cd {repo} && ~/.agents/bin/evidence A1 gh pr create --fill", repo) == "deny", "the PR gate sees through the runner"
    assert guard("env X=1 evidence A1 gh pr create --fill", repo) == "deny"
    write_stamp(repo, "validate")
    assert guard("gh pr create --fill", repo) == "allow"
    assert guard("gh pr new --fill", repo) == "allow", "gh's alias passes the same gate"
    open(os.path.join(repo, "CHANGELOG.md"), "w").write("- A line (#12).\n")
    assert guard("gh pr create --fill", repo) == "allow", "the changelog line CI checks doesn't invalidate the stamps"
    os.makedirs(os.path.join(repo, "changelog.d"))
    open(os.path.join(repo, "changelog.d", "feat~x.md"), "w").write("- A line.\n")
    assert guard("gh pr create --fill", repo) == "allow", "nor does its own changelog.d entry"
    os.makedirs(os.path.join(repo, "hooks"))
    open(os.path.join(repo, "hooks", "changelog_check.py"), "w").write("x = 1\n")
    assert guard("gh pr create --fill", repo) == "deny", "code named after the changelog is still code"
    for kind in ("review", "validate"):
        write_stamp(repo, kind)
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    assert guard("gh pr create --fill", repo) == "deny", "edit after stamps must invalidate them"
    git(repo, "checkout", "trunk", "--", "app.py")
    for kind in ("review", "validate"):
        write_stamp(repo, kind)
    git(repo, "mv", "app.py", "CHANGELOG.txt")
    assert guard("gh pr create --fill", repo) == "deny", "code renamed to a changelog name is still code removed"
    for kind in ("verify", "verify-empty"):
        done = subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", kind], cwd=repo, capture_output=True, text=True)
        checked = subprocess.run(["python3", H + "review_stamp.py", "check", "--kind", kind], cwd=repo)
        assert done.returncode != 0 and "stop_checks.py verify" in done.stderr and checked.returncode == 1, (kind, done)


def stamps_survive_merging_the_default_branch(base):
    repo = new_repo(base)
    commit = ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam"]
    lines = [f"line_{n} = {n}\n" for n in range(1, 31)]
    open(os.path.join(repo, "app.py"), "w").write("".join(lines))
    open(os.path.join(repo, "other.py"), "w").write("z = 1\n")
    git(repo, "add", "-A")
    git(repo, *commit, "thirty lines")
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "w").write("".join(lines[:14] + ["line_15 = 'changed'\n"] + lines[15:]))
    git(repo, *commit, "the change")

    def check(kind):
        return subprocess.run(["python3", H + "review_stamp.py", "check", "--kind", kind], cwd=repo).returncode == 0

    def on_trunk(edit):
        git(repo, "switch", "-q", "trunk")
        edit()
        git(repo, *commit, "trunk moves")
        git(repo, "switch", "-q", "feature")
        merged = git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-edit", "trunk")
        assert merged.returncode == 0, merged.stderr

    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    code = f"import sys; sys.path.insert(0, {H!r}); import review_stamp as r; r.record_verify('verify', r.fingerprint())"
    subprocess.run(["python3", "-c", code], cwd=repo, check=True)
    assert check("review") and check("validate") and check("verify")

    on_trunk(lambda: open(os.path.join(repo, "other.py"), "w").write("z = 2\n"))
    assert check("review") and check("validate"), "merging trunk leaves the branch's own diff as it was"
    assert guard("gh pr create --fill", repo) == "allow", "so the gate still holds the review and validation"
    assert not check("verify"), "but verify ran on code without trunk's change: it runs again"
    on_trunk(lambda: open(os.path.join(repo, "app.py"), "w").write("".join(["line_1 = 'trunk'\n"] + lines[1:])))
    assert check("review"), "a trunk edit outside the change's context lines, in the same file"
    on_trunk(lambda: open(os.path.join(repo, "app.py"), "w").write(
        "".join(["line_1 = 'trunk'\n"] + lines[1:11] + ["line_12 = 'trunk'\n"] + lines[12:])))
    assert not check("review") and not check("validate"), "a trunk edit next to the change: what was reviewed moved"
    assert guard("gh pr create --fill", repo) == "deny"
    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    open(os.path.join(repo, "new.py"), "w").write("a = 1\n")
    assert not check("review"), "an untracked file is part of the change"
    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    open(os.path.join(repo, "new.py"), "w").write("a = 2\n")
    assert not check("review"), "and so is its content"
    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    git(repo, "add", "new.py")
    git(repo, *commit, "add new.py")
    assert check("review"), "committing a reviewed untracked file keeps the stamp"
    for before, after, why in [("x = 2\n", "x = 2 \n", "trailing whitespace"), ("x = 2\n", "x = 2\r\n", "a line ending"),
                               (b"s = '\xe9'\n", b"s = '\xe8'\n", "a byte that isn't UTF-8")]:
        mode = "wb" if isinstance(before, bytes) else "w"
        open(os.path.join(repo, "edge.py"), mode, **({} if mode == "wb" else {"newline": ""})).write(before)
        for kind in ("review", "validate"):
            assert write_stamp(repo, kind).returncode == 0, f"stamping {why}"
        assert check("review"), why
        open(os.path.join(repo, "edge.py"), mode, **({} if mode == "wb" else {"newline": ""})).write(after)
        assert not check("review"), f"changing {why} is an edit"
    open(os.path.join(repo, ".gitattributes"), "w").write("*.cfg diff=nocomments\n")
    git(repo, "config", "diff.nocomments.textconv", "sed -e s/#.*//")
    open(os.path.join(repo, "app.cfg"), "w").write("a = 1  # one\n")
    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    open(os.path.join(repo, "app.cfg"), "w").write("a = 1  # two\n")
    assert not check("review"), "a diff text converter can't hide an edit"
    for name, kind in [(n, k) for n in ("año.py", 'a"b.py', "tab\tname.py", "CHANGELOG.md\n") for k in ("review", "validate", "verify")]:
        open(os.path.join(repo, name), "w").write(f"{kind} = 1\n")
        if kind == "verify":
            subprocess.run(["python3", "-c", code], cwd=repo, check=True)
        else:
            assert write_stamp(repo, kind).returncode == 0
        open(os.path.join(repo, name), "w").write(f"{kind} = 2\n")
        assert not check(kind), f"an edit to {name!r}, a name git quotes, invalidates the {kind} stamp"
    for kind in ("review", "validate"):
        assert write_stamp(repo, kind).returncode == 0
    index = os.path.join(repo, ".git", "index")
    saved = open(index, "rb").read()
    open(index, "wb").write(b"not an index")
    assert not check("review") and not check("verify"), "a git failure isn't an empty change"
    phase = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "--refresh"], cwd=repo, capture_output=True,
                           text=True)
    assert phase.stdout.startswith("stamps: git"), ("the status line says it failed", phase)
    open(index, "wb").write(saved)
    assert check("review")


def validate_stamp_needs_evidence(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/evidence")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    reports = os.path.expanduser("~/.agents/bin/reports")
    report = subprocess.run([reports, "path", "validation"], cwd=repo, capture_output=True, text=True).stdout.strip()
    evidence = subprocess.run([reports, "path", "evidence"], cwd=repo, capture_output=True, text=True).stdout.strip()
    assert os.path.isdir(evidence) and os.path.dirname(evidence) == os.path.dirname(report), (report, evidence)
    assert os.path.basename(evidence) == os.path.basename(report).replace("validation-", "evidence-")[:-3], evidence

    ran = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "A1", "python3", "-c", "print('seen it'); exit(3)"],
                         cwd=repo, capture_output=True, text=True)
    with open(os.path.join(evidence, "A1.txt")) as fh:
        saved = fh.read()
    assert ran.returncode == 3 and "seen it" in ran.stdout, ran
    assert "python3 -c" in saved and "exit(3)" in saved and repo in saved and "exit 3" in saved and "seen it" in saved, saved
    assert subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "A1"], cwd=repo, capture_output=True).returncode == 64
    os.chmod(evidence, 0o500)
    try:
        unsaved = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "A9", "true"], cwd=repo, capture_output=True, text=True)
    finally:
        os.chmod(evidence, 0o700)
    assert unsaved.returncode == 74 and "A9.txt" in unsaved.stderr, "evidence that couldn't be saved isn't a pass: " + unsaved.stderr
    slow = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "A8", "bash", "-c", "echo first; sleep 1; echo last"],
                          cwd=repo, capture_output=True, text=True)
    with open(os.path.join(evidence, "A8.txt")) as fh:
        assert "last" in fh.read(), "the saved output is complete"
    elsewhere = os.path.join(base, "guide-evidence")
    os.makedirs(elsewhere)
    outside = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "M1", "echo", "from the guide"], cwd=base,
                             capture_output=True, text=True, env={**os.environ, "EVIDENCE_DIR": elsewhere})
    assert outside.returncode == 0 and "from the guide" in open(os.path.join(elsewhere, "M1.txt")).read(), outside.stderr
    unset_dir = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "M2", "true"], cwd=base, capture_output=True, text=True,
                               env={**os.environ, "EVIDENCE_DIR": ""})
    assert unset_dir.returncode == 74 and "EVIDENCE_DIR" in unset_dir.stderr, "an empty EVIDENCE_DIR means step 0 failed"
    escaped = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "../out", "true"], cwd=repo, capture_output=True)
    assert escaped.returncode == 64 and not os.path.exists(os.path.join(os.path.dirname(evidence), "out.txt")), "an id is a file name"

    def write_validate():
        return subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", "validate"], cwd=repo, capture_output=True, text=True)

    def stamped():
        return subprocess.run(["python3", H + "review_stamp.py", "check", "--kind", "validate"], cwd=repo).returncode == 0

    done = write_validate()
    assert done.returncode == 2 and "validation report" in done.stderr and not stamped(), done
    with open(os.path.join(evidence, "empty.png"), "w"):
        pass
    with open(report, "w") as fh:
        fh.write("A. CLI\n\n| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                 "| A1 | exits 3 | − | PASS | [output](evidence-x/A1.txt) |\n"
                 "| A2 | prints | + | PASS | printed it, rc=0 |\n"
                 "| A3 | staging only | + | NOT RUN | needs staging |\n\n"
                 "B. UI\n\n| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                 "| B1 | page | + | FAIL | ![page](empty.png) |\n"
                 "| B2 | named inside another name | + | PASS | xA1.txt |\n"
                 "| B3 | a \\| b in the check | + | PASS | rc=0 |\n"
                 "| B4 | bold result | + | **PASS** | rc=0 |\n"
                 "| B5 | empty evidence | + | PASS ||\n"
                 "| B6 | no evidence cell | + | PASS |\n\n"
                 "D. Bold header\n\n| # | Check | Case | **Result** | **Evidence** |\n|---|---|---|---|---|\n| D1 | x | + | PASS | rc=0 |\n\n"
                 "C. No outer pipes\n\n# | Check | Case | Result | Evidence\n---|---|---|---|---\nC1 | x | + | PASS | rc=0\n")
    done = write_validate()
    named = {row for row in ("A1", "A2", "A3", "B1", "B2", "B3", "B4", "B5", "B6", "C1", "D1") if f"{row}:" in done.stderr}
    assert done.returncode == 2 and named == {"A2", "B1", "B2", "B3", "B4", "B5", "B6", "C1", "D1"} and not stamped(), done.stderr
    with open(os.path.join(evidence, "b1.png"), "wb") as fh:
        fh.write(b"\x89PNG")
    with open(report, "w") as fh:
        fh.write("| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                 "| A1 | exits 3 | − | PASS | `A1.txt` |\n| A3 | staging only | + | NOT RUN | needs staging |\n"
                 "| B1 | page | + | FAIL | ![page](evidence-x/b1.png) |\n"
                 f"| B2 | linked by absolute path, as validate writes it | + | PASS | [output]({evidence}/A1.txt) |\n")
    done = write_validate()
    assert done.returncode == 0 and stamped(), done.stderr
    listed = subprocess.run([reports], cwd=repo, capture_output=True, text=True).stdout
    assert "A1.txt" in listed and "b1.png" in listed and "\x89PNG" not in listed, listed


def unrun_checks_never_hold_the_pr(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/checks")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    for kind in ("review", "validate"):
        write_stamp(repo, kind)
    guide = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "path", "checks-guide"], cwd=repo,
                           capture_output=True, text=True).stdout.strip()
    with open(guide, "w") as fh:
        fh.write("# Guide\n## Before the merge (optional)\n### M1. Try it on a phone\n## After the merge\n### P1. Backfill\n")

    def decision(command):
        return "deny" if run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": "test"}) else "allow"

    for command in ["gh pr create --fill", "gh pr ready", "gh pr ready 12", "gh pr ready 12 --repo o/r"]:
        assert decision(command) == "allow", f"the user decides on unrun checks: {command}"
    with open(os.path.join(os.path.dirname(guide), os.path.basename(guide).replace("checks-guide-", "staging-guide-")), "w") as fh:
        fh.write("# Staging guide\n## Before the merge\n### S1. Deploy\n## Results (2026-10-01)\n\n"
                 "| Step | Result | Evidence |\n|---|---|---|\n| S1 | FAIL | `S1.txt` |\n")
    os.remove(guide)
    for command in ["gh pr create --fill", "gh pr ready 12"]:
        assert decision(command) == "allow", f"nor does an older guide's failed staging step: {command}"
    for args in (["staging"], ["write", "--kind", "staging"], ["check", "--kind", "staging"], ["write", "--kind", "reveiw"]):
        done = subprocess.run(["python3", H + "review_stamp.py", *args], cwd=repo, capture_output=True, text=True)
        assert done.returncode == 2 and "Unknown" in done.stderr, f"a removed or mistyped stamp is refused: {args} {done.stderr}"
    open(os.path.join(repo, "app.py"), "a").write("y = 3\n")
    assert decision("gh pr create --fill") == "deny", "the review gate still holds an unreviewed change"


def guard_fails_closed(base):
    # Every harness lets a command through when its hook crashes or times out, so the guard must answer deny itself.
    def reason(command, cwd, env=None):
        d = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": "test"}, env=env)
        return d["hookSpecificOutput"]["permissionDecisionReason"] if d else None

    gone = os.path.join(base, "gone")
    assert "The guard failed (FileNotFoundError" in (reason("gh pr create --fill", gone) or ""), "a crash denies"
    repo = new_repo(base)
    fake = os.path.join(base, "fake-git")
    os.makedirs(fake)
    with open(os.path.join(fake, "git"), "w") as fh:
        fh.write("#!/bin/sh\nsleep 30\n")
    os.chmod(os.path.join(fake, "git"), 0o755)
    started = time.time()
    hung = reason("gh pr create --head feat/x --fill", repo, env={"PATH": fake + ":" + os.environ["PATH"]})
    assert "The guard failed (TimeoutExpired" in (hung or "") and time.time() - started < 10, (hung, time.time() - started)
    with open(os.path.join(fake, "git"), "w") as fh:
        fh.write("#!/bin/sh\nsleep 5\n")  # each call within a per-call limit, together past the hook's 10 s
    started = time.time()
    slow = reason("gh pr create --head feat/x --fill", repo, env={"PATH": fake + ":" + os.environ["PATH"]})
    assert "TimeoutExpired" in (slow or "") and time.time() - started < 10, (slow, time.time() - started)
    assert reason("ls", gone) is None, "a command no check needs git for still runs"


def pr_gate_follows_worktrees(base):
    main = new_repo(base, "main")
    wt = os.path.join(base, "wt")
    git(main, "switch", "-q", "-c", "other")
    git(main, "worktree", "add", "-q", "-b", "feat/x", wt, "trunk")
    open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
    for kind in ("review", "validate"):
        write_stamp(wt, kind)
    assert guard(f"cd {wt} && gh pr create --fill", main) == "allow"
    assert guard("gh pr create --head feat/x --fill", main) == "allow"
    open(os.path.join(main, "app.py"), "a").write("z = 3\n")
    assert guard("gh pr new --fill", main) == "deny"
    assert guard("gh pr new --head feat/x --fill", main) == "allow", "gh's alias acts on the --head worktree too"


def codex_pr_commands_name_their_checkout(base):
    # Codex runs a command in its per-call workdir but sends the hook the session's cwd (openai/codex#33986): a PR
    # opened from an unreviewed checkout would be judged by the reviewed one the session started in.
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    for kind in ("review", "validate"):
        write_stamp(repo, kind)

    def codex(command):
        d = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": "test",
                                       "turn_id": "t1"}, env={"AGENTS_HARNESS": ""})
        return d["hookSpecificOutput"]["permissionDecisionReason"] if d else "allow"

    assert "33986" in codex("gh pr create --fill"), "the checkout it runs in is unknown"
    assert "33986" in codex("gh pr edit 5 --title x")
    assert "33986" in codex("command gh pr create --fill"), "a wrapper doesn't hide it"
    assert "33986" in codex("gh pr -R o/r create --fill"), "nor gh's --repo flag"
    assert codex("gh pr ready 12") == "allow", "marking ready reads no checkout, so it needs no cd"
    # Each of these leaves the PR in the unknown workdir, or somewhere the gate would read differently.
    for command in ["cd sub && gh pr create --fill", f"cd {repo}; gh pr create --fill", f"false && cd {repo}; gh pr create --fill",
                    f"cd {repo} | gh pr create --fill", f"cd {repo} && cd - && gh pr create --fill", f"  cd {repo} && gh pr create --fill",
                    "cd /$TARGET && gh pr create --fill", f'cd "~/x" && gh pr create --fill', f"cd {repo} && sh -c 'cd /tmp && gh pr create'",
                    f'echo "$(true)"; echo "; cd {repo}"; gh pr create --fill', f'echo "; cd {repo}"; gh pr create --fill',
                    f"cd {repo} && true & gh pr create --fill", f"cd {repo}/missing && true; gh pr create --fill",
                    f"cd {os.path.dirname(repo)}/rep? && gh pr create --fill", f"cd\n{repo} && gh pr create --fill",
                    "env GH_HOST=github.com gh pr create --fill", "if true; then gh pr create --fill; fi", "{ gh pr edit 5 --title x; }",
                    f"cd {repo} && {{ cd /tmp; gh pr create --fill; }}", f"cd {repo} && command cd /tmp && gh pr create --fill"]:
        assert "33986" in codex(command), f"the PR's checkout isn't the one checked: {command!r}"
    assert "33986" in codex("gh pr new --fill"), "gh's alias for create"
    for command in ["gh pr create --help", "gh pr new -h"]:
        assert codex(command) == "allow", f"help opens no PR: {command!r}"
    assert codex(f"cd {repo} && gh pr create --fill") == "allow"
    assert codex(f'cd "{repo}" && git push && gh pr create --fill') == "allow"
    assert codex("gh pr view 5") == "allow", "only the commands the PR checks judge"
    bracketed = os.path.join(base, "repo[1]")
    shutil.copytree(repo, bracketed)
    assert codex(f"cd '{bracketed}' && gh pr view 5 && gh pr create --fill") == "allow", "quoted, a [ is only a character"
    for command in [f"cd {repo} && gh pr create --head fix/cd --fill", f"cd {repo} && gh pr create --body-file /tmp/cd.md",
                    f"cd {repo} && gh pr create --fill 2>&1"]:
        assert codex(command) == "allow", f"nothing here runs the PR elsewhere: {command!r}"
    assert guard("gh pr create --fill", repo) == "allow", "Claude Code's cwd is where the command runs"


# --- stop checks --------------------------------------------------------------------------------
def reports_survive_worktree_removal(base):
    main = new_repo(base, "main")
    wt = os.path.join(base, "wt")
    git(main, "worktree", "add", "-q", "-b", "feat/z", wt, "trunk")
    open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "change")
    subprocess.run(["python3", H + "review_stamp.py", "write"], cwd=wt, capture_output=True)
    reports = os.path.expanduser("~/.agents/bin/reports")
    guide = subprocess.run([reports, "path", "checks-guide"], cwd=wt, capture_output=True, text=True).stdout.strip()
    open(guide, "w").write("# Checks guide\n")
    git(main, "worktree", "remove", wt)
    git(main, "switch", "-q", "feat/z")
    shown = subprocess.run([reports], cwd=main, capture_output=True, text=True).stdout
    assert "# Checks guide" in shown, f"guide written at {guide} is gone after removing the worktree"
    stamped = subprocess.run(["python3", H + "review_stamp.py", "check"], cwd=main).returncode == 0
    assert stamped, "the review stamp must survive removing the worktree so the PR can be opened from the main checkout"


def reports_brief_gives_one_line_per_report(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/brief")
    reports = os.path.expanduser("~/.agents/bin/reports")

    def path(kind):
        return subprocess.run([reports, "path", kind], cwd=repo, capture_output=True, text=True).stdout.strip()
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/kit-spec/path.sh")], cwd=repo, capture_output=True, text=True).stdout.strip()
    open(spec, "w").write("# Round prices once\n\nGoal: totals drift.\n" + "x\n" * 200)
    open(path("review"), "w").write("Self-review (risk: standard)\n" + "detail\n" * 200 + "Open:     the 0.005 case\n")
    open(path("validation"), "w").write("| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                                         "| A1 | a PASS | + | **PASS** | e |\n| A2 | b | - | FAIL | e |\n|A3|c|+|NOT RUN|-|\n")
    open(path("follow-pr"), "w").write("## Run 1\nStatus:  waiting on CI\n## Run 2\nStatus:  ready to merge\n")
    open(path("pr-body"), "w").write("Totals now round once.\n")
    shown = subprocess.run([reports, "brief"], cwd=repo, capture_output=True, text=True).stdout
    for line in ("Round prices once", "Self-review (risk: standard)", "Open:     the 0.005 case", "1 PASS, 1 FAIL, 1 NOT RUN",
                 "Status:  ready to merge", path("review")):
        assert line in shown, (line, shown)
    assert "detail" not in shown and "waiting on CI" not in shown and len(shown.splitlines()) < 20, shown
    listed = subprocess.run([reports], cwd=repo, capture_output=True, text=True).stdout
    assert "Self-review (risk: standard)" in listed, listed
    assert all("pr-body" not in out and "Totals now round once" not in out for out in (shown, listed)), \
        "the PR description isn't a report"
    open(path("review"), "w").write("Self-review\n")
    open(path("follow-pr"), "w").write("## Run 1\n")
    done = subprocess.run([reports, "brief"], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0 and "no Status line yet" in done.stdout and "1 PASS, 1 FAIL, 1 NOT RUN" in done.stdout, done


def reports_reads_another_branch(base):
    main = new_repo(base, "main")
    reports = os.path.expanduser("~/.agents/bin/reports")

    def run(*args, cwd=main):
        return subprocess.run([reports, *args], cwd=cwd, capture_output=True, text=True)
    git(main, "switch", "-q", "-c", "feat/ship")
    on_branch = {kind: run("path", kind).stdout.strip() for kind in ("checks-guide", "evidence", "pr-body")}
    assert os.path.basename(on_branch["pr-body"]) == "pr-body-main-feat~ship.md", on_branch
    open(on_branch["checks-guide"], "w").write("# Checks after the merge\n")
    open(os.path.join(on_branch["evidence"], "P1.txt"), "w").write("loaded\n")
    git(main, "switch", "-q", "trunk")
    wt = os.path.join(base, "wt")
    git(main, "worktree", "add", "-q", "-b", "other", wt, "trunk")
    assert run("path", "pr-body", cwd=wt).stdout.strip() == on_branch["pr-body"].replace("feat~ship", "other"), \
        "each branch has its own PR description"
    git(wt, "switch", "-q", "--detach")
    detached = run("path", "pr-body", cwd=wt)
    assert detached.returncode == 2 and "needs a branch" in detached.stderr and not detached.stdout, \
        f"detached checkouts would share one description: {detached}"
    assert run("--branch", "feat/ship", "path", "pr-body", cwd=wt).stdout.strip() == on_branch["pr-body"]
    git(wt, "switch", "-q", "other")
    for cwd in (main, wt):
        for kind, path in on_branch.items():
            got = run("--branch", "feat/ship", "path", kind, cwd=cwd)
            assert got.returncode == 0 and got.stdout.strip() == path, (cwd, kind, got)
        shown = run("--branch", "feat/ship", cwd=cwd).stdout
        assert "# Checks after the merge" in shown and "P1.txt" in shown, shown
        brief = run("--branch", "feat/ship", "brief", cwd=cwd)
        assert brief.returncode == 0 and on_branch["checks-guide"] in brief.stdout and "phase:" not in brief.stdout \
            and "bin/reports --branch feat/ship" in brief.stdout, brief
    git(main, "branch", "feat/it's")
    hint = run("--branch", "feat/it's", "brief").stdout
    assert "--branch feat/it\\'s" in hint, hint
    assert "# Checks after the merge" not in run().stdout, "without --branch it reads the current branch"
    assert run("--branch").returncode == 2, "--branch needs a name"


def stop_suggests_a_fresh_session_once_the_pr_is_open(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/big")
    phase = os.path.join(repo, ".git", "agents", "phase", "feat~big.json")
    os.makedirs(os.path.dirname(phase))

    def transcript(name, context):
        path = os.path.join(base, name)
        usage = {"input_tokens": 10, "cache_read_input_tokens": context - 1010, "cache_creation_input_tokens": 1000, "output_tokens": 5}
        with open(path, "w") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {**usage, "cache_read_input_tokens": 1}}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "isSidechain": True, "message": {"usage": {"input_tokens": 1}}}) + "\n")
        return path

    def stop_with(session, label, context):
        json.dump({"label": label}, open(phase, "w"))
        return run_hook("stop_checks.py", {"session_id": RUN + session, "cwd": repo,
                                           "transcript_path": transcript(session + ".jsonl", context)})
    first = stop_with("big", "PR open", 300_000)
    assert first and "300K" in first.get("systemMessage", "") and "bin/reports brief" in first["systemMessage"] \
        and "decision" not in first, first
    assert stop_with("big", "PR open", 310_000) is None, "once per session and branch"
    assert stop_with("small", "PR open", 100_000) is None, "a small context needs no new session"
    assert stop_with("early", "review", 300_000) is None, "mid-change a new session would re-read everything"
    assert stop_with("edge", "PR open", 150_000) is None, "over 150K, not at it"
    assert stop_with("below-window", "PR open", 160_000), "compaction at a 200K window comes before any higher threshold"
    merged = stop_with("merged", "merged", 300_000)
    assert merged and "is merged" in merged["systemMessage"], merged
    assert stop_with("ship", "ship", 300_000) is None, "ship is the step that opens the PR, not a merged branch"


def stop_suggests_a_fresh_session_for_the_next_change(base):
    repo = new_repo(base)

    def stop_with(session, context, edited=(), transcript=True):
        path = os.path.join(base, session + ".jsonl") if transcript else None
        usage = {"input_tokens": 10, "cache_read_input_tokens": context - 10, "cache_creation_input_tokens": 0, "output_tokens": 5}
        if path:
            open(path, "w").write(json.dumps({"type": "assistant", "message": {"usage": usage}}) + "\n")
        for f in edited:
            run_hook("post_edit.py", {"session_id": RUN + session, "cwd": repo, "tool_input": {"file_path": f}})
        return run_hook("stop_checks.py", {"session_id": RUN + session, "cwd": repo, "transcript_path": path}) or {}
    assert not stop_with("nc", 200_000), "the default branch is no change"
    git(repo, "switch", "-q", "-c", "feat/a")
    assert not stop_with("nc", 200_000), "the session's first change carries no earlier one"
    git(repo, "branch", "-m", "feat/a", "feat/a2")
    assert not stop_with("nc", 200_000), "a renamed branch is the same change"
    git(repo, "branch", "-c", "feat/a2", "feat/copy")
    git(repo, "switch", "-q", "feat/copy")
    assert "feat/copy" in stop_with("nc", 200_000).get("systemMessage", ""), "a copy carries the reflog, not the change"
    git(repo, "switch", "-q", "feat/a2")
    git(repo, "switch", "-q", "-c", "feat/b")
    moved = stop_with("nc", 200_000).get("systemMessage", "")
    assert "200K" in moved and "feat/b" in moved and "bin/reports brief" in moved, moved
    logged = [json.loads(l)["detail"] for l in open(os.path.expanduser("~/.agents/logs/hooks.jsonl"))
              if '"new-change"' in l and RUN + "nc" in l]
    assert logged == ["feat/copy 200K", "feat/b 200K"], logged
    assert not stop_with("nc", 300_000), "once per session and branch"
    wt = os.path.join(base, "wt")
    git(repo, "worktree", "add", "-q", "-b", "feat/c", wt, "trunk")
    open(os.path.join(wt, "notes.txt"), "w").write("x\n")
    in_worktree = stop_with("nc", 200_000, [os.path.join(wt, "notes.txt")]).get("systemMessage", "")
    assert "feat/c" in in_worktree, f"a change started in a worktree of its own counts too: {in_worktree}"
    git(repo, "switch", "-q", "feat/a2")
    assert not stop_with("nc-small", 200_000)
    git(repo, "switch", "-q", "feat/b")
    assert not stop_with("nc-small", 150_000), "over 150K, not at it"
    git(repo, "switch", "-q", "feat/a2")
    assert not stop_with("nc-codex", 200_000, transcript=False)
    git(repo, "switch", "-q", "feat/b")
    assert not stop_with("nc-codex", 200_000, transcript=False), "no transcript (Codex): no context to measure"
    git(repo, "switch", "-q", "-c", "feat/d")
    phase = os.path.join(repo, ".git", "agents", "phase", "feat~d.json")
    os.makedirs(os.path.dirname(phase), exist_ok=True)
    json.dump({"label": "PR open"}, open(phase, "w"))
    both = stop_with("nc", 300_000).get("systemMessage", "")
    assert "PR is open" in both and "moved on" not in both, f"one message when both apply: {both}"
    logged = [l for l in open(os.path.expanduser("~/.agents/logs/hooks.jsonl")) if '"new-change"' in l and RUN + "nc" in l]
    assert len(logged) == 3, f"copy, b and c; only the notice shown is logged: {logged}"
    git(repo, "switch", "-q", "feat/a2")
    state = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", RUN + "nc-broken.branches")
    open(state, "w").write('torn line\n[1]\n["a", 2]\n')
    git(repo, "switch", "-q", "feat/a2")
    stop_with("nc-broken", 200_000)
    assert "feat/a2" in open(state).read(), "a torn state file is history lost, not a crash"


def overlay_found_from_worktree_with_another_name(base):
    main = new_repo(base, "zz-agents-overlay-repo")
    wt = os.path.join(base, "zz-agents-overlay-repo-worktree-session-xyz")
    git(main, "worktree", "add", "-q", "-b", "session/xyz", wt, "trunk")
    name = subprocess.run([os.path.expanduser("~/.agents/bin/repo-name")], cwd=wt, capture_output=True, text=True).stdout.strip()
    assert name == "zz-agents-overlay-repo", name
    vdir = os.path.expanduser("~/.agents/repos/zz-agents-overlay-repo")
    os.makedirs(vdir, exist_ok=True)
    try:
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\necho ran: overlay verify\n")
        os.chmod(os.path.join(vdir, "verify"), 0o755)
        open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
        stop(RUN + "s11", wt, [os.path.join(wt, "app.py")])
        report = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "path", "verify"], cwd=wt,
                                capture_output=True, text=True).stdout.strip()
        assert "ran: overlay verify" in open(report).read(), "the repo's overlay must run from a worktree named differently"
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def overlay_edited_while_it_runs_still_runs_as_it_started(base):
    """bash reads a script as it runs it: another session editing the overlay mid-run made it fail half-way."""
    repo = new_repo(base, "zz-agents-overlay-edited")
    vdir = os.path.expanduser("~/.agents/repos/zz-agents-overlay-edited")
    os.makedirs(vdir, exist_ok=True)
    overlay, started = os.path.join(vdir, "verify"), os.path.join(base, "started")
    script = f"#!/bin/bash\ntouch {started}\nsleep 2\necho ran: overlay verify\nexit 0\n"

    def edit_once_started():
        for _ in range(200):
            if os.path.exists(started):
                open(overlay, "w").write(script.replace("exit 0", "exit 7"))  # same length: bash reads on from here
                return
            time.sleep(0.05)
    try:
        open(overlay, "w").write(script)
        os.chmod(overlay, 0o755)
        open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
        editor = threading.Thread(target=edit_once_started)
        editor.start()
        result = stop(RUN + "s12", repo, [os.path.join(repo, "app.py")])
        editor.join()
        assert os.path.exists(started) and "exit 7" in open(overlay).read(), "the overlay was edited mid-run"
        assert "failed" not in result.get("reason", ""), result
        open(overlay, "w").write("#!/bin/bash\nif then\n")
        open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
        reason = stop(RUN + "s13", repo, [os.path.join(repo, "app.py")]).get("reason", "")
        assert f"{overlay}: line 2" in reason and ".verify-run-" not in reason, "errors name the overlay, not its copy"
        assert not [f for f in os.listdir(vdir) if f.startswith(".verify-run-")], "the copy is gone"
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def stop_catches_leftovers_in_worktree(base):
    main = new_repo(base, "main")
    wt = os.path.join(base, "wt")
    git(main, "worktree", "add", "-q", "-b", "feat/y", wt, "trunk")
    os.makedirs(os.path.join(wt, "tests"))
    open(os.path.join(wt, "app.py"), "a").write("breakpoint()\n")
    open(os.path.join(wt, "tests", "test_a.py"), "w").write("import pytest\n@pytest.mark.skip\ndef test_a(): pass\n")
    reason = stop(RUN + "s1", main, [os.path.join(wt, "app.py"), os.path.join(wt, "tests", "test_a.py")]).get("reason", "")
    assert "Debug leftover" in reason and "Skipped or focused test" in reason, reason


def profiles_with_after_turn(base, scripts):
    profiles = os.path.join(base, "profiles")
    for name, script in scripts.items():
        os.makedirs(os.path.join(profiles, name))
        path = os.path.join(profiles, name, "after-turn")
        with open(path, "w") as fh:
            fh.write(script)
        os.chmod(path, 0o755)
    return {"AGENTS_PROFILES_DIR": profiles, "AGENTS_LOG_DIR": os.path.join(base, "logs")}


def wait_for(path, seconds=5):
    deadline = time.time() + seconds
    while not os.path.exists(path) and time.time() < deadline:
        time.sleep(0.05)
    return os.path.exists(path)


SAVE_STDIN = '#!/bin/sh\ncat > "$(dirname "$0")/got.json"\n'


def stop_starts_each_profile_after_turn(base):
    env = profiles_with_after_turn(base, {"one": SAVE_STDIN, "two": SAVE_STDIN})
    os.makedirs(os.path.join(env["AGENTS_PROFILES_DIR"], "none"))
    got = [os.path.join(env["AGENTS_PROFILES_DIR"], name, "got.json") for name in ("one", "two")]
    repo = new_repo(base)
    run_hook("stop_checks.py", {"session_id": RUN + "at1", "cwd": repo, "transcript_path": "/t.jsonl"}, env=env)
    assert all(wait_for(path) for path in got), "a turn without edits starts every profile's after-turn"
    payload = json.load(open(got[0]))
    assert payload["session_id"] == RUN + "at1" and payload["transcript_path"] == "/t.jsonl", payload
    for path in got:
        os.remove(path)
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    run_hook("post_edit.py", {"session_id": RUN + "at1", "cwd": repo, "tool_input": {"file_path": os.path.join(repo, "app.py")}}, env=env)
    run_hook("stop_checks.py", {"session_id": RUN + "at1", "cwd": repo, "stop_hook_active": True}, env=env)
    assert all(wait_for(path) for path in got), "a turn with edits, an automatic continuation, starts them too"
    for path in got:
        os.remove(path)
    subprocess.run(["python3", H + "stop_checks.py", "verify"], cwd=repo, capture_output=True,
                   env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env})
    assert not any(wait_for(path, 1) for path in got), "verify is not a turn"
    for flag in ("1", ""):
        run_hook("stop_checks.py", {"session_id": RUN + "at1", "cwd": repo}, env={**env, "AGENTS_AFTER_TURN": flag})
        assert not any(wait_for(path, 1) for path in got), f"a session an after-turn started doesn't start another ({flag!r})"
    assert not os.path.exists(env["AGENTS_LOG_DIR"]), "nothing logged when every after-turn starts"


def stop_never_waits_for_profile_after_turn(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("breakpoint()\n")
    payload = {"cwd": repo, "padding": "x" * 300_000}

    def timed_stop(session, env):
        run_hook("post_edit.py", {"session_id": session, "cwd": repo, "tool_input": {"file_path": os.path.join(repo, "app.py")}}, env=env)
        started = time.time()
        decision = run_hook("stop_checks.py", {**payload, "session_id": session}, env=env)
        return decision, time.time() - started
    without, plain_secs = timed_stop(RUN + "at2a", {"AGENTS_PROFILES_DIR": os.path.join(base, "no-profiles")})
    # The after-turn outlasts a stop hook slowed by a loaded machine (4.9 s once), so "done" only exists if the hook waited,
    # and a hook that waited takes 15 s more: two stops on a busy machine differed by 0.9 s, so the margin is 5 s.
    env = profiles_with_after_turn(base, {"slow": '#!/bin/sh\nyes noise | head -c 2000000\nsleep 15\n'
                                                    'touch "$(dirname "$0")/done"\nexit 1\n'})
    done = os.path.join(env["AGENTS_PROFILES_DIR"], "slow", "done")
    decision, secs = timed_stop(RUN + "at2b", env)
    assert decision == without and "Debug leftover" in decision.get("reason", ""), (decision, without)
    assert secs < plain_secs + 5 and not os.path.exists(done), f"the stop hook waited: {secs:.2f}s vs {plain_secs:.2f}s"
    assert wait_for(done, 25), "the after-turn keeps running after the stop hook returns"


def stop_logs_an_after_turn_that_cannot_start(base):
    env = profiles_with_after_turn(base, {"bad-shebang": "#!/nonexistent/interpreter\n", "works": SAVE_STDIN,
                                          "not-executable": SAVE_STDIN})
    os.chmod(os.path.join(env["AGENTS_PROFILES_DIR"], "not-executable", "after-turn"), 0o644)
    repo = new_repo(base)
    run_hook("stop_checks.py", {"session_id": RUN + "at3", "cwd": repo}, env=env)
    assert wait_for(os.path.join(env["AGENTS_PROFILES_DIR"], "works", "got.json")), "one broken profile doesn't stop the others"
    logged = [json.loads(line) for line in open(os.path.join(env["AGENTS_LOG_DIR"], "hooks.jsonl"))]
    failed = sorted(entry["detail"].split(":")[0] for entry in logged if entry["decision"] == "profile-after-turn-failed")
    assert failed == ["bad-shebang", "not-executable"], logged
    no_room = subprocess.run(["python3", "-c", f"import sys; sys.path.insert(0, {H!r}); import stop_checks as s\n"
                              "def full(*args, **kwargs): raise OSError(28, 'No space left on device')\n"
                              f"s.tempfile.TemporaryFile = full; s.start_profile_after_turns({{'session_id': '{RUN}at3b'}})"],
                             capture_output=True, text=True, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env})
    logged = [json.loads(line) for line in open(os.path.join(env["AGENTS_LOG_DIR"], "hooks.jsonl"))]
    assert no_room.returncode == 0 and any(e["session"] == RUN + "at3b" and e["detail"].startswith("works:") for e in logged), \
        f"no room for the payload file is a failed start, not a crashed hook: {no_room.stderr[-300:]}"


def stop_checks_edits_after_its_directory_is_removed(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    session_dir = os.path.join(base, "session-worktree")
    os.makedirs(session_dir)
    transcript = os.path.join(base, "s21.jsonl")
    open(transcript, "w").write("")
    open(os.path.join(repo, "app.py"), "a").write("breakpoint()\n")
    run_hook("post_edit.py", {"session_id": RUN + "s21", "cwd": session_dir, "tool_input": {"file_path": os.path.join(repo, "app.py")}})
    payload = json.dumps({"session_id": RUN + "s21", "cwd": session_dir, "transcript_path": transcript})
    # The harness starts the hook in the session's directory, already gone.
    out = subprocess.run(["bash", "-c", f'cd "$1" && rmdir "$1" && exec python3 {H}stop_checks.py', "-", session_dir],
                         input=payload, capture_output=True, text=True,
                         env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE}).stdout
    reason = (json.loads(out) if out.strip() else {}).get("reason", "")
    assert "Debug leftover" in reason, "another session removed this one's worktree; what it edited elsewhere is still checked"
    os.makedirs(session_dir)
    midway = subprocess.run(["python3", "-c", f"import os, sys; sys.path.insert(0, {H!r}); import stop_checks as s; os.chdir({session_dir!r}); "
                             f"print(s.in_checkout({repo!r}, lambda: os.rmdir({session_dir!r}) or 'checked'))"],
                            capture_output=True, text=True, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
    assert midway.stdout.strip() == "checked", f"removed while the hook was in another checkout: {midway.stderr[-300:]}"


def stop_skips_a_checkout_removed_while_checked(base):
    # A worktree removed at the end of a session while its verify ran crashed the hook with FileNotFoundError.
    main = new_repo(base, "zz-agents-vanishing")
    git(main, "switch", "-q", "-c", "feature")
    wt = os.path.join(base, "zz-agents-vanishing-wt")
    git(main, "worktree", "add", "-q", "-b", "session/gone", wt, "trunk")
    vdir = os.path.expanduser("~/.agents/repos/zz-agents-vanishing")
    os.makedirs(vdir, exist_ok=True)
    try:
        open(os.path.join(vdir, "verify"), "w").write(
            f'#!/bin/sh\n[ "$(pwd -P)" = "{os.path.realpath(wt)}" ] || exit 0\ngit -C "{main}" worktree remove --force "$PWD"\n'
            'exit 1\n')
        os.chmod(os.path.join(vdir, "verify"), 0o755)
        open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
        open(os.path.join(main, "app.py"), "a").write("breakpoint()\n")
        session = RUN + "s22"
        for path in (os.path.join(wt, "app.py"), os.path.join(main, "app.py")):
            run_hook("post_edit.py", {"session_id": session, "cwd": main, "tool_input": {"file_path": path}})
        result = subprocess.run(["python3", H + "stop_checks.py"], input=json.dumps({"session_id": session, "cwd": main}),
                                capture_output=True, text=True, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
        assert not os.path.isdir(wt) and "Traceback" not in result.stderr, (os.path.isdir(wt), result.stderr[-500:], result.stdout[-300:])
        reason = (json.loads(result.stdout) if result.stdout.strip() else {}).get("reason", "")
        assert "Debug leftover" in reason, "the checkouts still there are checked"
        assert "failed (exit 1)" not in reason, "a removed checkout's verify isn't reported as failing: " + reason
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def stop_catches_committed_leftover(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("breakpoint()\n")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "wip")
    reason = stop(RUN + "s9", repo, [os.path.join(repo, "app.py")]).get("reason", "")
    assert "Debug leftover" in reason, "a committed leftover still reaches the pull request"


def stop_falls_back_to_auto_verify(base):
    repo = new_repo(base, "zz-agents-no-overlay")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    stop(RUN + "s10", repo, [os.path.join(repo, "app.py")])
    report = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "path", "verify"], cwd=repo,
                            capture_output=True, text=True).stdout.strip()
    text = open(report).read()
    assert "verify_auto.py" in text and ("ran: " in text or "nothing was checked" in text), text


def stop_continues_only_once(base):
    repo = new_repo(base)
    open(os.path.join(repo, "app.py"), "a").write("breakpoint()\n")
    run_hook("post_edit.py", {"session_id": RUN + "s2", "cwd": repo, "tool_input": {"file_path": os.path.join(repo, "app.py")}})
    assert run_hook("stop_checks.py", {"session_id": RUN + "s2", "cwd": repo, "stop_hook_active": True}) is None


def stop_flags_secrets_redacted(base):
    repo = new_repo(base)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    open(os.path.join(repo, "cfg.py"), "w").write(f'TOKEN = "{token}"\n')
    reason = stop(RUN + "s3", repo, [os.path.join(repo, "cfg.py")]).get("reason", "")
    assert "Possible secret" in reason and token not in reason, reason


def stop_flags_marked_override_only(base):
    repo = new_repo(base)
    path = os.path.join(repo, "docker-compose.override.yml")
    open(path, "w").write("# agents: temporary override\nservices: {}\n")
    assert "override" in stop(RUN + "s4", repo, [os.path.join(repo, "app.py")]).get("reason", "")
    open(path, "w").write("services: {}\n")
    assert "override" not in stop(RUN + "s5", repo, [os.path.join(repo, "app.py")]).get("reason", "")


def stop_finds_override_in_primary_checkout_and_health_finds_it_later(base):
    # validate writes the override where the stack runs, the primary checkout, while the agent edits a worktree.
    main = os.path.realpath(new_repo(base, "main"))  # git reports /private/var for macOS's /var
    wt = os.path.join(base, "wt")
    git(main, "worktree", "add", "-q", "-b", "feat/o", wt, "trunk")
    open(os.path.join(main, "compose.override.yml"), "w").write("# agents: temporary override\nservices: {}\n")
    open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
    state = os.path.join(base, "state")
    env = {"AGENTS_STATE_DIR": state}
    run_hook("post_edit.py", {"session_id": RUN + "o1", "cwd": wt, "tool_input": {"file_path": os.path.join(wt, "app.py")}})
    reason = (run_hook("stop_checks.py", {"session_id": RUN + "o1", "cwd": wt}, env=env) or {}).get("reason", "")
    assert "compose.override.yml" in reason and main in reason, reason
    gone = os.path.join(base, "gone")
    open(os.path.join(state, "checkouts.txt"), "a").write(gone + "\n")
    out = subprocess.run([sys.executable, H + "stop_checks.py", "leftover-overrides"],
                         capture_output=True, text=True, env={**os.environ, **env})
    assert out.stdout.split() == [os.path.join(main, "compose.override.yml")], out
    assert open(os.path.join(state, "checkouts.txt")).read().split() == [main], "deleted checkouts are pruned"


def verify_stamp_and_effort_nudge(base):
    repo = new_repo(base, "zz-agents-test-repo")
    vdir = os.path.expanduser("~/.agents/repos/zz-agents-test-repo")
    os.makedirs(vdir, exist_ok=True)
    try:
        def stamped(kind):
            return subprocess.run(["python3", H + "review_stamp.py", "check", "--kind", kind], cwd=repo).returncode == 0
        report = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "path", "verify"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(vdir, "verify"), 0o755)
        open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
        stop(RUN + "s4", repo, [os.path.join(repo, "app.py")])
        assert not stamped("verify") and stamped("verify-empty"), "a silent green verify checked nothing"
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\necho 'skipped: ruff (not installed)'\n"
                                                      "echo 'ran: nothing to check: no changed PHP files'\n")
        open(os.path.join(repo, "app.py"), "a").write("w = 1\n")
        stop(RUN + "s5", repo, [os.path.join(repo, "app.py")])
        assert not stamped("verify") and stamped("verify-empty"), "a green verify that checked nothing isn't a pass"
        assert "# Verify: PASS, but nothing was checked" in open(report).read()
        # The verdict reads the whole output: the saved report keeps only its tail.
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\necho 'ran: pytest tests/test_app.py'\n"
                                                      "i=0; while [ $i -lt 400 ]; do echo 'skipped: a tool'; i=$((i+1)); done\n")
        open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
        stop(RUN + "s6", repo, [os.path.join(repo, "app.py")])
        assert stamped("verify"), "green verify should stamp the change"
        assert "# Verify: PASS\n" in open(report).read(), "verify should leave its evidence in a report"
        # Run by hand, verify stamps the change only through the same runner, and only when it passed.
        def verify_by_hand():
            return subprocess.run(["python3", H + "stop_checks.py", "verify"], cwd=repo, capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
        with open(os.path.join(repo, "app.py"), "a") as fh:
            fh.write("v = 4\n")
        assert not stamped("verify")
        done = verify_by_hand()
        assert done.returncode == 0 and stamped("verify") and "ran: pytest" in done.stdout, done.stdout + done.stderr
        assert "checked nothing" not in done.stderr, done.stderr
        verify_stamp = subprocess.run(["python3", "-c", f"import sys; sys.path.insert(0, {H!r}); import review_stamp as r; "
                                       "print(r.stamp_path('verify'))"], cwd=repo, capture_output=True, text=True).stdout.strip()
        os.chmod(verify_stamp, 0o400)
        try:
            done = verify_by_hand()
        finally:
            os.chmod(verify_stamp, 0o600)
        assert done.returncode == 1 and "stamp" in done.stderr, "a pass that couldn't be stamped isn't reported as stamped"
        verify_by_hand()
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho 'ran: nothing to check: no changed files'\n")
        done = verify_by_hand()
        assert stamped("verify-empty") and not stamped("verify"), "the latest run's stamp is the only one left"
        # The /flow pane's button reads this run's verdict here, not from a stamp another run can replace.
        assert done.returncode == 0 and "verify passed, but it checked nothing" in done.stderr, done.stderr
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho failing; exit 1\n")
        done = verify_by_hand()
        assert done.returncode == 1 and not stamped("verify") and not stamped("verify-empty") and "failing" in done.stdout, \
            "a failing rerun on unchanged code voids the earlier pass: " + done.stdout + done.stderr
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho 'ran: nothing to check: no changed files'\n")
        verify_by_hand()
        assert stamped("verify-empty")
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\nprintf 'bad \\377 byte\\n'; exit 1\n")
        done = verify_by_hand()
        assert done.returncode == 1 and not stamped("verify-empty") and "bad" in done.stdout, \
            "output that isn't UTF-8 still voids the earlier pass: " + done.stderr
        verify_by_hand_empty = subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", "verify"], cwd="/",
                                              capture_output=True, text=True)
        assert verify_by_hand_empty.returncode != 0 and "stop_checks.py verify" in verify_by_hand_empty.stderr, "outside a repo too"
        no_repo = subprocess.run(["python3", H + "stop_checks.py", "verify"], cwd=tempfile.mkdtemp(dir=base),
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                 env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, "GIT_CEILING_DIRECTORIES": base})
        assert no_repo.returncode == 1 and "not a git repository" in no_repo.stderr and "Traceback" not in no_repo.stderr, \
            "verify outside a repository says so instead of crashing: " + no_repo.stderr
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho 'ran: nothing to check: no changed files'\n")
        verify_by_hand()
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/no/such/interpreter\n")
        done = verify_by_hand()
        assert done.returncode == 1 and not stamped("verify-empty") and "interpreter" not in done.stdout, \
            "a verify that can't start voids the earlier pass: " + done.stderr
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho 'ran: pytest'; echo more >> app.py\n")
        done = verify_by_hand()
        assert done.returncode == 0 and not stamped("verify"), "an edit made while verify ran is not covered by its stamp"
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho 'ran: pytest, then hung'; sleep 5\n")
        timed_out = subprocess.run(["python3", "-c", f"import sys; sys.path.insert(0, {H!r}); import stop_checks as s; s.VERIFY_TIMEOUT = 1; "
                                    f"print(s.run_verify({repo!r}))"], cwd=repo, capture_output=True, text=True,
                                   env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
        with open(report) as fh:
            saved = fh.read()
        assert "True" in timed_out.stdout and "timed out" in saved and "then hung" in saved, (timed_out, saved)
        with open(os.path.join(vdir, "verify"), "w") as fh:
            fh.write("#!/bin/sh\necho failing; exit 1\n")
        outs = [stop(RUN + "s7", repo, [os.path.join(repo, "app.py")]) for _ in range(2)]
        assert "systemMessage" in outs[1] and "systemMessage" not in outs[0], outs
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def stop_asks_once_about_files_outside_the_change_map(base):
    repo = new_repo(base, "zz-agents-drift")
    for path in ("old.py", "lib/café.py"):
        os.makedirs(os.path.join(repo, os.path.dirname(path)), exist_ok=True)
        open(os.path.join(repo, path), "w").write("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "more")
    git(repo, "checkout", "-q", "-b", "feat/drift")
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/kit-spec/path.sh")], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    with_map = ("# Drift\n\nGoal: x.\n\n## Acceptance criteria\n1. y — verify: tests/test_app.py\n\n"
                "## Change map\n- Ways in: the app — app.py:1, ./lib/café.py:1, cache.py.bak\n\n"
                "## Assumptions\n- lib/util.py stays as it is.\n")
    open(spec, "w").write(with_map)
    open(os.path.join(repo, "committed.py"), "w").write("c = 1\n")
    git(repo, "add", "committed.py")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "committed")
    os.remove(os.path.join(repo, "old.py"))
    # Bytecode as a test run leaves it where Python has no pycache prefix (CI's Python; macOS's system one has one).
    for path in ("lib/util.py", "other/app.py", "tests/test_app.py", "package-lock.json", "__pycache__/app.cpython-314.pyc"):
        os.makedirs(os.path.join(repo, os.path.dirname(path)), exist_ok=True)
        open(os.path.join(repo, path), "w").write("x = 1\n")
    for path in ("app.py", "lib/café.py"):
        open(os.path.join(repo, path), "a").write("y = 2\n")

    def asked(session, edited):
        reason = stop(RUN + session, repo, [os.path.join(repo, p) for p in edited]).get("reason", "")
        line = next((line for line in reason.splitlines() if "Change map doesn't name" in line), "")
        return sorted(line.split("`")[1::2])
    everything = ["app.py", "lib/café.py", "lib/util.py", "other/app.py", "tests/test_app.py", "committed.py"]
    first = asked("d1", everything)
    assert first == ["committed.py", "lib/util.py", "old.py", "other/app.py"], \
        f"unmapped code files: committed, deleted, named only in Assumptions, or same-named elsewhere; not lock files: {first}"
    assert asked("d1", everything) == [], "each file is asked about once per session"
    open(os.path.join(repo, "cache.py"), "w").write("z = 3\n")
    assert asked("d1", ["cache.py"]) == ["cache.py"], "a file that drifts later is asked about; cache.py.bak isn't cache.py"
    open(spec, "w").write(with_map.replace("## Change map\n- Ways in: the app — app.py:1, ./lib/café.py:1, cache.py.bak\n\n", ""))
    assert asked("d2", everything) == [], "a spec without a Change map asks nothing"
    open(spec, "w").write(with_map)
    git(repo, "branch", "-m", "feat/drift-renamed")
    assert asked("d3", ["cache.py"]) == ["cache.py", "committed.py", "lib/util.py", "old.py", "other/app.py"], \
        "the spec follows a branch rename"


FAKE_GH = """#!/bin/sh
echo "$*" >> "$FAKE_GH_DIR/calls.log"
[ -f "$FAKE_GH_DIR/on-call" ] && { sh "$FAKE_GH_DIR/on-call"; rm "$FAKE_GH_DIR/on-call"; }
[ -f "$FAKE_GH_DIR/error" ] && { echo "error connecting to api.github.com" >&2; exit 1; }
case "$*" in
  *check-runs*) cat "$FAKE_GH_DIR/runs.jsonl" ;;
  *graphql*) [ -f "$FAKE_GH_DIR/probe.json" ] && cat "$FAKE_GH_DIR/probe.json" ;;
esac
exit 0
"""


def stop_follows_ci_after_a_push(base):
    remote = os.path.join(base, "remote.git")
    subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
    repo = new_repo(base)
    git(repo, "remote", "add", "origin", remote)
    git(repo, "checkout", "-q", "-b", "feat/ci")
    fake = os.path.join(base, "fake-gh")
    os.makedirs(fake)
    open(os.path.join(fake, "gh"), "w").write(FAKE_GH)
    os.chmod(os.path.join(fake, "gh"), 0o755)
    env = {"PATH": fake + ":" + os.environ["PATH"], "FAKE_GH_DIR": fake}

    def ci(state):
        if os.path.exists(os.path.join(fake, "error")):
            os.remove(os.path.join(fake, "error"))
        if state == "unreadable":
            open(os.path.join(fake, "error"), "w").write("")
        runs = [] if state in ("none", "unreadable") else [
            {"name": "suite", "status": "in_progress" if state == "running" else "completed",
             "conclusion": {"failed": "failure", "passed": "success"}.get(state), "details_url": ""},
            {"name": "ci/lint: py", "status": "completed", "conclusion": "success", "details_url": ""}]
        open(os.path.join(fake, "runs.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in runs))

    def push(session):
        open(os.path.join(repo, "app.py"), "a").write("n = 1\n")
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "more")
        git(repo, "push", "-q", "-u", "origin", "feat/ci")
        run_hook("guard_bash.py", {"tool_input": {"command": "git push -u origin feat/ci"}, "cwd": repo, "session_id": session})
        return git(repo, "rev-parse", "HEAD").stdout.strip()

    def asked(session):
        out = subprocess.run(["python3", H + "stop_checks.py"], input=json.dumps({"session_id": session, "cwd": repo}),
                             capture_output=True, text=True, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, **env})
        assert out.returncode == 0 and not out.stderr, f"the stop hook broke: {out.stderr}"
        return json.loads(out.stdout).get("reason", "") if out.stdout.strip() else ""

    session = RUN + "ci1"
    ci("passed")
    assert asked(session) == "", "a turn with no push asks nothing about CI"
    sha = push(session)
    ci("none")
    assert "still running" in asked(session), "no checks right after the push means they haven't started"
    ci("failed")
    reason = asked(session)
    assert "CI failed" in reason and "(suite)" in reason and f"ci-wait --sha {sha}" in reason, reason
    assert asked(session) == "", "the same failure is asked about once"
    sha = push(session)
    ci("running")
    reason = asked(session)
    assert "still running" in reason and f"ci-wait --sha {sha}" in reason, reason
    assert asked(session) == "", "a commit still running is asked about once"
    ci("unreadable")
    assert "Couldn't read CI" in asked(session), "an unknown state isn't taken as passed"
    ci("passed")
    assert asked(session) == ""
    calls = os.path.join(fake, "calls.log")
    before = open(calls).read()
    assert asked(session) == "" and open(calls).read() == before, "a checkout whose CI passed isn't queried again"

    # Each checkout has its own push time: an old push with no CI stays quiet when another checkout pushes now.
    session = RUN + "ci2"
    other = os.path.join(base, "other checkout")
    shutil.copytree(repo, other)
    pushed = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", f"{session}.pushed")
    run_hook("guard_bash.py", {"tool_input": {"command": f'git -c color.ui=never -C "{other}" push'}, "cwd": repo,
                               "session_id": session})
    open(pushed, "a").write(f"{os.path.realpath(repo)}\t{time.time() - 3600}\n")
    ci("none")
    reason = asked(session)
    assert reason.count("still running") == 1, f"only the checkout pushed just now is still waiting for checks: {reason}"
    shutil.rmtree(other)
    run_hook("guard_bash.py", {"tool_input": {"command": "git push"}, "cwd": repo, "session_id": session})
    ci("failed")
    reason = asked(session)
    assert reason.count("CI failed") == 1, f"a checkout removed after its push doesn't stop the others' checks: {reason}"

    # A push recorded while the stop reads CI (a subagent's) survives the stop rewriting the file.
    run_hook("guard_bash.py", {"tool_input": {"command": "git push"}, "cwd": repo, "session_id": session})
    open(os.path.join(fake, "on-call"), "w").write(f"printf '/pushed/meanwhile\\t{time.time()}\\n' >> '{pushed}'\n")
    ci("passed")
    asked(session)
    assert "/pushed/meanwhile" in open(pushed).read(), open(pushed).read()

    # A bare path, as the guard wrote before push times: it was pushed when the file was last written.
    session = RUN + "ci3"
    pushed = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", f"{session}.pushed")
    open(pushed, "w").write(os.path.realpath(repo) + "\n")
    ci("none")
    assert "still running" in asked(session), "a fresh push from an older guard still gets its grace"

    session = RUN + "ci4"
    phase_line("phase off ci", repo)
    sha = push(session)
    ci("failed")
    before = open(calls).read()
    assert asked(session) == "" and open(calls).read() == before, "ci off: CI isn't even queried after a push"
    phase_line("phase on ci", repo)
    assert f"CI failed on {sha[:12]}" in asked(session), "back on, the pushed commit's CI is asked about"

    # A repo that plainly runs no CI isn't waited for, even right after the push; CI that only runs on pull requests is.
    def probe(pr_checked):
        rollup = {"statusCheckRollup": {"state": "SUCCESS"} if pr_checked else None}
        open(os.path.join(fake, "probe.json"), "w").write(json.dumps({"data": {"repository": {
            "defaultBranchRef": {"target": {"statusCheckRollup": None}}, "parent": {"statusCheckRollup": None},
            "pullRequests": {"nodes": [{"commits": {"nodes": [{"commit": rollup}]}}]}}}}))
    session = RUN + "ci5"
    probe(pr_checked=False)
    push(session)
    ci("none")
    pushed = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", f"{session}.pushed")
    assert asked(session) == "", "no CI anywhere: nothing to wait for right after the push"
    assert os.path.realpath(repo) not in open(pushed).read(), "a repo with no CI isn't followed"
    assert open(pushed[:-len(".pushed")] + ".ci-asked").read() == "", "a repo with no CI records no question"
    before = open(calls).read()
    assert asked(session) == "" and open(calls).read() == before, "a repo with no CI isn't queried again"
    session = RUN + "ci6"
    probe(pr_checked=True)
    push(session)
    assert "still running" in asked(session), "checks on a recent pull request: CI runs on pull requests, so wait"
    os.remove(os.path.join(fake, "probe.json"))


def guard_records_the_pushed_checkout(base):
    repo, other = new_repo(os.path.join(base, "a")), new_repo(os.path.join(base, "b c"))
    pushed = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", f"{RUN}push.pushed")

    def recorded(command):
        if os.path.exists(pushed):
            os.remove(pushed)
        if run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": RUN + "push"}):
            return "denied"
        return sorted({line.split("\t")[0] for line in open(pushed).read().splitlines()}) if os.path.exists(pushed) else []

    here, there = os.path.realpath(repo), os.path.realpath(other)
    assert recorded("git push") == [here]
    assert recorded(f'cd "{other}" && git push -u origin HEAD') == [there]
    assert recorded(f'git --no-pager -C "{other}" push') == [there]
    assert recorded(f'git push; cd "{other}"; git push') == sorted([here, there])
    assert recorded(f"  cd {other.replace(' ', chr(92) + ' ')} && git push") == [there]
    assert recorded(f'git status\n\n  \n\t cd "{other}"\n\n\tgit push') == [there], "a cd between blank lines still counts"
    assert recorded(f'(cd "{other}" && git status); git push') == [here], "a subshell's cd stays in the subshell"
    assert recorded("git status && echo 'git push'") == [], "a push only in quoted text isn't one"
    for prose in ["cat > \"$(echo /tmp/n.md)\" <<'EOF'\nthen git push\nEOF",
                  "cat > \"$(echo /tmp/n.md)\" <<'EOF'\nnever git push --force origin x\nEOF"]:
        assert recorded(prose) == [], f"a push only in a heredoc's text isn't one: {prose!r}"


def post_edit_syntax_feedback(base):
    repo = new_repo(base)
    bad = os.path.join(repo, "bad.py")
    open(bad, "w").write("def f(:\n")
    # Harnesses set FORCE_COLOR, and Python 3.13+ then colours its tracebacks: the agent must get plain text.
    d = run_hook("post_edit.py", {"session_id": RUN + "s8", "cwd": repo, "tool_input": {"file_path": bad}},
                 env={"FORCE_COLOR": "3"})
    assert d and d.get("decision") == "block", d
    assert "line 1: invalid syntax" in d["reason"] and "\x1b" not in d["reason"] and "Traceback" not in d["reason"], d
    # py_compile wrote bytecode into the user's repo wherever Python keeps it next to the source (CI's did).
    # An explicit cache prefix makes any written bytecode visible on every machine.
    good, prefix = os.path.join(repo, "good.py"), os.path.join(base, "pycache")
    open(good, "w").write("x = 1\n")
    assert run_hook("post_edit.py", {"session_id": RUN + "s9", "cwd": repo, "tool_input": {"file_path": good}},
                    env={"PYTHONPYCACHEPREFIX": prefix}) is None
    written = [f for _, _, files in os.walk(prefix) for f in files if f.startswith("good.")]
    assert not written, f"the syntax check must not write bytecode: {written}"


def impeccable_files_in_a_shared_repo_need_the_user_first(base):
    profiles = os.path.join(base, "profiles")
    os.makedirs(os.path.join(profiles, "me"))
    env = {"HOME": base, "AGENTS_PROFILES_DIR": profiles}
    shared, personal, local = new_repo(base, "shared"), new_repo(base, "personal"), new_repo(base, "local")
    git(shared, "remote", "add", "origin", "git@github.com:someone/app.git")
    git(personal, "remote", "add", "origin", "https://github.com/Me/site.git")
    open(os.path.join(profiles, "me", "personal-repos.txt"), "w").write("# mine\ngithub.com/me\n")
    outside = tempfile.mkdtemp(dir=base)

    def prompt(text, session="d1"):
        run_hook("prompt_approvals.py", {"prompt": text, "session_id": session, "cwd": shared}, env=env)

    def verdict(got):  # only this guard's deny counts: a crash or another rule must not pass for it
        why = (got or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
        assert not got or "needs the user's say-so" in why or "approvals come from the user" in why, why
        return "deny" if got else "allow"

    def tool(name, inp, cwd=shared, session="d1"):
        return verdict(run_hook("guard_files.py", {"tool_name": name, "tool_input": inp, "cwd": cwd, "session_id": session},
                                env=env))

    def shell(command, cwd=shared, session="d1"):
        return verdict(run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": session},
                                env=env))

    prompt("rediseña la home")
    product = os.path.join(shared, "PRODUCT.md")
    for name, inp in [("Write", {"file_path": product, "content": "x"}),
                      ("Write", {"file_path": os.path.join(shared, "DESIGN.md"), "content": "x"}),
                      ("Edit", {"file_path": os.path.join(shared, ".impeccable", "config.json"), "new_string": "{}"}),
                      ("MultiEdit", {"file_path": product, "edits": [{"new_string": "x"}]}),
                      ("apply_patch", {"input": "*** Begin Patch\n*** Add File: DESIGN.md\n+x\n*** End Patch"}),
                      ("Write", {"file_path": os.path.join(shared, ".gitignore"), "content": "node_modules\n.impeccable/\n"}),
                      ("apply_patch", {"input": "*** Begin Patch\n*** Update File: .gitignore\n@@\n+DESIGN.md\n*** End Patch"})]:
        assert tool(name, inp) == "deny", f"{name} {inp} in a shared repository"
    for command in ["cat > PRODUCT.md <<'EOF'\n# x\nEOF", "echo x >> DESIGN.md", "mkdir -p .impeccable/mocks",
                    "tee PRODUCT.md < /tmp/x", "cp /tmp/x DESIGN.md", "touch .impeccable/config.json",
                    "python3 -c \"open('PRODUCT.md','w').write('x')\"", "echo .impeccable >> .gitignore",
                    "~/.agents/skills/impeccable/scripts/impeccable serve-question --start --payload p.json",
                    "npx impeccable live", "impeccable hooks on", "mv /tmp/x PRODUCT.md", "install -m 644 /tmp/x DESIGN.md",
                    "ln -s /tmp/x PRODUCT.md", "echo DESIGN.md | tee -a .gitignore", "cat >> .gitignore <<'EOF'\nDESIGN.md\nEOF",
                    "npx -y impeccable@latest live", "FOO=1 impeccable hooks reset", "echo x >| DESIGN.md"]:
        assert shell(command) == "deny", command
    for name, inp in [("Edit", {"file_path": os.path.join(shared, ".gitignore"), "old_string": "", "new_string": "PRODUCT.md"}),
                      ("apply_patch", {"input": "*** Begin Patch\n*** Update File: notes.md\n*** Move to: PRODUCT.md\n*** End Patch"})]:
        assert tool(name, inp) == "deny", f"{name} {inp}"
    assert shell("git commit -m 'docs; impeccable live notes'") == "allow", "quoted text isn't a command"
    assert shell("cat > README.md <<'EOF'\nimpeccable live\nEOF") == "allow", "nor is a heredoc body"
    assert shell("impeccable help") == "allow"
    for command in ["impeccable context", "impeccable detect --json index.html", "impeccable doctor", "cat PRODUCT.md",
                    "grep DESIGN.md README.md", "echo hi > notes.txt"]:
        assert shell(command) == "allow", f"read-only or unrelated: {command}"
    reason = run_hook("guard_files.py", {"tool_name": "Write", "tool_input": {"file_path": product, "content": "x"},
                                        "cwd": shared, "session_id": "d1"}, env=env)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`PRODUCT.md`" in reason and "carry on without it" in reason, reason

    for cwd in (personal, local, outside):
        assert tool("Write", {"file_path": os.path.join(cwd, "PRODUCT.md"), "content": "x"}, cwd) == "allow", cwd
        assert shell("mkdir -p .impeccable && impeccable live", cwd) == "allow", cwd

    prompt("vale, crea PRODUCT.md y DESIGN.md")
    assert tool("Write", {"file_path": product, "content": "x"}) == "allow", "the user named it this turn"
    assert tool("Write", {"file_path": product, "content": "x"}, session="other") == "deny", "only in that session"
    assert shell("mkdir -p .impeccable") == "deny", "and only what was named"
    prompt("sí, usa .impeccable y añádelo al .gitignore")
    assert shell("mkdir -p .impeccable && echo .impeccable/ >> .gitignore") == "allow"
    os.makedirs(os.path.join(shared, ".impeccable"))
    open(os.path.join(shared, ".gitignore"), "w").write(".impeccable/\n")
    prompt("sigue")
    assert shell("impeccable serve-question --start --payload p.json") == "allow", "once .impeccable/ exists"
    assert tool("Write", {"file_path": os.path.join(shared, ".impeccable", "a.json"), "content": "{}"}) == "allow"
    assert tool("Write", {"file_path": os.path.join(shared, ".gitignore"), "content": ".impeccable/\ndist\n"}) == "allow", \
        "a .gitignore that already lists it"
    assert shell("impeccable live") == "deny" and shell("impeccable hooks reset") == "deny", \
        "live and hooks edit project files even when .impeccable/ exists"
    prompt("arranca impeccable live")
    assert shell("impeccable live") == "allow" and shell("impeccable hooks on") == "deny"
    prompt("ahora sí, hooks on")
    assert shell("impeccable hooks on") == "allow" and shell("impeccable live") == "deny"
    assert shell("impeccable live-poll") == "allow", "once live runs, its helpers are free"
    prompt("añade node_modules al .gitignore")
    os.remove(os.path.join(shared, ".gitignore"))
    assert shell("echo DESIGN.md >> .gitignore") == "deny", "naming .gitignore alone doesn't allow gitignoring the files"
    prompt("don’t create DESIGN.md")
    assert tool("Write", {"file_path": os.path.join(shared, "DESIGN.md"), "content": "x"}) == "deny", "a curly apostrophe"
    prompt("DESIGN.md? not yet")
    assert tool("Write", {"file_path": os.path.join(shared, "DESIGN.md"), "content": "x"}) == "deny", "a refusal after it"
    own_approval = os.path.join(base, ".agents", "approvals", "turn", "d1", "design.design-md")
    assert tool("Write", {"file_path": own_approval, "content": ""}) == "deny", "the agent can't write its own approval"
    prompt("no crees DESIGN.md todavía")
    assert tool("Write", {"file_path": os.path.join(shared, "DESIGN.md"), "content": "x"}) == "deny", "a refusal grants nothing"
    prompt("sí, crea PRODUCT.md")
    assert shell("echo PRODUCT.md >> .gitignore") == "deny", "creating it doesn't allow gitignoring it"
    assert shell("impeccable doctor --fix", local) == "allow" and shell("impeccable hooks status") == "allow"
    shutil.rmtree(os.path.join(shared, ".impeccable"))
    open(os.path.join(shared, ".gitignore"), "w").write(".impeccable/\n")
    assert shell("impeccable doctor --fix") == "deny", "doctor --fix writes project files"
    assert shell(f"cd {shared} && mkdir .impeccable", outside) == "deny", "a cd earlier in the chain"
    git(personal, "remote", "add", "upstream", "git@github.com:work/site.git")
    assert tool("Write", {"file_path": os.path.join(personal, "DESIGN.md"), "content": "x"}, personal) == "deny", \
        "a fork of someone else's repository is shared"
    prompt("this product is great; the design matters")
    assert tool("Write", {"file_path": os.path.join(shared, "DESIGN.md"), "content": "x"}) == "deny", \
        "only the file's own name counts"


def impeccable_guard_edge_cases(base):
    profiles = os.path.join(base, "profiles")
    os.makedirs(os.path.join(profiles, "broken"))
    open(os.path.join(profiles, "broken", "mcp-writes.json"), "w").write("{not json")
    env = {"HOME": base, "AGENTS_PROFILES_DIR": profiles}
    shared = new_repo(base, "shared")
    git(shared, "remote", "add", "origin", "git@github.com:someone/app.git")
    os.makedirs(os.path.join(shared, "packages", "web"))
    src = tempfile.mkdtemp(dir=base)
    for name in ("PRODUCT.md", "DESIGN.md"):
        open(os.path.join(src, name), "w").write("x")

    def shell(command, cwd=shared):
        got = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": cwd, "session_id": "e1"}, env=env)
        return (got or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "allow")

    assert "rewrites remote history" in shell("git push --force origin x"), \
        "a broken profile's MCP rules don't take the shell guard down"
    assert "PRODUCT.md" in shell("echo x > PRODUCT.md"), "nor this one, which still asks before the file"
    os.remove(os.path.join(profiles, "broken", "mcp-writes.json"))
    for command in [f"cp {src}/PRODUCT.md .", f"mv {src}/DESIGN.md .", f"cp -R {src}/PRODUCT.md packages",
                    'echo x >"packages/web/DESIGN.md"', "env NODE_ENV=dev impeccable live", "CI=1 impeccable hooks on",
                    "sh -c 'touch PRODUCT.md'", "(touch DESIGN.md)", "impeccable hook-admin on",
                    "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: PRODUCT.md\n+x\n*** End Patch\nEOF",
                    "touch product.md", "mkdir .IMPECCABLE", "echo .impeccable/ >> .GitIgnore",
                    "sed -i '' '$a\\\n.impeccable/' .gitignore"]:
        assert "say-so" in shell(command), command
    for command in ["impeccable hooks", "impeccable serve-question --schema", "impeccable doctor"]:
        assert shell(command) == "allow", f"read-only: {command}"
    os.makedirs(os.path.join(shared, ".impeccable"))
    assert "say-so" in shell("impeccable serve-question --start", os.path.join(shared, "packages", "web")), \
        "Impeccable writes .impeccable/ where it runs, not at the root"
    open(os.path.join(shared, ".gitignore"), "w").write("# PRODUCT.md is tracked\n!DESIGN.md\n.impeccable/config.local.json\n")
    for rule in ("PRODUCT.md", "DESIGN.md", ".impeccable/"):
        assert "say-so" in shell(f"echo {rule} >> .gitignore"), f"a comment or negation doesn't list {rule}"

    def grants(text):
        return subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import design_files; "
                               "print(design_files.approval_names(sys.argv[2]))", H, text],
                              capture_output=True, text=True, env={**os.environ, **env}).stdout.strip()
    assert grants("Don't create PRODUCT.md or DESIGN.md") == "[]", "the dot in a name isn't a sentence end"
    assert grants("Do not create any additional files for this project, including PRODUCT.md") == "[]"
    assert grants("Do not add PRODUCT.md to .gitignore") == "[]"
    assert grants("create PRODUCT.md.bak") == "[]", "another file's name"
    assert grants("No tests for now. Create PRODUCT.md.") == "['design.product-md']", "a refusal about something else"
    assert grants("Now create PRODUCT.md") == "['design.product-md']", "a word starting with no isn't a refusal"
    assert grants("Note: create DESIGN.md") == "['design.design-md']"
    assert grants("DESIGN.md? Do not create it.") == "[]" and grants("PRODUCT.md? not now") == "[]"

    fresh = new_repo(base, "fresh")
    git(fresh, "remote", "add", "origin", "git@github.com:someone/web.git")
    for command in ["cp /tmp/x PRODUCT.md > /dev/null", "install -d .impeccable", f"ln -s {src}/PRODUCT.md",
                    "echo x &> PRODUCT.md", "env -i impeccable live", "command -- impeccable live", "nohup impeccable live",
                    "npx impeccable doctor --fix", "(cd /tmp && true); touch PRODUCT.md",
                    f"cd {fresh} && apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: PRODUCT.md\n+x\n*** End Patch\nEOF",
                    "apply_patch <<'EOF'\n*** Begin Patch\n*** Update File: .gitignore\n@@\n+PRODUCT.md\n*** End Patch\nEOF"]:
        assert "say-so" in shell(command, fresh if not command.startswith("cd ") else base), command
    for command in ["npx impeccable serve-question --schema", "impeccable live --help", "impeccable hooks on --help",
                    "cat PRODUCT.md", "ls DESIGN.md"]:
        assert shell(command, fresh) == "allow", f"read-only: {command}"
    spaced = os.path.join(base, "My App")
    os.makedirs(spaced)
    git(spaced, "init", "-q")
    git(spaced, "remote", "add", "origin", "git@github.com:someone/my-app.git")
    for command in ["if true; then impeccable live; fi", "{ impeccable hooks on; }", "! impeccable live",
                    "/usr/bin/env impeccable live", "env cp /tmp/x/PRODUCT.md .", f'cd "{spaced}" && touch PRODUCT.md']:
        assert "say-so" in shell(command, fresh if "My App" not in command else base), command
    for command in ["grep PRODUCT.md .gitignore", "rg 'PRODUCT.md|DESIGN.md' .gitignore"]:
        assert shell(command, fresh) == "allow", f"read-only: {command}"
    run_hook("prompt_approvals.py", {"prompt": "add PRODUCT.md to .gitignore", "session_id": "e1", "cwd": fresh}, env=env)
    assert shell("echo PRODUCT.md >> .gitignore", fresh) == "allow"
    assert "say-so" in shell("echo DESIGN.md >> .gitignore", fresh), "gitignoring one file doesn't allow the others"
    run_hook("prompt_approvals.py", {"prompt": "ok, run impeccable hooks on", "session_id": "e1", "cwd": fresh}, env=env)
    assert shell("impeccable hooks on", fresh) == "allow"
    assert "say-so" in shell("impeccable hooks reset", fresh), "approving hooks on doesn't approve reset"

    def tool(inp, cwd=fresh):
        got = run_hook("guard_files.py", {"tool_name": "apply_patch" if "input" in inp else "Write", "tool_input": inp,
                                          "cwd": cwd, "session_id": "e2"}, env=env)
        return (got or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "allow")
    patch = "*** Begin Patch\n*** Update File: README.md\n@@\n+See PRODUCT.md\n*** Update File: .gitignore\n@@\n+dist/\n*** End Patch"
    assert tool({"input": patch}) == "allow", "a README line isn't an ignore rule"
    assert tool({"file_path": os.path.join(fresh, ".gitignore"), "content": "# PRODUCT.md is tracked\ndist/\n"}) == "allow", \
        "nor is a comment"
    assert "say-so" in tool({"file_path": "~/fresh/PRODUCT.md", "content": "x"}, base), "~ is the home, not a folder in cwd"
    os.makedirs(os.path.join(profiles, "broken"), exist_ok=True)
    open(os.path.join(profiles, "broken", "mcp-writes.json"), "w").write("{not json")
    personal = new_repo(base, "personal")
    assert tool({"file_path": os.path.join(personal, "a.py"), "content": "x"}, personal) == "allow", \
        "a broken MCP profile doesn't block every file write"
    os.remove(os.path.join(profiles, "broken", "mcp-writes.json"))
    git(personal, "remote", "add", "origin", "git@github.com:me/site.git")
    git(personal, "config", "remote.origin.pushurl", "git@github.com:someone/site.git")
    open(os.path.join(profiles, "broken", "personal-repos.txt"), "w").write("github.com/me\n")
    assert "say-so" in tool({"file_path": os.path.join(personal, "PRODUCT.md"), "content": "x"}, personal), \
        "a work push destination makes it shared"

    with open(os.path.join(shared, ".git", "config"), "a") as fh:
        fh.write('[remote "x"\n')  # git can't parse its config: that's no proof of a personal repository
    assert "say-so" in shell("touch DESIGN.md"), "a failing git counts as shared"

def hook_log_names_the_suite_run_only_inside_one(base):
    # run.sh counts a leaked line by this field when its cwd is a fixed path like /tmp.
    def logged(**env):
        log_dir = tempfile.mkdtemp(dir=base)
        code = f"import sys; sys.path.insert(0, {H!r}); import hooklog; hooklog.log('t', 'allow', {{'cwd': '/tmp'}}, '')"
        outside = {k: v for k, v in os.environ.items() if k != "AGENTS_SUITE_RUN"}
        subprocess.run(["python3", "-c", code], env={**outside, "AGENTS_LOG_DIR": log_dir, **env}, check=True)
        return json.loads(open(os.path.join(log_dir, "hooks.jsonl")).read())
    assert logged(AGENTS_SUITE_RUN="kit-suite-abc").get("suite_run") == "kit-suite-abc"
    assert "suite_run" not in logged(), "a hook outside the suite logs no suite_run"
    assert "suite_run" not in logged(AGENTS_SUITE_RUN=""), "an empty id is no id"


def impeccable_guard_reads_prose_as_text(base):
    profiles = os.path.join(base, "profiles")
    os.makedirs(profiles)
    env = {"HOME": base, "AGENTS_PROFILES_DIR": profiles}
    shared = new_repo(base, "shared")
    git(shared, "remote", "add", "origin", "git@github.com:someone/app.git")

    def shell(command):
        got = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": shared, "session_id": "p1"}, env=env)
        return (got or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "allow")

    # The rule took its targets from the command's own text, so a mention in prose read as a write.
    for label, command in [
            ("a commit message", "git commit -m 'Ask before PRODUCT.md and .impeccable/ in shared repos'"),
            ("a double-quoted one", 'git commit -m "Keep DESIGN.md out; impeccable live is gated"'),
            ("an echo to a log", "echo 'checked PRODUCT.md' >> /tmp/log.txt"),
            ("a quoted heredoc with $( after it", "cat > notes.txt <<'EOF'\nRun `impeccable detect`; never create PRODUCT.md.\n"
                                                    "EOF\nN=$(wc -l < notes.txt)"),
            ("a commit message from a heredoc", "git commit -m \"$(cat <<'EOF'\nAsk before DESIGN.md\nEOF\n)\""),
            ("a delimiter with a dash", "cat > notes.txt <<'DOC-END'\nCreate PRODUCT.md here\nDOC-END\nN=$(pwd)")]:
        assert shell(command) == "allow", label
    os.makedirs(os.path.join(shared, "My App"))
    for label, command in [
            ("python -c with spaces", "python3 -c \"open('PRODUCT.md', 'w').write('x')\""),
            ("a quoted path", 'echo x > "DESIGN.md"'),
            ("a quoted path with a space", 'echo x > "My App/PRODUCT.md"'),
            ("a quoted folder with a space", "mkdir -p '.impeccable/design notes'"),
            ("a script on stdin", "N=$(pwd)\npython3 - <<'PY'\nopen('PRODUCT.md', 'w').write('x')\nPY"),
            ("a here-string", "sh <<< 'touch PRODUCT.md'"),
            ("a pipe into sh", "printf '%s\\n' 'touch PRODUCT.md' | sh"),
            ("quotes in an unquoted heredoc", "cat > notes.txt <<EOF\n'$(touch PRODUCT.md)'\nEOF"),
            ("a heredoc only mentioned", "# Use <<'EOF' for literal text\ntouch PRODUCT.md"),
            ("a heredoc header that goes on", "cat <<'EOF' \\\n> PRODUCT.md\ncontent\nEOF"),
            ("a partly quoted delimiter", "cat <<'EOF'x\nnotes\nEOFx\ntouch PRODUCT.md"),
            ("an unquoted heredoc first", "cat <<A <<'B'\n$(touch PRODUCT.md)\nA\nnotes\nB"),
            ("a versioned python", "python3.12 -c \"open('PRODUCT.md', 'w').write('x')\""),
            ("python flags with values", "python3 -W ignore -c \"open('PRODUCT.md', 'w').write('x')\""),
            ("long shell flags", "bash --noprofile --norc -c 'touch PRODUCT.md'"),
            ("node long flags", "node --input-type=module -e \"import fs from 'fs'; fs.writeFileSync('PRODUCT.md', 'x')\""),
            ("python behind command", "command python3 -c \"open('PRODUCT.md', 'w').write('x')\""),
            ("python behind env", "env MODE=test python3 -c \"open('PRODUCT.md', 'w').write('x')\""),
            ("python in an if", "if python3 -c \"open('PRODUCT.md', 'w').write('x')\"; then :; fi"),
            ("awk", "awk 'BEGIN { print \"text\" > \"PRODUCT.md\" }'"),
            ("a process substitution", "source <(printf '%s\\n' 'touch PRODUCT.md')"),
            ("a heredoc example in a comment, closed later", "# Example <<'EOF'\ntouch PRODUCT.md\nEOF\nN=$(pwd)"),
            ("a dashed unquoted delimiter", "cat <<DOC-END\nDOC\n'$(touch PRODUCT.md)'\nDOC-END"),
            ("apostrophes in comments", "# Don't forget the file\ntouch PRODUCT.md\n# It's done"),
            ("escaped quotes", "echo \\'start; touch PRODUCT.md; echo \\'end"),
            ("a quoted .gitignore with a space", "N=$(pwd); echo PRODUCT.md >> 'My App/.gitignore'"),
            ("an echo around a substitution", "echo $(touch 'PRODUCT.md')"),
            ("a quoted part of a redirect target", 'echo text > ./"PRODUCT.md"'),
            ("a -m that isn't a message", "git checkout -m 'PRODUCT.md'"),
            ("a heredoc owned by a later command", "cat /dev/null; python3 - <<'PY'\nopen('PRODUCT.md', 'w').write('x')\nPY\nN=$(pwd)"),
            ("a patch in a heredoc", "N=$(pwd)\ngit apply <<'PATCH'\ndiff --git a/PRODUCT.md b/PRODUCT.md\nnew file mode 100644\n"
                                     "--- /dev/null\n+++ b/PRODUCT.md\n@@ -0,0 +1 @@\n+x\nPATCH"),
            ("a heredoc after a comment mark", "cat /dev/null # <<':'\ntouch PRODUCT.md\n:\nN=$(pwd)"),
            ("a command named like echo", 'echo-file() { touch "$1"; }\necho-file "PRODUCT.md"'),
            ("a process substitution after an echo", "echo text > >(tee 'PRODUCT.md')"),
            ("a cat heredoc run by bash", "bash <<<$(cat <<'SCRIPT'\ntouch PRODUCT.md\nSCRIPT\n)"),
            ("a heredoc after a background job", "cat build.log & python3 - <<'PY'\nopen('PRODUCT.md', 'w').write('x')\nPY\nN=$(pwd)"),
            ("$( inside double quotes", 'echo "$(touch PRODUCT.md) done"'),
            ("a heredoc into .gitignore", "cat >> .gitignore <<'EOF'\nPRODUCT.md\nEOF\nN=$(wc -l < .gitignore)"),
            ("a heredoc into the file", "cat > DESIGN.md <<'EOF'\n# Design\nEOF"),
            ("sh -c with spaces", "sh -c 'cd . && touch PRODUCT.md'")]:
        assert "say-so" in shell(command), label
    # Backtracking regexes took seconds on each of these.
    for label, command in [("a long unclosed quote", "echo PRODUCT.md '" + "a " * 100000),
                           ("a run of separators", "echo PRODUCT.md " + ";" * 100000),
                           ("escaped quotes", "echo PRODUCT.md " + '\\"a ' * 20000 + "; touch DESIGN.md"),
                           ("many short strings", "N=$(pwd); echo " + "'a b' " * 20000)]:
        started = child_cpu()
        shell(command)
        assert child_cpu() - started < 2, f"{child_cpu() - started:.1f}s of CPU on {label}"

PHASE_STORE = os.path.expanduser("~/.agents/approvals/phases")


def phase_line(text, cwd, session="phase"):
    """What prompt_approvals.py tells the agent after the user's message `text`, or None when it says nothing.
    A hook that crashed says nothing too, so it must have exited cleanly."""
    done = subprocess.run(["python3", H + "prompt_approvals.py"], capture_output=True, text=True,
                          input=json.dumps({"prompt": text, "session_id": RUN + session, "cwd": cwd}),
                          env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
    assert done.returncode == 0 and not done.stderr, f"the prompt hook broke on {text!r}: {done.stderr}"
    return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] if done.stdout.strip() else None


def stored_switches():
    """Every switch file in the store, as {path under the store: its JSON}: what the hook wrote, not what applies."""
    found = {}
    for folder, _, names in os.walk(PHASE_STORE):
        for name in names:
            if name.endswith(".json"):
                found[os.path.relpath(os.path.join(folder, name), PHASE_STORE)] = json.load(open(os.path.join(folder, name)))
    return found


def phases_off(cwd):
    """{phase: why it is off} as `bin/phase switches` prints them."""
    done = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "switches"], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    return dict(re.findall(r"^(\S+): off \((.+)\)$", done.stdout, re.M))


def a_phase_line_switches_phases_by_scope(base):
    a, b = new_repo(base, "zz-phase-a"), new_repo(base, "zz-phase-b")
    for repo in (a, b):
        git(repo, "switch", "-q", "-c", "feature")
    said = phase_line("phase off validate\nGo ahead with the fix.", a)
    assert "validate" in said and "branch" in said, said
    assert phases_off(a) == {"validate": "branch"}, phases_off(a)
    assert phases_off(b) == {}, "the same branch name in another repo keeps its phases"
    git(a, "switch", "-q", "-c", "other")
    assert phases_off(a) == {}, "a branch switch stays on its branch"
    phase_line("Phase off verify repo", a)
    git(a, "switch", "-q", "feature")
    assert phases_off(a) == {"validate": "branch", "verify": "repo"}, phases_off(a)
    worktree = os.path.join(base, "zz-phase-a-wt")
    git(a, "worktree", "add", "-q", worktree, "other")
    assert phases_off(worktree) == {"verify": "repo"}, "a worktree is the same repo"
    phase_line("phase off review global", b)
    assert phases_off(b) == {"review": "global"} and phases_off(a)["review"] == "global"
    phase_line("phase on review", b)
    assert phases_off(b) == {}, "a branch's on overrides a global off"
    assert phases_off(a)["review"] == "global"
    said = phase_line("phase off staging", a)
    assert said and "Unknown phase `staging`" in said and "staging" not in phases_off(a), "staging left the core flow"
    stored = os.path.join(PHASE_STORE, "global.json")
    kept = open(stored).read()
    with open(stored, "w") as fh:
        json.dump({**json.loads(kept), "staging": "off"}, fh)  # switched off before staging left the flow
    assert phases_off(a) == {"review": "global", "validate": "branch", "verify": "repo"}, phases_off(a)
    with open(stored, "w") as fh:
        fh.write(kept)
    shown = subprocess.run([os.path.expanduser("~/.agents/bin/reports")], cwd=a, capture_output=True, text=True).stdout
    assert "phases the user switched off" in shown and "validate: off (branch)" in shown, shown
    brief = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "brief"], cwd=a, capture_output=True, text=True).stdout
    assert "off: review (global), validate (branch), verify (repo)\n" in brief, brief
    git(a, "tag", "feature")
    assert phases_off(a)["validate"] == "branch", "a tag named like the branch doesn't move its switches"

    before = stored_switches()
    for text in ("please phase off spec", "> phase off spec", "Notes:\nphase off spec", "phase of spec", "phase offline",
                 "Phase on-call rota is broken", "Phase off: spec is done"):
        assert phase_line(text, b) is None, f"not a phase line: {text!r}"
    for text, why in (("phase off create-pr", "Unknown phase"), ("phase off spec everywhere", "Unknown scope"),
                      ("phase off spec repo please", "Not `phase off <phase> [branch|repo|global]`"),
                      ("phase off", "Not `phase off <phase> [branch|repo|global]`")):
        said = phase_line(text, b)
        assert said and why in said, f"{text!r}: {said}"
    outside = os.path.join(base, "not-a-repo")
    os.makedirs(outside)
    for text in ("phase off spec", "phase off spec repo"):
        said = phase_line(text, outside)
        assert said and "git repository" in said, f"{text!r} outside a repo: {said}"
    broken = os.path.join(base, "broken-worktree")
    os.makedirs(broken)
    open(os.path.join(broken, ".git"), "w").write(f"gitdir: {os.path.join(base, 'gone', '.git', 'worktrees', 'x')}\n")
    said = phase_line("phase off spec", broken)
    assert said and "git failed" in said, f"a broken worktree is a git failure, not a directory outside git: {said}"
    done = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "switches"], cwd=broken, capture_output=True, text=True)
    assert done.returncode == 1 and "Couldn't read the phase switches" in done.stderr, done.stdout + done.stderr
    git(b, "checkout", "-q", "--detach")
    said = phase_line("phase off spec", b)
    assert said and "no branch" in said, said
    assert stored_switches() == before, "none of those wrote a switch"
    assert "every branch of zz-phase-b" in phase_line("phase off spec repo", b), "a repo switch needs no branch"
    git(b, "switch", "-q", "feature")
    assert phases_off(b) == {"spec": "repo"}
    assert "spec" in phase_line("phase off spec\r\nThanks", a), "a message with CRLF line ends still switches"
    assert phases_off(a)["spec"] == "branch"
    assert "global" in phase_line("phase off ci global", outside), "a global switch needs no repo"
    assert phases_off(a)["ci"] == "global"

    # The turn approvals of the last message are cleared before a phase line that fails.
    turn = os.path.expanduser(f"~/.agents/approvals/turn/{RUN}phase")
    os.makedirs(turn, exist_ok=True)
    open(os.path.join(turn, "linear"), "w").close()
    said = phase_line("phase off spec", os.path.join(base, "removed-worktree"))
    assert said and "git failed" in said and not os.path.exists(os.path.join(turn, "linear")), said

    # Two sessions switching at once both keep their switch.
    switching = [subprocess.Popen(["python3", H + "prompt_approvals.py"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                  text=True, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
                 for _ in ("follow-pr", "audit", "second-model")]
    for process, phase in zip(switching, ("follow-pr", "audit", "second-model")):
        process.communicate(json.dumps({"prompt": f"phase off {phase} global", "session_id": RUN + phase, "cwd": a}))
    assert {"follow-pr", "audit", "second-model", "ci", "review"} <= set(stored_switches()["global.json"])

    store = os.path.join(PHASE_STORE, "global.json")
    assert guard(f"echo '{{}}' > {store}", a) == "deny", "the agent can't write a switch from the shell"
    for tool, tool_input in (("Write", {"file_path": store, "content": "{}"}),
                             ("Edit", {"file_path": store, "old_string": "off", "new_string": "on"})):
        wrote = run_hook("guard_files.py", {"tool_name": tool, "cwd": a, "tool_input": tool_input})
        assert wrote and wrote["hookSpecificOutput"]["permissionDecision"] == "deny", f"nor with {tool}"
    with open(store, "w") as fh:
        fh.write("{not json")
    said = phase_line("phase off verify", a)
    assert "Switched verify off" in said and "every phase counts as on" in said, \
        f"the switch is saved, and the message says another scope's file keeps every phase on: {said}"
    assert json.load(open(os.path.join(PHASE_STORE, "branch", "zz-phase-a", "feature.json")))["verify"] == "off"
    label = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "--refresh"], cwd=a, capture_output=True, text=True)
    assert "switches unreadable" in label.stdout, label.stdout + label.stderr
    with open(store, "w") as fh:
        fh.write('{"ci": "off", "a-phase-from-a-newer-kit": "off"}')
    assert phases_off(a)["ci"] == "global", "a phase this kit doesn't know is ignored, not an unreadable store"
    with open(store, "w") as fh:
        fh.write("{not json")
    done = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "switches"], cwd=a, capture_output=True, text=True)
    assert done.returncode == 1 and "Couldn't read" in done.stderr, done.stdout + done.stderr


def review_rounds(cwd):
    """`bin/phase review-rounds` as (rounds, where they're set)."""
    done = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "review-rounds"], cwd=cwd, capture_output=True,
                          text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    rounds, where = re.fullmatch(r"(\d) \((\w+)\)\n", done.stdout).groups()
    return int(rounds), where


def the_old_self_review_name_still_switches_the_review(base):
    a = new_repo(base, "zz-phase-alias")
    git(a, "switch", "-q", "-c", "feature")
    open(os.path.join(a, "app.py"), "a").write("y = 2\n")
    store = os.path.join(PHASE_STORE, "global.json")
    kept = open(store).read() if os.path.exists(store) else None
    os.makedirs(PHASE_STORE, exist_ok=True)

    def pr_gate():
        d = run_hook("guard_bash.py", {"tool_input": {"command": "gh pr create --fill"}, "cwd": a, "session_id": "test"})
        return d["hookSpecificOutput"]["permissionDecisionReason"] if d else ""

    def repo_file(switches):
        with open(repo_store, "w") as fh:
            json.dump(switches, fh)

    try:
        with open(store, "w") as fh:
            json.dump({"self-review": "off"}, fh)  # switched off before the phase was renamed
        assert phases_off(a) == {"review": "global"}, f"a stored self-review counts as review: {phases_off(a)}"
        assert "validation" in pr_gate(), f"a stored self-review: off lifts the review gate: {pr_gate()}"
        phase_line("phase on review", a)
        assert phases_off(a) == {}, "a branch's on under the new name overrides a global off under the old one"
        os.remove(os.path.join(PHASE_STORE, "branch", "zz-phase-alias", "feature.json"))
        repo_store = os.path.join(PHASE_STORE, "repo", "zz-phase-alias.json")
        os.makedirs(os.path.dirname(repo_store), exist_ok=True)
        repo_file({"self-review": "on", "review": "off", "verify": "off"})
        assert phases_off(a) == {"review": "repo", "verify": "repo"}, f"the new name wins within one file: {phases_off(a)}"
        repo_file({"self-review": "off", "review": "on"})
        assert phases_off(a) == {}, f"the new name wins, whichever way it points: {phases_off(a)}"
        repo_file({"self-review": "on", "review": "off", "verify": "off"})
        said = phase_line("phase on self-review repo", a)
        assert "Switched review on" in said, said
        assert json.load(open(repo_store)) == {"review": "on", "verify": "off"}, "one key per phase, the others kept"
        assert phases_off(a) == {"verify": "repo"}, phases_off(a)
        repo_file({"self-review": "on", "verify": "off"})
        phase_line("phase off review repo", a)
        assert json.load(open(repo_store)) == {"review": "off", "verify": "off"}, "the new name replaces the old key too"
    finally:
        if kept is None:
            os.remove(store)
        else:
            with open(store, "w") as fh:
                fh.write(kept)


def coming_back_after_hours_asks_for_the_status_first(base):
    repo = new_repo(base, "zz-return")
    git(repo, "switch", "-q", "-c", "feature")
    logs = os.path.join(base, "logs")
    env = {**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE, "HOME": base, "AGENTS_LOG_DIR": logs}
    now = time.time()

    def stamp(hours_ago, offset_hours=0):
        zone = datetime.timezone(datetime.timedelta(hours=offset_hours))
        at = datetime.datetime.fromtimestamp(now - hours_ago * 3600, zone)
        return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def claude(kind, text, hours_ago, offset_hours=0):
        return {"type": kind, "message": {"role": kind, "content": [{"type": "text", "text": text}]},
                "isSidechain": False, "timestamp": stamp(hours_ago, offset_hours)}

    def codex(event, hours_ago, **fields):
        return {"timestamp": stamp(hours_ago), "type": "event_msg", "payload": {"type": event, **fields}}

    def context(text, *entries, raw=None, codex_turn=False, missing=False, session="r1"):
        path = os.path.join(base, f"{session}.jsonl")
        if not missing:
            with open(path, "w", errors="surrogateescape") as fh:  # "\udcff" writes the byte 0xff
                fh.writelines(json.dumps(e) + "\n" for e in entries)
                fh.write(raw or "")
        payload = {"prompt": text, "session_id": RUN + session, "cwd": repo, "transcript_path": path,
                   "hook_event_name": "UserPromptSubmit", **({"turn_id": "turn-9"} if codex_turn else {})}
        done = subprocess.run(["python3", H + "prompt_approvals.py"], input=json.dumps(payload), capture_output=True,
                              text=True, timeout=10, env=env)
        assert done.returncode == 0 and not done.stderr, done.stderr
        return json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"] if done.stdout.strip() else None

    ask = "¿Está ya terminado?"
    ended = claude("assistant", "Blocked on me: merge #12 and deploy.", 8)
    told = context(ask, claude("user", "fix the report", 9), ended)
    assert told and "reports brief" in told and "Status" in told and "How sure" in told, told
    assert "--branch" in told and "unknown" in told, "where to read it when the checkout is gone, and what if it fails"
    assert context(ask, claude("assistant", "Done.", 7 + 59 / 60)) is None, "under the gap: nothing"
    harness_now = {"type": "attachment", "timestamp": stamp(0), "attachment": {"type": "hook_additional_context"}}
    assert context(ask, ended, claude("user", ask, 0), harness_now), \
        "the prompt already written, and harness entries written with it, don't end the absence"
    assert context(ask, claude("assistant", "Done.", 8, offset_hours=2)), "+02:00 is the same instant as Z"
    assert context(ask, claude("assistant", "Done.", 7.9, offset_hours=-5)) is None
    assert context(ask, codex("task_complete", 8, last_agent_message="Merge #12."), codex_turn=True)
    assert context(ask, codex("task_complete", 8, last_agent_message="Merge #12."),
                   codex("user_message", 0, message=ask), codex_turn=True), "Codex, the prompt already written"
    assert context(ask, codex("task_complete", 7.5, last_agent_message="Merge #12."), codex_turn=True) is None
    rollout_prompt = {"timestamp": stamp(0), "type": "response_item",
                      "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": ask}]}}
    assert context(ask, codex("task_complete", 8, last_agent_message="Merge #12."), rollout_prompt, codex_turn=True), \
        "a rollout writes the prompt as a response item"
    on_the_way_back = [claude("user", "<command-name>/model</command-name>", 0),
                       claude("user", "<local-command-stdout>Set model to opus</local-command-stdout>", 0),
                       claude("user", "<local-command-caveat>Caveat: …</local-command-caveat>", 0),
                       claude("user", "<command-message>model</command-message>\n<command-name>/model</command-name>", 0),
                       claude("user", "<bash-input>git status</bash-input>", 0),
                       claude("user", "<bash-stdout>ok</bash-stdout><bash-stderr></bash-stderr>", 0),
                       claude("user", "This session is being continued…", 0) | {"isCompactSummary": True}]
    assert context(ask, ended, *on_the_way_back), "a command run on the way back isn't the conversation"
    assert context(ask, claude("user", ask, 0), session="r-none") is None, "no entry before the prompt: no gap to read"
    assert any(e.get("decision") == "return-gap-unreadable" and e.get("session") == RUN + "r-none"
               for e in map(json.loads, open(os.path.join(logs, "hooks.jsonl")))), "and the log says why"
    for written in ([], [claude("user", ask, 0)]):
        assert context(ask, claude("assistant", "Done.", 9), claude("user", ask, 1), *written) is None, \
            "the same words sent an hour ago are the last entry, not this message"
        assert context(ask, codex("task_complete", 9, last_agent_message="Done."), codex("user_message", 1, message=ask),
                       *[codex("user_message", 0, message=ask)] * bool(written), codex_turn=True) is None
    assert context(ask, claude("assistant", "Done.", 7 + 59 / 60), claude("user", ask, 0)) is None
    assert context(ask, codex("task_complete", 7 + 59 / 60, last_agent_message="Merge #12."), codex_turn=True) is None
    assert context(ask, codex("task_complete", 7 + 59 / 60, last_agent_message="Merge #12."),
                   codex("user_message", 0, message=ask), codex_turn=True) is None
    side, meta = claude("assistant", "subagent", 0), claude("user", "<skill text>", 0)
    side["isSidechain"], meta["isMeta"] = True, True
    assert context(ask, ended, side, meta), "a subagent's or a skill's entries are not the conversation"
    assert context(ask, codex("task_complete", 8, last_agent_message="Merge #12."), codex("task_started", 0),
                   codex("token_count", 0), codex_turn=True), "events written as the new message arrives don't count"
    zoneless = claude("assistant", "Done.", 0)
    zoneless["timestamp"] = zoneless["timestamp"][:19]
    assert context(ask, ended, zoneless), "a time with no zone is skipped, not read as UTC or local"
    padding = [claude("assistant", "x" * 1000, 30) for _ in range(100)]
    assert context(ask, *padding, ended), "a transcript longer than the tail: read from a whole line"
    assert context(ask, *padding, claude("assistant", "Done.", 1)) is None

    for name, kwargs in [("missing", {"missing": True}), ("corrupt", {"raw": "{not json\n"}),
                         ("not-an-entry", {"raw": "[]\n"}), ("not-utf8", {"raw": "\udcff\n"}),
                         ("unreadable-message", {"raw": json.dumps(claude("assistant", "x", 0) | {"message": {"content": 42}})
                                                 + "\n"})]:
        repo = new_repo(base, "zz-return-" + name)
        git(repo, "switch", "-q", "-c", "feature")
        told = context("phase off validate\nCrea una tarea en Linear para esto", ended, session="r-" + name, **kwargs)
        assert told and "validate" in told and "reports brief" not in told, (name, told)
        got = run_hook("guard_mcp.py", {"tool_name": "mcp__linear__save_issue", "tool_input": {}, "cwd": repo,
                                        "session_id": RUN + "r-" + name}, env={"HOME": base, "AGENTS_LOG_DIR": logs})
        assert got is None, f"{name}: the approval still takes effect: {got}"
        lines = [json.loads(line) for line in open(os.path.join(logs, "hooks.jsonl"))]
        assert any(e.get("decision") == "return-gap-unreadable" and RUN + "r-" + name in json.dumps(e) for e in lines), \
            (name, lines[-3:])

    repo = new_repo(base, "zz-return-both")
    git(repo, "switch", "-q", "-c", "feature")
    told = context("phase off validate\nsigue", claude("assistant", "Done.", 9), session="r-both")
    assert "validate" in told and "reports brief" in told, f"both contexts: {told}"


def a_review_rounds_line_sets_the_re_checks_by_scope(base):
    a, b = new_repo(base, "zz-rounds-a"), new_repo(base, "zz-rounds-b")
    for repo in (a, b):
        git(repo, "switch", "-q", "-c", "feature")
    assert review_rounds(a) == (1, "default"), "one re-check when nobody set it"
    said = phase_line("review-rounds 3 global\nCarry on.", a)
    assert said.startswith("The user's `review-rounds` line: Switched review-rounds to 3 for every repo. "
                           "Review re-check rounds here now: 3 (global)."), said
    assert review_rounds(a) == review_rounds(b) == (3, "global")
    phase_line("Review-Rounds 0 repo", a)
    assert review_rounds(a) == (0, "repo") and review_rounds(b) == (3, "global"), "0 is a value, not unset"
    said = phase_line("review-rounds 9\r\nThanks", a)
    assert "for branch feature of zz-rounds-a" in said, f"a CRLF line still sets it, on the branch by default: {said}"
    assert review_rounds(a) == (9, "branch")
    git(a, "switch", "-q", "-c", "other")
    assert review_rounds(a) == (0, "repo"), "a branch setting stays on its branch"
    said = phase_line("review-rounds 5 global", a)
    assert "A narrower setting overrides it here. Review re-check rounds here now: 0 (repo)." in said, said
    assert phases_off(a) == {}, "a rounds setting switches no phase off"

    before = stored_switches()
    for text in ("please review-rounds 2", "review-rounds-2", "review rounds 2", "Notes:\nreview-rounds 2"):
        assert phase_line(text, b) is None, f"not a review-rounds line: {text!r}"
    for text, why in (("review-rounds 10", "`10` isn't a number of re-check rounds from 0 to 9"),
                      ("review-rounds -1", "`-1` isn't a number"), ("review-rounds ²", "isn't a number"),
                      ("review-rounds two", "`two` isn't a number"), ("review-rounds 2 everywhere", "Unknown scope"),
                      ("review-rounds", "Not `review-rounds <0-9> [branch|repo|global]`"),
                      ("review-rounds 2 repo now", "Not `review-rounds <0-9>")):
        said = phase_line(text, b)
        assert said and why in said and "Tell the user" in said, f"{text!r}: {said}"
    assert stored_switches() == before, "none of those wrote a setting"

    store = os.path.join(PHASE_STORE, "global.json")
    for bad in ('{"review-rounds": "on"}', '{"review-rounds": true}', '{"review-rounds": 10}', '{"spec": 2}'):
        with open(store, "w") as fh:
            fh.write(bad)
        done = subprocess.run([os.path.expanduser("~/.agents/bin/phase"), "review-rounds"], cwd=b, capture_output=True,
                              text=True)
        assert done.returncode == 1 and "Couldn't read the phase switches" in done.stderr, (bad, done.stdout, done.stderr)


def only_the_user_runs_the_switch_cli(base):
    """/flow off|on runs `phase_switches.py set`, the same code as the message line; the agent may not."""
    repo = new_repo(base, "zz-phase-cli")
    git(repo, "switch", "-q", "-c", "feature")
    for cmd in ["python3 ~/.agents/hooks/phase_switches.py set off validate", "python3 hooks/phase_switches.py set on spec repo",
                "cd /x && python3 '/Users/me/.agents/hooks/phase_switches.py' set off ci global",
                "bash -c 'python3 ~/.agents/hooks/phase_switches.py set off verify'",
                "~/.agents/bin/evidence A1 python3 ~/.agents/hooks/phase_switches.py set off audit",
                "python3 ~/.agents/hooks/phase_switches.py \\\n  set off spec", "python3 hooks/phase_switches.py 'set' off spec",
                "python3 hooks/phase_switches.py > /dev/null set off spec", "(python3 hooks/phase_switches.py set off x)",
                "cd ~/.agents/hooks && python3 -m phase_switches set off spec", "python3 -m hooks.phase_switches set off ci",
                "eval 'python3 hooks/phase_switches.py set off x'", "bash -lc 'python3 hooks/phase_switches.py set off x'",
                'echo "$(python3 hooks/phase_switches.py set off x)"', "bash -e -c 'python3 hooks/phase_switches.py set off x'",
                "bash <<'EOF'\npython3 hooks/phase_switches.py set off x\nEOF", "cat <<EOF | sh\npython3 hooks/phase_switches.py set off x\nEOF",
                "python3 ~/.agents/hooks/phase_switches.py set review-rounds 2 --global"]:
        assert guard(cmd, repo) == "deny", f"should deny: {cmd}"
    said = run_hook("guard_bash.py", {"tool_input": {"command": "python3 hooks/phase_switches.py set review-rounds 0"},
                                      "cwd": repo, "session_id": "test"})["hookSpecificOutput"]["permissionDecisionReason"]
    for form in ("`phase off|on", "`review-rounds <0-9>", "`/flow off|on", "`/flow review-rounds <0-9>"):
        assert form in said, (form, said)
    for cmd in ["python3 -m py_compile hooks/phase_switches.py", "grep -n 'set' hooks/phase_switches.py",
                "~/.agents/bin/phase switches", "git log --grep 'phase_switches set'", "echo phase_switches.py; set -e",
                "rg -n 'phase_switches.py set' hooks", 'git commit -m "Run phase_switches.py set from /flow"',
                "grep -c 'phase_switches.py set' tests/test_hooks.py",
                "git commit -m \"$(cat <<'EOF'\nDocument phase_switches.py set\nEOF\n)\""]:
        assert guard(cmd, repo) == "allow", f"should allow: {cmd}"

    def cli(*args, cwd=repo):
        return subprocess.run(["python3", H + "phase_switches.py", "set", *args], cwd=cwd, capture_output=True, text=True,
                              env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})

    done = cli("off", "validate", "repo")
    assert done.returncode == 0 and phases_off(repo) == {"validate": "repo"}, done.stdout
    said = done.stdout.strip()
    assert said.startswith("Switched validate off for every branch of zz-phase-cli. Phases off here now:"), said
    assert phase_line("phase off validate repo", repo) == (f"The user's `phase off` line: {said} Confirm it to the user in "
                                                           "one line, and skip the steps of the phases off."), "same switch, same facts"
    assert cli("on", "validate", "--repo").returncode == 0 and phases_off(repo) == {}, "the flag form /flow offers"
    done = cli("off", "second-model", "--global")
    assert done.returncode == 0 and phases_off(repo) == {"second-model": "global"}, done.stdout
    assert "second-model (global)" in done.stdout, done.stdout
    assert cli("on", "second-model", "--global").returncode == 0 and phases_off(repo) == {}
    done = cli("off", "self-review", "--repo")
    assert done.returncode == 0 and phases_off(repo) == {"review": "repo"}, f"/flow takes the former name: {done.stdout}"
    assert cli("on", "review", "--repo").returncode == 0 and phases_off(repo) == {}
    for reviewer in ("claude-review", "codex-review"):
        assert cli("off", reviewer, "--global").returncode == 0 and phases_off(repo) == {reviewer: "global"}, reviewer
        assert cli("on", reviewer, "--global").returncode == 0 and phases_off(repo) == {}
    outside = os.path.join(base, "not-a-repo-cli")
    os.makedirs(outside)
    try:
        refused = cli("off", "spec", "repo", cwd=outside)
        assert refused.returncode == 3 and "isn't in a git repository" in refused.stdout, refused.stdout
        assert cli("off", "spec", "--GLOBAL", cwd=outside).returncode == 0 and phases_off(repo) == {"spec": "global"}
    finally:
        cli("on", "spec", "global", cwd=outside)
    assert phases_off(repo) == {}
    done = cli("review-rounds", "2", "--repo")
    assert done.returncode == 0 and review_rounds(repo) == (2, "repo"), done.stdout
    assert done.stdout.strip() == ("Switched review-rounds to 2 for every branch of zz-phase-cli. "
                                   "Review re-check rounds here now: 2 (repo)."), done.stdout
    done = cli("review-rounds", "12")
    assert done.returncode == 3 and "`12` isn't a number" in done.stdout and review_rounds(repo) == (2, "repo"), done.stdout
    assert "`/flow review-rounds <0-9> [--repo|--global]`" in cli("review-rounds").stdout
    for args, why in ((["off"], "Not `/flow off <phase> [--repo|--global]`"), (["off", "create-pr"], "Unknown phase"),
                      (["off", "spec", "everywhere"], "Unknown scope"), (["off", "spec", "repo", "now"], "Not `/flow off"),
                      (["sideways", "spec"], "must be off, on or review-rounds")):
        done = cli(*args)
        # 3, not 1: a crash exits 1, and the mod must not read it as a refusal.
        assert done.returncode == 3 and why in done.stdout and phases_off(repo) == {}, (args, done.stdout, done.stderr)
        assert "Tell the user" not in done.stdout and "The user" not in done.stdout, "the CLI speaks to the user"
    assert cli().returncode == 2, "set with no action is a usage error"


def pr_gate_skips_the_phases_switched_off(base):
    repo = new_repo(base, "zz-phase-gate")
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")

    def reason(command):
        d = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": "test"})
        return d["hookSpecificOutput"]["permissionDecisionReason"] if d else None

    assert "No review recorded" in reason("gh pr create --fill")
    phase_line("phase off self-review", repo)
    assert "validation" in (reason("gh pr create --fill") or ""), "review off, by its old name, leaves the validate gate"
    phase_line("phase off validate", repo)
    assert reason("gh pr create --fill") is None, "both off: nothing to wait for"
    phase_line("phase on review", repo)
    assert "No review recorded" in (reason("gh pr create --fill") or ""), "back on, the gate needs a real stamp again"
    write_stamp(repo, "review")
    assert reason("gh pr create --fill") is None
    phase_line("phase on validate", repo)
    assert "validation" in (reason("gh pr create --fill") or ""), "back on, validate needs a real stamp again"
    write_stamp(repo, "validate")
    assert reason("gh pr create --fill") is None


    # The gate reads the switches of the checkout the PR is opened from, not the session's.
    other = new_repo(base, "zz-phase-other")
    git(other, "switch", "-q", "-c", "feature")
    open(os.path.join(other, "app.py"), "a").write("y = 2\n")
    phase_line("phase off review", other)
    phase_line("phase off validate", other)
    assert reason(f"cd {other} && gh pr create --fill") is None, "the other checkout's switches apply there"
    phase_line("phase off review", repo)
    phase_line("phase on review", repo)
    git(other, "worktree", "add", "-q", "-b", "feat2", os.path.join(base, "zz-phase-other-wt"))
    open(os.path.join(base, "zz-phase-other-wt", "app.py"), "a").write("w = 1\n")
    assert "No review recorded" in (reason(f"cd {os.path.join(base, 'zz-phase-other-wt')} && gh pr create --fill") or ""), \
        "a branch switch doesn't reach another branch of the same repo"

    # An unreadable file in one scope keeps every phase on, the readable ones' too, and the gate says why.
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    phase_line("phase off review", repo)
    phase_line("phase off validate", repo)
    assert reason("gh pr create --fill --draft") is None
    with open(os.path.join(PHASE_STORE, "global.json"), "w") as fh:
        fh.write("[")
    said = reason("gh pr create --fill --draft") or ""
    assert "No review recorded" in said and "Couldn't read" in said, f"an unreadable store means every phase on, said: {said}"


def stop_skips_the_phases_switched_off(base):
    repo = new_repo(base, "zz-phase-stop")
    git(repo, "switch", "-q", "-c", "feature")
    vdir = os.path.expanduser("~/.agents/repos/zz-phase-stop")
    os.makedirs(vdir)
    ran = os.path.join(base, "verify-ran")
    with open(os.path.join(vdir, "verify"), "w") as fh:
        fh.write(f"#!/bin/sh\necho x >> '{ran}'\necho 'ran: pytest'\necho 'FAILED test_app.py'\nexit 1\n")
    os.chmod(os.path.join(vdir, "verify"), 0o755)

    def verify_runs():
        return len(open(ran).read().splitlines()) if os.path.exists(ran) else 0
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/kit-spec/path.sh")], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    with open(spec, "w") as fh:
        fh.write("# X\n\nGoal: x.\n\n## Acceptance criteria\n1. y — verify: a test\n\n## Change map\n- Ways in: none\n")

    def asked(session, line):
        with open(os.path.join(repo, "app.py"), "a") as fh:
            fh.write(line)
        return stop(RUN + session, repo, [os.path.join(repo, "app.py")]).get("reason", "")

    reason = asked("p1", "y = 2\n")
    assert "FAILED" in reason and "Change map" in reason and verify_runs() == 1, f"both on: {reason}"
    phase_line("phase off verify", repo)
    reason = asked("p2", "breakpoint()\n")
    assert "FAILED" not in reason and verify_runs() == 1, f"verify off: it doesn't run: {reason}"
    assert "Change map" in reason and "Debug leftover" in reason, f"spec on, and the other checks run: {reason}"
    done = subprocess.run(["python3", H + "stop_checks.py", "verify"], cwd=repo, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env={**os.environ, "AGENTS_TEST": "1", "AGENTS_STATE_DIR": STATE})
    assert verify_runs() == 2, f"run by hand, verify runs whatever the switch: {done.stdout}{done.stderr}"
    phase_line("phase on verify", repo)
    phase_line("phase off spec", repo)
    reason = asked("p3", "<<<<<<< HEAD\n")
    assert "Change map" not in reason and "FAILED" in reason and verify_runs() == 3, f"spec off, verify on: {reason}"
    assert "conflict marker" in reason, f"the other checks run whatever is off: {reason}"


def a_stacked_branch_is_measured_against_its_parent(base):
    repo = new_repo(base, "zz-stacked", branch="main")
    remote = os.path.join(base, "zz-stacked.git")
    git(base, "init", "-q", "--bare", remote)
    git(repo, "remote", "add", "origin", remote)
    git(repo, "push", "-q", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")  # as a clone has it

    def commit(path, text):
        open(os.path.join(repo, path), "w").write(text)
        git(repo, "add", path)
        git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", path)

    def base_of():
        done = subprocess.run(["python3", H + "review_stamp.py", "base"], cwd=repo, capture_output=True, text=True)
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout.strip()

    def changed():
        done = subprocess.run(["python3", "-c", f"import sys; sys.path.insert(0, {H!r}); import review_stamp; "
                               "print(' '.join(review_stamp.changed_paths()[1]))"], cwd=repo, capture_output=True, text=True)
        return done.stdout.split()

    def leftover(session):
        return "Debug leftover in parent.py" in stop(RUN + session, repo, [os.path.join(repo, "child.py")]).get("reason", "")

    git(repo, "switch", "-q", "-c", "parent")
    commit("parent.py", "breakpoint()\n")
    git(repo, "switch", "-q", "-c", "child")
    commit("child.py", "c = 1\n")
    assert base_of() == "origin/main" and changed() == ["child.py", "parent.py"], "a local-only parent is no evidence"
    assert leftover("k1"), "without a parent the stop hook reads the parent's lines too"

    git(repo, "push", "-q", "origin", "parent")
    assert base_of() == "origin/parent", "a pushed branch in this branch's history is its parent"
    assert changed() == ["child.py"], f"the stamps cover what the child adds: {changed()}"
    assert not leftover("k2"), "nor does the stop hook read the parent's lines"
    triaged = subprocess.run([os.path.join(KIT, "bin", "triage"), "--json"], cwd=repo, capture_output=True, text=True)
    result = json.loads(triaged.stdout)
    assert result["files"] == 1 and result["base"] == "origin/parent", triaged.stdout + triaged.stderr
    said = subprocess.run([os.path.join(KIT, "bin", "triage")], cwd=repo, capture_output=True, text=True).stdout
    assert "Base: origin/parent (" in said, f"triage says which base it used: {said}"

    git(repo, "push", "-q", "-u", "origin", "child")
    commit("child2.py", "d = 1\n")
    assert base_of() == "origin/parent", "the branch's own pushed copy is not its parent"
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/backup")
    assert base_of() == "origin/parent", "a pushed branch at HEAD would leave nothing to review"

    def pr_gate(command):
        said = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": "test"})
        return said["hookSpecificOutput"]["permissionDecisionReason"] if said else ""
    target = "targets `main`, but its stamps measure the change against `origin/parent`"
    assert target in pr_gate("gh pr create --title t --body b"), "a PR without --base targets the default branch"
    assert target in pr_gate("gh pr create --base main --title t --body b")
    assert "git config branch.child.agentsBase main" in pr_gate("gh pr create -B main --title t --body b")
    assert target in pr_gate("gh pr create --base parent --base main --title t"), "gh takes the last --base"
    git(repo, "config", "branch.child.gh-merge-base", "parent")
    assert "No review recorded" in pr_gate("gh pr create --title t"), "without --base, gh targets gh-merge-base"
    git(repo, "config", "--unset", "branch.child.gh-merge-base")
    for matching in ("gh pr create --base parent --title t --body b", "gh pr create -B origin/parent --title t",
                     "gh pr create --base=parent --title t", "gh pr create -Bparent --title t",
                     "gh pr create -B=parent --title t", "gh pr create --base parent --title '-Better checks'",
                     "gh pr create --base parent --body '--base=main' -t t"):
        assert "No review recorded" in pr_gate(matching), f"the measured base passes the target check: {matching}"

    assert write_stamp(repo, "review").returncode == 0
    git(repo, "config", "branch.child.agentsBase", "main")
    assert base_of() == "origin/main" and "parent.py" in changed(), "a recorded base wins over the pushed parent"
    assert leftover("k3"), "and the stop hook reads from it"
    stale = subprocess.run(["python3", H + "review_stamp.py", "check"], cwd=repo, capture_output=True, text=True)
    assert stale.returncode == 1, "recording another base makes stamps measured against the old one stale"
    triaged = json.loads(subprocess.run([os.path.join(KIT, "bin", "triage"), "--json"], cwd=repo, capture_output=True,
                                        text=True).stdout)
    assert triaged["base"] == "origin/main" and triaged["files"] == 3, triaged

    git(repo, "config", "branch.child.agentsBase", "parent")
    assert base_of() == "origin/parent", "a recorded branch name is read as its origin copy"
    said = pr_gate("gh pr create --base parent --title t")
    assert "has no validation recorded" in said, f"the record names the target, and the review measured against it counts: {said}"
    for ignored, why in (("gone", "doesn't resolve"), ("child", "names the branch itself"),
                         ("origin/child", "names its own pushed copy"), ("refs/heads/child", "names the branch itself"),
                         ("refs/remotes/origin/child", "names its own pushed copy"), ("HEAD", "names the branch itself"),
                         ("island", "shares no history")):
        if ignored == "island":
            git(repo, "switch", "-q", "--orphan", "island")
            commit("island.py", "i = 1\n")
            git(repo, "switch", "-q", "child")
        git(repo, "config", "branch.child.agentsBase", ignored)
        assert base_of() == "origin/parent", f"a record that {why} is ignored, and detection applies"

    git(repo, "config", "branch.child.agentsBase", "main")
    git(repo, "branch", "-m", "child", "renamed")
    assert base_of() == "origin/main", "the record follows `git branch -m`"
    git(repo, "switch", "-q", "--detach")
    assert base_of() == "origin/main", "a detached HEAD has no parent to find"
    git(repo, "switch", "-q", "-c", "tracked", "--track", "origin/parent")
    commit("tracked.py", "t = 1\n")
    assert base_of() == "origin/parent", "a branch started from its pushed parent tracks it, which is evidence, not its own copy"
    git(repo, "config", "branch.tracked.agentsBase", "parent")
    assert base_of() == "origin/parent", "and recording that parent still counts"
    git(repo, "branch", "feat/p", "origin/parent")
    git(repo, "remote", "add", "feat", remote)  # a remote named like the branch's prefix doesn't make it remote
    git(repo, "config", "branch.tracked.agentsBase", "feat/p")
    assert base_of() == "feat/p", "a recorded local branch, with no pushed copy, is the base"
    said = pr_gate("gh pr create --base p --title t")
    assert "against `feat/p`" in said and "pass `--base feat/p`" in said, f"only a remote's name is stripped: {said}"
    assert "No review recorded" in pr_gate("gh pr create --base feat/p --title t")
    git(repo, "switch", "-q", "main")
    git(repo, "branch", "-q", "-D", "renamed")
    assert git(repo, "config", "--get-regexp", r"branch\.renamed\.").stdout == "", "and goes with `git branch -D`"


TESTS = [a_stacked_branch_is_measured_against_its_parent, a_phase_line_switches_phases_by_scope, the_old_self_review_name_still_switches_the_review, coming_back_after_hours_asks_for_the_status_first, a_review_rounds_line_sets_the_re_checks_by_scope, only_the_user_runs_the_switch_cli, pr_gate_skips_the_phases_switched_off, stop_skips_the_phases_switched_off,
         guard_blocks_irreversible, only_the_user_grants_an_approval_in_a_dialog,
         the_agent_stays_out_of_the_approvals,
         guard_allows_routine, guard_resolves_rm_variables, guard_reads_code_not_text, guard_command_starts_stay_on_their_line, guard_reads_through_wrappers_and_scripts, guard_mcp_linear, asking_for_a_service_approves_its_writes_for_that_turn, a_broken_profile_blocks_mcp_calls_instead_of_letting_them_through, the_hook_log_keeps_secrets_out, a_turn_approval_that_cant_be_removed_is_moved_aside, an_answer_to_the_agents_offer_approves_the_service_it_named, a_block_says_when_no_approval_dialog_can_show,
         a_write_to_an_object_the_user_approved_is_approved_for_the_session, an_allow_line_approves_a_database_statement_for_that_turn,
         a_plain_request_approves_a_database_statement_for_that_turn, a_message_asking_to_merge_approves_gh_pr_merge_for_that_turn,
         a_message_asking_to_push_approves_that_protected_branch, a_first_push_to_an_empty_remote_needs_no_approval, guard_mcp_logs_browser_mcp, pr_gate_review_and_validation,
          stamps_survive_merging_the_default_branch, validate_stamp_needs_evidence, unrun_checks_never_hold_the_pr, pr_gate_follows_worktrees, guard_fails_closed, codex_pr_commands_name_their_checkout, reports_survive_worktree_removal, reports_brief_gives_one_line_per_report, reports_reads_another_branch, overlay_found_from_worktree_with_another_name, overlay_edited_while_it_runs_still_runs_as_it_started, stop_catches_leftovers_in_worktree, stop_checks_edits_after_its_directory_is_removed, stop_skips_a_checkout_removed_while_checked, stop_starts_each_profile_after_turn, stop_never_waits_for_profile_after_turn, stop_logs_an_after_turn_that_cannot_start, hook_log_names_the_suite_run_only_inside_one, stop_catches_committed_leftover,
          stop_falls_back_to_auto_verify, stop_continues_only_once,
          stop_flags_secrets_redacted, stop_flags_marked_override_only,
          stop_finds_override_in_primary_checkout_and_health_finds_it_later, verify_stamp_and_effort_nudge,
          stop_asks_once_about_files_outside_the_change_map, stop_follows_ci_after_a_push, stop_suggests_a_fresh_session_once_the_pr_is_open, stop_suggests_a_fresh_session_for_the_next_change, guard_records_the_pushed_checkout,
          post_edit_syntax_feedback, impeccable_files_in_a_shared_repo_need_the_user_first, impeccable_guard_edge_cases, impeccable_guard_reads_prose_as_text]

if sys.argv[1:]:
    by_name = {t.__name__: t for t in TESTS}
    for name in sys.argv[1:]:
        test(by_name[name])
else:
    # Each test in a process of its own, as every test already gets its own HOME, hook state and session ids there:
    # threads would share the HOME's repo overlays and logs.
    def run_alone(t):
        done = subprocess.run([sys.executable, os.path.abspath(__file__), t.__name__], capture_output=True, text=True,
                              errors="replace")
        # Passed only when it says so and exits 0: a test that exits early, 0 or not, never reported.
        passed = done.returncode == 0 and f"ok   {t.__name__}\n" in done.stdout
        if passed:
            return True, done.stdout
        said = "" if f"FAIL {t.__name__}" in done.stdout else f"FAIL {t.__name__}\n     exit {done.returncode}, no result\n"
        return False, said + done.stdout + done.stderr
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 2)) as pool:
        outcomes = list(pool.map(run_alone, TESTS))
    shutil.rmtree(STATE, ignore_errors=True)
    shutil.rmtree(os.environ["HOME"], ignore_errors=True)
    print("".join(out for _, out in outcomes), end="")
    failed = sum(not passed for passed, _ in outcomes)
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)

shutil.rmtree(STATE, ignore_errors=True)
shutil.rmtree(os.environ["HOME"], ignore_errors=True)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
sys.exit(1 if failed else 0)
