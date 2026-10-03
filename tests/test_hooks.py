#!/usr/bin/env python3
"""Regression tests for the kit's hooks. Each test builds throwaway git repos in a temp dir.

Run: python3 ~/.agents/tests/test_hooks.py          every test, each in its own process (exit 1 on any failure)
     python3 ~/.agents/tests/test_hooks.py <name>...  only those, one after another in this process
"""
import concurrent.futures
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
    assert guard("git restore --staged .") == "allow", "unstaging keeps the work"
    work = os.path.join(base, "work")
    os.makedirs(os.path.join(work, "sub", "build"))
    assert guard("cd sub && rm -rf build", cwd=work) == "allow"
    assert guard("cd lib && make && cd .. && rm -rf lib/build", cwd="/usr") == "allow", "cd .. comes back, not up"
    assert guard("cd -; cd /usr; rm -rf lib/build", cwd="/usr") == "allow", "an absolute cd ends the unknown"
    assert guard("(cd / && ls) && rm -rf lib/build", cwd="/usr") == "allow", "a subshell's cd ends with it"
    assert guard("git restore --staged -- .") == "allow" and guard("git restore -Sq .") == "allow"


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
                 "| B1 | page | + | FAIL | ![page](evidence-x/b1.png) |\n"
                 f"| B2 | linked by absolute path, as validate writes it | + | PASS | [output]({evidence}/A1.txt) |\n")
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

    def write_staging():
        return subprocess.run(["python3", H + "review_stamp.py", "write", "--kind", "staging"], cwd=repo,
                              capture_output=True, text=True)

    refused = write_staging()
    assert refused.returncode == 2 and "without a PASS" in refused.stdout + refused.stderr, refused
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
    assert "re-run" in (reason("gh pr ready") or ""), "results nobody stamped for this change"
    assert write_staging().returncode == 0
    open(os.path.join(repo, "app.py"), "a").write("z = 3\n")
    assert "re-run" in (reason("gh pr ready") or ""), "an edit after the staging run"
    for kind in ("review", "validate"):
        write_stamp(repo, kind)
    assert "re-run" in (reason("gh pr create --fill") or ""), "on every path the gate holds"
    assert write_staging().returncode == 0, "re-stamped once the affected steps passed again"
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
    with open(guide, "w") as fh:
        fh.write(steps + head + "| S1 | FAIL | [out](e/S1.txt) |\n| S2 | PASS | [out](e/S2.txt) |\n"
                 "\n## Notes\n\n| Step | Result | Evidence |\n|---|---|---|\n| S1 | PASS | [out](e/S1.txt) |\n")
    assert "FAIL" in (reason("gh pr ready") or ""), "the round ends at the next section: a later table can't hide a FAIL"
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
    nothing = write_staging()
    assert nothing.returncode == 2 and "no staging guide" in nothing.stdout + nothing.stderr, nothing


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


def reports_brief_gives_one_line_per_report(base):
    repo = new_repo(base)
    git(repo, "switch", "-q", "-c", "feat/brief")
    reports = os.path.expanduser("~/.agents/bin/reports")

    def path(kind):
        return subprocess.run([reports, "path", kind], cwd=repo, capture_output=True, text=True).stdout.strip()
    spec = subprocess.run([os.path.expanduser("~/.agents/skills/spec/path.sh")], cwd=repo, capture_output=True, text=True).stdout.strip()
    open(spec, "w").write("# Round prices once\n\nGoal: totals drift.\n" + "x\n" * 200)
    open(path("review"), "w").write("Self-review (risk: standard)\n" + "detail\n" * 200 + "Open:     the 0.005 case\n")
    open(path("validation"), "w").write("| # | Check | Case | Result | Evidence |\n|---|---|---|---|---|\n"
                                         "| A1 | a PASS | + | **PASS** | e |\n| A2 | b | - | FAIL | e |\n|A3|c|+|NOT RUN|-|\n")
    open(path("follow-pr"), "w").write("## Run 1\nStatus:  waiting on CI\n## Run 2\nStatus:  ready to merge\n")
    shown = subprocess.run([reports, "brief"], cwd=repo, capture_output=True, text=True).stdout
    for line in ("Round prices once", "Self-review (risk: standard)", "Open:     the 0.005 case", "1 PASS, 1 FAIL, 1 NOT RUN",
                 "Status:  ready to merge", path("review")):
        assert line in shown, (line, shown)
    assert "detail" not in shown and "waiting on CI" not in shown and len(shown.splitlines()) < 20, shown
    open(path("review"), "w").write("Self-review\n")
    open(path("follow-pr"), "w").write("## Run 1\n")
    done = subprocess.run([reports, "brief"], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0 and "no Status line yet" in done.stdout and "1 PASS, 1 FAIL, 1 NOT RUN" in done.stdout, done


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
    assert stop_with("small", "PR open", 200_000) is None, "a small context needs no new session"
    assert stop_with("early", "self-review", 300_000) is None, "mid-change a new session would re-read everything"
    assert stop_with("edge", "PR open", 250_000) is None, "over 250K, not at it"
    merged = stop_with("merged", "ship", 300_000)
    assert merged and "is merged" in merged["systemMessage"], merged


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
    assert not stop_with("nc-codex", 200_000, transcript=False), "no transcript (Codex, Pi): no context to measure"
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
    env = profiles_with_after_turn(base, {"slow": '#!/bin/sh\nyes noise | head -c 2000000\nsleep 3\n'
                                                    'touch "$(dirname "$0")/done"\nexit 1\n'})
    done = os.path.join(env["AGENTS_PROFILES_DIR"], "slow", "done")
    decision, secs = timed_stop(RUN + "at2b", env)
    assert decision == without and "Debug leftover" in decision.get("reason", ""), (decision, without)
    assert secs < plain_secs + 0.5 and not os.path.exists(done), f"the stop hook waited: {secs:.2f}s vs {plain_secs:.2f}s"
    assert wait_for(done), "the after-turn keeps running after the stop hook returns"


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
    assert "guard failed" in shell("echo x > PRODUCT.md"), "and this guard fails closed, saying why"
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

TESTS = [guard_blocks_irreversible, guard_allows_routine, guard_mcp_linear, asking_for_a_service_approves_its_writes_for_that_turn, guard_mcp_logs_browser_mcp, pr_gate_review_and_validation,
          stamps_survive_merging_the_default_branch, validate_stamp_needs_evidence, pr_gate_waits_for_staging, pr_gate_follows_worktrees, guard_fails_closed, codex_pr_commands_name_their_checkout, reports_survive_worktree_removal, reports_brief_gives_one_line_per_report, overlay_found_from_worktree_with_another_name, stop_catches_leftovers_in_worktree, stop_checks_edits_after_its_directory_is_removed, stop_skips_a_checkout_removed_while_checked, stop_starts_each_profile_after_turn, stop_never_waits_for_profile_after_turn, stop_logs_an_after_turn_that_cannot_start, stop_catches_committed_leftover,
          stop_falls_back_to_auto_verify, stop_continues_only_once,
          stop_flags_secrets_redacted, stop_flags_marked_override_only,
          stop_finds_override_in_primary_checkout_and_health_finds_it_later, verify_stamp_and_effort_nudge,
          stop_asks_once_about_files_outside_the_change_map, stop_follows_ci_after_a_push, stop_suggests_a_fresh_session_once_the_pr_is_open, stop_suggests_a_fresh_session_for_the_next_change, guard_records_the_pushed_checkout,
          post_edit_syntax_feedback, impeccable_files_in_a_shared_repo_need_the_user_first, impeccable_guard_edge_cases]

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
