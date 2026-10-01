#!/usr/bin/env python3
"""Regression tests for the kit's hooks. Each test builds throwaway git repos in a temp dir.

Run: python3 ~/.agents/tests/test_hooks.py   (exit 1 on any failure)
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

# A HOME of our own: the tests add repo overlays and write hook logs, and must never touch the real ones.
KIT = os.path.realpath(os.path.expanduser("~/.agents"))
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-hooks-home-")
os.environ.pop("EVIDENCE_DIR", None)  # set when a staging step runs this suite: the tests' evidence would land there
os.makedirs(os.path.expanduser("~/.agents/repos"))
for entry in set(os.listdir(KIT)) - {"logs", "repos", "approvals"}:
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
    started = time.time()
    assert guard(";" * 60000 + "x/" * 30000) == "allow" and time.time() - started < 3, "a long line must not stall the guard"
    # An edit chained before a blocked step was lost without a word: the agent took it as done.
    d = run_hook("guard_bash.py", {"tool_input": {"command": "sed -i '' s/a/b/ notes.md && git push --force origin x"},
                                   "cwd": "/tmp", "session_id": "test"})
    assert "Nothing in this command ran" in d["hookSpecificOutput"]["permissionDecisionReason"], d


def guard_allows_routine(base):
    for cmd in ["git status", "git push -u origin feature/x", "git push --force-with-lease origin feature/x",
                "git branch -d old", "rm -rf /tmp/foo", "gh pr create --help", "make phpunit", "npm run build",
                "echo 'it would truncate the list'"]:
        assert guard(cmd) == "allow", f"should allow: {cmd}"


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


def guard_mcp_logs_browser_mcp(base):
    log = os.path.expanduser("~/.agents/logs/hooks.jsonl")
    for tool in ("mcp__playwright-headless__browser_navigate", "mcp__claude-in-chrome__navigate", "mcp__linear__get_issue"):
        got = run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": {}, "cwd": "/tmp", "session_id": "test"})
        assert got is None, f"{tool} must never be blocked: {got}"
    logged = [json.loads(l)["detail"] for l in open(log) if '"browser-mcp"' in l and '"session": "test"' in l]
    assert logged[-2:] == ["mcp__playwright-headless__browser_navigate", "mcp__claude-in-chrome__navigate"], logged[-3:]


# --- stamps and the PR gate ---------------------------------------------------------------------
def pr_gate_review_and_validation(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feature")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    assert guard("gh pr create --fill", repo) == "deny"
    assert guard(f"cd {repo} && GH_HOST=x gh pr create --fill", repo) == "deny"
    assert guard("cd no-such-dir; gh pr create --fill", repo) == "deny", "a failed cd can't crash the guard open"
    assert guard("sh -c $'gh pr create --fill'", repo) == "deny", "sh -c $'...'"
    assert guard("""python3 -c 'print("gh pr create")'""", repo) == "allow", "phrase inside a string is not a PR"
    assert guard('grep -n "gh pr create" hooks/guard_bash.py', repo) == "allow"
    assert guard('grep -n "guard_bash\\|gh pr create" bin/docs', repo) == "allow", "a | inside a pattern starts no command"
    assert guard("cat > notes.md <<'EOF'\nRun gh pr create\n| gh pr create --fill\nEOF", repo) == "allow", "heredoc text"
    assert guard("cat > notes.md <<'EOF'\ntext\nEOF\ngh pr create --fill", repo) == "deny", "a real one after a heredoc"
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
            "exec -aSlip gh pr create --fill", "env -uGH_TOKEN gh pr create --fill"]
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
    started = time.time()
    assert guard(long_env, repo) == "allow" and time.time() - started < 3, "a long env line must not stall the guard"
    write_stamp(repo, "review")
    assert guard("gh pr create --fill", repo) == "deny", "behavior change needs validation too"
    assert guard(f"cd {repo} && ~/.agents/bin/evidence A1 gh pr create --fill", repo) == "deny", "the PR gate sees through the runner"
    assert guard("env X=1 evidence A1 gh pr create --fill", repo) == "deny"
    write_stamp(repo, "validate")
    assert guard("gh pr create --fill", repo) == "allow"
    open(os.path.join(repo, "CHANGELOG.md"), "w").write("- A line (#12).\n")
    assert guard("gh pr create --fill", repo) == "allow", "the changelog line CI checks doesn't invalidate the stamps"
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
    elsewhere = os.path.join(base, "staging-evidence")
    os.makedirs(elsewhere)
    outside = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "S1", "echo", "from staging"], cwd=base,
                             capture_output=True, text=True, env={**os.environ, "EVIDENCE_DIR": elsewhere})
    assert outside.returncode == 0 and "from staging" in open(os.path.join(elsewhere, "S1.txt")).read(), outside.stderr
    unset_dir = subprocess.run([os.path.expanduser("~/.agents/bin/evidence"), "S2", "true"], cwd=base, capture_output=True, text=True,
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
                 "| B1 | page | + | FAIL | ![page](evidence-x/b1.png) |\n")
    done = write_validate()
    assert done.returncode == 0 and stamped(), done.stderr
    listed = subprocess.run([reports], cwd=repo, capture_output=True, text=True).stdout
    assert "A1.txt" in listed and "b1.png" in listed and "\x89PNG" not in listed, listed


def pr_gate_waits_for_staging(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/staged")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
    for kind in ("review", "validate"):
        write_stamp(repo, kind)
    reports = os.path.expanduser("~/.agents/bin/reports")
    guide = subprocess.run([reports, "path", "staging-guide"], cwd=repo, capture_output=True, text=True).stdout.strip()
    evidence = subprocess.run([reports, "path", "evidence"], cwd=repo, capture_output=True, text=True).stdout.strip()
    fake = os.path.join(base, "fake-gh")
    os.makedirs(fake)
    with open(os.path.join(fake, "gh"), "w") as fh:
        fh.write("#!/bin/sh\n[ \"$1 $2 $3\" = \"pr view 12\" ] && echo feat/staged\n"
                 "[ \"$1 $2 $3\" = \"pr view 13\" ] && echo someone-else\nexit 0\n")
    os.chmod(os.path.join(fake, "gh"), 0o755)

    def reason(command):
        d = run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": "test"},
                     env={"PATH": fake + ":" + os.environ["PATH"]})
        return d["hookSpecificOutput"]["permissionDecisionReason"] if d else None

    def decision(command):
        return "deny" if reason(command) else "allow"

    steps = "# Guide\n## Before the merge\n### S1. Deploy\n### S2. Check\n## After the merge\n### P1. Backfill\n"
    head = "\n## Results (2026-09-30)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
    with open(guide, "w") as fh:
        fh.write(steps)
    for command in ["gh pr create --fill", "gh pr ready", "gh pr ready 12", f"cd {repo} && ~/.agents/bin/evidence A1 gh pr ready",
                    "gh pr create --fill; gh pr create --draft --head other", "gh pr ready --undo && gh pr ready 12",
                    f"cd {repo} && gh pr ready && cd /tmp", "if true; then gh pr ready; fi", "{ gh pr ready; }",
                    "gh pr ready 12 --repo other/repo", "gh pr -R other/repo ready 12", 'gh pr ready "12"',
                    "gh -R other/repo pr ready 12", "gh pr ready -Rother/repo 12",
                    'gh pr create --head "$(git branch --show-current)" --title "Allow --draft PRs" --fill',
                    "gh pr create --fill # --draft", "gh pr ready 12 # --undo"]:
        assert decision(command) == "deny", f"staging is pending: {command}"
    gone = run_hook("guard_bash.py", {"tool_input": {"command": "gh pr ready"}, "cwd": os.path.join(base, "gone"),
                                      "session_id": "test"}, env={"PATH": fake + ":" + os.environ["PATH"]})
    assert gone and "doesn't exist" in gone["hookSpecificOutput"]["permissionDecisionReason"], "a crash would allow"
    for heading in ("## Before the merge \n", "## Before the merge ##\n"):
        with open(guide, "w") as fh:
            fh.write(steps.replace("## Before the merge\n", heading))
        assert decision("gh pr ready") == "deny", f"steps under {heading!r} still count"
    with open(guide, "w") as fh:
        fh.write(steps)
    for command in ["gh pr create --draft --fill", "gh pr create -d --fill", "gh pr ready --undo", "gh pr view 12"]:
        assert decision(command) == "allow", command
    with open(os.path.join(evidence, "S1.txt"), "w") as fh:
        fh.write("$ deploy\nexit 0\n")
    with open(guide, "w") as fh:
        fh.write(steps + head + "| S1 | PASS | [out](e/S1.txt) |\n| S2 | PASS | looked fine |\n")
    assert decision("gh pr ready") == "deny", "a PASS with no saved evidence"
    with open(os.path.join(evidence, "S2.txt"), "w") as fh:
        fh.write("$ check\nexit 0\n")
    with open(guide, "w") as fh:
        fh.write(steps + "\n## Results (2026-09-30)\n\n| Step | Result | Observation |\n|---|---|---|\n"
                 "| S1 | PASS | S1.txt |\n| S2 | PASS | S2.txt |\n")
    assert decision("gh pr ready") == "deny", "a results table without an Evidence column proves nothing"
    with open(guide, "w") as fh:
        fh.write(steps + head + "| S1 | PASSING | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n")
    assert decision("gh pr ready") == "deny", "PASSING isn't PASS"
    with open(guide, "w") as fh:
        fh.write(steps + head + "| S1 | PASS | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n")
    for command in ["gh pr create --fill", "gh pr ready", "gh pr ready 12", 'gh pr ready "12"', "if true; then gh pr ready; fi",
                    "gh pr ready 2>&1", "gh pr ready > ready.log", "gh pr ready 12 2> err.log", "gh pr ready 12; gh pr ready 12"]:
        assert decision(command) == "allow", f"every S step passed with evidence: {command}"
    for command in ["gh pr ready 12 --repo o/r", "gh pr -R o/r ready 12", "gh -R o/r pr ready 12", "gh pr ready -Ro/r 12"]:
        assert "--repo" in (reason(command) or ""), f"staging passed, but not checked for that repo: {command}"
    assert "someone-else" in (reason('gh pr ready "13"') or ""), "a quoted target is still the target"
    assert "unknown" in (reason("gh pr ready 99") or ""), "a PR gh can't find"
    assert "One `gh pr ready`" in (reason("gh pr ready 12; gh pr ready 13") or ""), "one lookup per command"
    with open(guide, "a") as fh:
        fh.write("\n## Results after the deploy (2026-10-01)\n\n| Step | Result | Evidence |\n|---|---|---|\n"
                 "| P1 | PASS | [out](e/S1.txt) |\n")
    assert decision("gh pr ready") == "allow", "ship's table after the deploy isn't the staging results"
    for rows, expected in [("| S1 | FAIL | [out](e/S1.txt) |\n| S1 | PASS | [out](e/S1.txt) |\n", "allow"),
                           ("| S1 | PASS | [out](e/S1.txt) |\n| S1 | FAIL | [out](e/S1.txt) |\n", "deny")]:
        with open(guide, "w") as fh:
            fh.write(steps + head + rows + "| S2 | PASS | [out](e/S2.txt) |\n")
        assert decision("gh pr ready") == expected, f"a step's last row decides: {rows!r}"
    with open(guide, "wb") as fh:
        fh.write(b"# Guide\n## Before the merge\n### S1. \xff\xfe\n")
    assert "Couldn't read the staging results" in (reason("gh pr ready") or ""), "a check that crashes holds the PR"
    with open(guide, "w") as fh:
        fh.write("# Guide\n## Before the merge\nNothing staging can prove.\n## After the merge\n### P1. Backfill\n")
    assert decision("gh pr ready") == "allow", "only after-merge steps"
    with open(guide, "w") as fh:
        fh.write("# Guide\n## After the merge\n### P1. Backfill\n")
    assert decision("gh pr ready") == "allow", "no Before-the-merge part at all"
    if os.geteuid() != 0:  # root reads it anyway
        os.chmod(guide, 0)
        try:
            assert decision("gh pr ready") == "deny", "a guide that can't be read isn't a guide that passed"
        finally:
            os.chmod(guide, 0o644)
    assert decision("gh pr ready 13") == "deny", "another branch's PR can't be checked from this checkout"
    os.remove(guide)
    assert decision("gh pr create --fill") == "allow", "no guide, no staging"


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
    assert "33986" in codex("gh pr ready 12"), "marking ready checks the checkout too"
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
    guide = subprocess.run([reports, "path", "staging-guide"], cwd=wt, capture_output=True, text=True).stdout.strip()
    open(guide, "w").write("# Staging guide\n")
    git(main, "worktree", "remove", wt)
    git(main, "switch", "-q", "feat/z")
    shown = subprocess.run([reports], cwd=main, capture_output=True, text=True).stdout
    assert "# Staging guide" in shown, f"guide written at {guide} is gone after removing the worktree"
    stamped = subprocess.run(["python3", H + "review_stamp.py", "check"], cwd=main).returncode == 0
    assert stamped, "the self-review stamp must survive the hand-off so the PR can be opened from the main checkout"


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


def stop_catches_leftovers_in_worktree(base):
    main = new_repo(base, "main")
    wt = os.path.join(base, "wt")
    git(main, "worktree", "add", "-q", "-b", "feat/y", wt, "trunk")
    os.makedirs(os.path.join(wt, "tests"))
    open(os.path.join(wt, "app.py"), "a").write("breakpoint()\n")
    open(os.path.join(wt, "tests", "test_a.py"), "w").write("import pytest\n@pytest.mark.skip\ndef test_a(): pass\n")
    reason = stop(RUN + "s1", main, [os.path.join(wt, "app.py"), os.path.join(wt, "tests", "test_a.py")]).get("reason", "")
    assert "Debug leftover" in reason and "Skipped or focused test" in reason, reason


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
        verify_by_hand()
        assert stamped("verify-empty") and not stamped("verify"), "the latest run's stamp is the only one left"
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
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/spec/path.sh")], cwd=repo, capture_output=True,
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


def guard_records_the_pushed_checkout(base):
    repo, other = new_repo(os.path.join(base, "a")), new_repo(os.path.join(base, "b c"))
    pushed = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks", f"{RUN}push.pushed")

    def recorded(command):
        if os.path.exists(pushed):
            os.remove(pushed)
        run_hook("guard_bash.py", {"tool_input": {"command": command}, "cwd": repo, "session_id": RUN + "push"})
        return sorted({line.split("\t")[0] for line in open(pushed).read().splitlines()}) if os.path.exists(pushed) else []

    here, there = os.path.realpath(repo), os.path.realpath(other)
    assert recorded("git push") == [here]
    assert recorded(f'cd "{other}" && git push -u origin HEAD') == [there]
    assert recorded(f'git --no-pager -C "{other}" push') == [there]
    assert recorded(f'git push; cd "{other}"; git push') == sorted([here, there])
    assert recorded(f"  cd {other.replace(' ', chr(92) + ' ')} && git push") == [there]
    assert recorded(f'(cd "{other}" && git status); git push') == [here], "a subshell's cd stays in the subshell"
    assert recorded("git status && echo 'git push'") == [], "a push only in quoted text isn't one"


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


for t in [guard_blocks_irreversible, guard_allows_routine, guard_mcp_linear, asking_for_a_service_approves_its_writes_for_that_turn, guard_mcp_logs_browser_mcp, pr_gate_review_and_validation,
          validate_stamp_needs_evidence, pr_gate_waits_for_staging, pr_gate_follows_worktrees, guard_fails_closed, codex_pr_commands_name_their_checkout, reports_survive_worktree_removal, overlay_found_from_worktree_with_another_name, stop_catches_leftovers_in_worktree, stop_catches_committed_leftover,
          stop_falls_back_to_auto_verify, stop_continues_only_once,
          stop_flags_secrets_redacted, stop_flags_marked_override_only,
          stop_finds_override_in_primary_checkout_and_health_finds_it_later, verify_stamp_and_effort_nudge,
          stop_asks_once_about_files_outside_the_change_map, stop_follows_ci_after_a_push, guard_records_the_pushed_checkout,
          post_edit_syntax_feedback]:
    test(t)

shutil.rmtree(STATE, ignore_errors=True)
shutil.rmtree(os.environ["HOME"], ignore_errors=True)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
