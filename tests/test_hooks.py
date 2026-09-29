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
            "eval 'gh pr create --fill'", "bash -lc 'gh pr create --fill'", "sh -c \"cd x && gh pr create --fill\""]
    for command in runs:
        assert guard(command, repo) == "deny", f"the shell runs gh here: {command!r}"
    inert = ["cat <<EOF\n EOF\ngh pr create --fill\nEOF", "cat <<'END-MARK'\ngh pr create --fill\nEND-MARK",
             "echo 'a | gh pr create'", "cat <<-EOF\n\tgh pr create\n\tEOF",
             "python3 - <<'PY'\ns = '`x`|gh pr create $(y)'\nPY", "python3 - <<'PY'\ns = \"bash -lc 'gh pr create'\"\nPY",
             "grep -n 'eval `gh pr create`' notes.md","grep -n '`gh pr create`' notes.md",
             'echo "\\`gh pr create\\`"']
    for command in inert:
        assert guard(command, repo) == "allow", f"nothing runs gh here: {command!r}"
    subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", "review"], cwd=repo, capture_output=True)
    assert guard("gh pr create --fill", repo) == "deny", "behavior change needs validation too"
    subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", "validate"], cwd=repo, capture_output=True)
    assert guard("gh pr create --fill", repo) == "allow"
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    assert guard("gh pr create --fill", repo) == "deny", "edit after stamps must invalidate them"


def pr_gate_follows_worktrees(base):
    main = new_repo(base, "main")
    wt = os.path.join(base, "wt")
    git(main, "switch", "-q", "-c", "other")
    git(main, "worktree", "add", "-q", "-b", "feat/x", wt, "trunk")
    open(os.path.join(wt, "app.py"), "a").write("y = 2\n")
    for kind in ("review", "validate"):
        subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", kind], cwd=wt, capture_output=True)
    assert guard(f"cd {wt} && gh pr create --fill", main) == "allow"
    assert guard("gh pr create --head feat/x --fill", main) == "allow"


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
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\necho failing; exit 1\n")
        outs = [stop(RUN + "s7", repo, [os.path.join(repo, "app.py")]) for _ in range(2)]
        assert "systemMessage" in outs[1] and "systemMessage" not in outs[0], outs
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def stop_asks_once_about_files_outside_the_change_map(base):
    repo = new_repo(base, "zz-agents-drift")
    git(repo, "checkout", "-q", "-b", "feat/drift")
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/spec/path.sh")], cwd=repo, capture_output=True,
                          text=True).stdout.strip()
    with_map = ("# Drift\n\nGoal: x.\n\n## Acceptance criteria\n1. y — verify: tests/test_app.py\n\n"
                "## Change map\n- Ways in: the app — app.py:1\n\n## Assumptions\n- lib/util.py stays as it is.\n")
    open(spec, "w").write(with_map)
    for path in ("lib/util.py", "other/app.py", "tests/test_app.py"):
        os.makedirs(os.path.join(repo, os.path.dirname(path)), exist_ok=True)
        open(os.path.join(repo, path), "w").write("x = 1\n")
    open(os.path.join(repo, "app.py"), "a").write("y = 2\n")

    def asked(session, edited):
        reason = stop(RUN + session, repo, [os.path.join(repo, p) for p in edited]).get("reason", "")
        return sorted(line.split("`")[1] for line in reason.splitlines() if "Change map doesn't name" in line)
    everything = ["app.py", "lib/util.py", "other/app.py", "tests/test_app.py"]
    assert asked("d1", everything) == ["lib/util.py", "other/app.py"], \
        "unmapped code files, including one named only in Assumptions and a same-named file elsewhere"
    assert asked("d1", everything) == [], "each file is asked about once per session"
    open(os.path.join(repo, "cache.py"), "w").write("z = 3\n")
    assert asked("d1", ["cache.py"]) == ["cache.py"], "a file that drifts later is asked about"
    open(spec, "w").write(with_map.replace("## Change map\n- Ways in: the app — app.py:1\n\n", ""))
    assert asked("d2", everything) == [], "a spec without a Change map asks nothing"


def post_edit_syntax_feedback(base):
    repo = new_repo(base)
    bad = os.path.join(repo, "bad.py")
    open(bad, "w").write("def f(:\n")
    d = run_hook("post_edit.py", {"session_id": RUN + "s8", "cwd": repo, "tool_input": {"file_path": bad}})
    assert d and d.get("decision") == "block", d


for t in [guard_blocks_irreversible, guard_allows_routine, guard_mcp_linear, asking_for_a_service_approves_its_writes_for_that_turn, guard_mcp_logs_browser_mcp, pr_gate_review_and_validation,
          pr_gate_follows_worktrees, reports_survive_worktree_removal, overlay_found_from_worktree_with_another_name, stop_catches_leftovers_in_worktree, stop_catches_committed_leftover,
          stop_falls_back_to_auto_verify, stop_continues_only_once,
          stop_flags_secrets_redacted, stop_flags_marked_override_only,
          stop_finds_override_in_primary_checkout_and_health_finds_it_later, verify_stamp_and_effort_nudge,
          stop_asks_once_about_files_outside_the_change_map,
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
