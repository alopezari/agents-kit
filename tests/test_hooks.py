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

H = os.path.expanduser("~/.agents/hooks/")
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
                "mysql -e 'drop table wp_x'", "touch ~/.agents/approvals/linear", "rm -rf ~/Projects"]:
        assert guard(cmd) == "deny", f"should deny: {cmd}"


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
    approval = os.path.expanduser("~/.agents/approvals/linear")
    had = os.path.exists(approval)
    if had:
        os.rename(approval, approval + ".bak")
    try:
        for tool, inp, want in cases:
            got = run_hook("guard_mcp.py", {"tool_name": tool, "tool_input": inp, "cwd": "/tmp", "session_id": "test"},
                           env={"AGENTS_PROFILES_DIR": os.path.join(base, "profiles")})
            got = "deny" if got else None
            assert got == want, f"{tool} {inp}: {got} != {want}"
    finally:
        if had:
            os.rename(approval + ".bak", approval)


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
    assert guard("""python3 -c 'print("gh pr create")'""", repo) == "allow", "phrase inside a string is not a PR"
    assert guard('grep -n "gh pr create" hooks/guard_bash.py', repo) == "allow"
    assert guard('grep -n "guard_bash\\|gh pr create" bin/docs', repo) == "allow", "a | inside a pattern starts no command"
    assert guard("cat > notes.md <<'EOF'\nRun gh pr create\n| gh pr create --fill\nEOF", repo) == "allow", "heredoc text"
    assert guard("cat > notes.md <<'EOF'\ntext\nEOF\ngh pr create --fill", repo) == "deny", "a real one after a heredoc"
    runs = ['GH_HOST="x" gh pr create --fill', 'echo "$(gh pr create --fill)"', "cat <<EOF\n$(gh pr create --fill)\nEOF",
            "# <<EOF\ngh pr create --fill\nEOF", "echo $((1<<2)); gh pr create --fill", 'printf \\" | gh pr create --fill',
            "echo `gh pr create --fill`"]
    for command in runs:
        assert guard(command, repo) == "deny", f"the shell runs gh here: {command!r}"
    inert = ["cat <<EOF\n EOF\ngh pr create --fill\nEOF", "cat <<'END-MARK'\ngh pr create --fill\nEND-MARK",
             "echo 'a | gh pr create'", "cat <<-EOF\n\tgh pr create\n\tEOF"]
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
    assert "verify_auto.py" in text and ("ran: " in text or "every check was skipped" in text), text


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
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\nexit 0\n")
        os.chmod(os.path.join(vdir, "verify"), 0o755)
        open(os.path.join(repo, "app.py"), "a").write("y = 2\n")
        stop(RUN + "s6", repo, [os.path.join(repo, "app.py")])
        ok = subprocess.run(["python3", H + "review_stamp.py", "check", "--kind", "verify"], cwd=repo).returncode == 0
        assert ok, "green verify should stamp the change"
        report = subprocess.run([os.path.expanduser("~/.agents/bin/reports"), "path", "verify"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
        assert "# Verify: PASS" in open(report).read(), "verify should leave its evidence in a report"
        open(os.path.join(vdir, "verify"), "w").write("#!/bin/sh\necho failing; exit 1\n")
        outs = [stop(RUN + "s7", repo, [os.path.join(repo, "app.py")]) for _ in range(2)]
        assert "systemMessage" in outs[1] and "systemMessage" not in outs[0], outs
    finally:
        shutil.rmtree(vdir, ignore_errors=True)


def post_edit_syntax_feedback(base):
    repo = new_repo(base)
    bad = os.path.join(repo, "bad.py")
    open(bad, "w").write("def f(:\n")
    d = run_hook("post_edit.py", {"session_id": RUN + "s8", "cwd": repo, "tool_input": {"file_path": bad}})
    assert d and d.get("decision") == "block", d


for t in [guard_blocks_irreversible, guard_allows_routine, guard_mcp_linear, guard_mcp_logs_browser_mcp, pr_gate_review_and_validation,
          pr_gate_follows_worktrees, reports_survive_worktree_removal, overlay_found_from_worktree_with_another_name, stop_catches_leftovers_in_worktree, stop_catches_committed_leftover,
          stop_falls_back_to_auto_verify, stop_continues_only_once,
          stop_flags_secrets_redacted, stop_flags_marked_override_only,
          stop_finds_override_in_primary_checkout_and_health_finds_it_later, verify_stamp_and_effort_nudge,
          post_edit_syntax_feedback]:
    test(t)

# Test runs must not pollute the real hook log.
log = os.path.expanduser("~/.agents/logs/hooks.jsonl")
if os.path.exists(log):
    keep = [l for l in open(log) if "agents-test-" not in l and '"session": "test"' not in l]
    open(log, "w").writelines(keep)

shutil.rmtree(STATE, ignore_errors=True)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
