#!/usr/bin/env python3
"""bin/approve: which approvals would lift a guard's block, and the turn approvals a dialog answer writes."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []


def new_home(base):
    """A HOME whose ~/.agents is this kit without its approvals, logs or profiles, and a profile guarding `tracker`."""
    os.makedirs(os.path.join(base, ".agents"))
    for entry in set(os.listdir(KIT)) - {"logs", "repos", "approvals", "profiles"}:
        os.symlink(os.path.join(KIT, entry), os.path.join(base, ".agents", entry))
    os.makedirs(os.path.join(base, "profiles", "work"))
    json.dump({"gateways": [{"tool": "gateway__execute$", "service_field": "provider", "operation_fields": ["subtool"],
                             "writes": {"tracker": ["create-issue"]}}],
               "direct": {"playwright": "^browser_type$"}},  # guard_mcp lets browser tools through anyway
              open(os.path.join(base, "profiles", "work", "mcp-writes.json"), "w"))
    env = {**os.environ, "HOME": base, "AGENTS_PROFILES_DIR": os.path.join(base, "profiles"), "AGENTS_TEST": "1",
           "AGENTS_STATE_DIR": os.path.join(base, "state")}
    env.pop("AGENTS_LOG_DIR", None)  # the tests read the hook log in their own HOME
    return env


def run(env, *args, payload=None):
    return subprocess.run([os.path.join(KIT, "bin", "approve"), *args], capture_output=True, text=True, env=env,
                          input=json.dumps(payload) if payload is not None else "")


def needed(env, payload):
    """What bin/approve needs for the call, given the block the real guard gives it."""
    script = "guard_bash.py" if payload["tool_name"] == "Bash" else "guard_mcp.py"
    done = subprocess.run(["python3", os.path.join(KIT, "hooks", script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    deny = json.loads(done.stdout)["hookSpecificOutput"]["permissionDecisionReason"] if done.stdout.strip() else ""
    done = run(env, "needed", payload={**payload, "deny": "PreToolUse:Bash hook error: " + deny if deny else ""})
    assert done.returncode == 0 and not done.stderr, done
    return json.loads(done.stdout)


def bash(command, session="s1"):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "session_id": session, "cwd": "/tmp"}


def mcp(tool, tool_input=None, session="s1"):
    return {"tool_name": tool, "tool_input": tool_input or {}, "session_id": session, "cwd": "/tmp"}


def guard(env, script, payload):
    done = subprocess.run(["python3", os.path.join(KIT, "hooks", script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=env)
    assert done.returncode == 0 and not done.stderr, done
    return "deny" if done.stdout.strip() else "allow"


def names_the_approvals_that_would_lift_a_block(base):
    env = new_home(base)
    got = needed(env, bash("psql -c 'DROP TABLE runs'"))
    assert got == {"names": ["sql.drop-table"], "what": "`DROP TABLE runs` destroys database data",
                   "scope": "any DROP TABLE"}, got
    got = needed(env, bash("psql -c 'truncate  table runs; DROP DATABASE x; drop table y'"))
    assert got["names"] == ["sql.drop-database", "sql.drop-table", "sql.truncate-table"], got
    assert got["what"] == "`TRUNCATE TABLE runs`, `DROP DATABASE x`, `DROP TABLE y` destroy database data", got
    assert got["scope"] == "any DROP DATABASE or DROP TABLE or TRUNCATE TABLE", got
    assert needed(env, bash("echo 'DROP TABLE x' > out.sql && mysql < out.sql"))["what"] == \
        "`DROP TABLE x` destroys database data", "the statement stops where the shell takes over"
    assert needed(env, bash("echo DROP TABLE x > out.sql"))["what"] == "`DROP TABLE x` destroys database data"
    assert needed(env, bash('psql -c \'DROP TABLE "users"\''))["what"] == '`DROP TABLE "users"` destroys database data'
    assert needed(env, bash('psql -c "DROP TABLE runs"'))["what"] == "`DROP TABLE runs` destroys database data"
    assert needed(env, bash('psql -c "DROP TABLE \\"users\\""'))["what"] == '`DROP TABLE "users"` destroys database data'
    assert needed(env, bash('mysql -e "DROP DATABASE x" 2>&1'))["what"] == "`DROP DATABASE x` destroys database data"
    assert needed(env, bash("psql <<'SQL'\nDROP TABLE\n  runs;\nSQL"))["what"] == "`DROP TABLE runs` destroys database data"
    got = needed(env, mcp("mcp__linear__save_issue", {"title": "Fix it"}))
    assert got == {"names": ["linear"], "what": "`save_issue` writes to linear, which other people see",
                   "scope": "every linear write"}, got
    assert needed(env, bash("git push --force origin x && psql -c 'DROP TABLE runs'"))["names"] == [], \
        "another rule blocked it: allowing the statement would change nothing"
    assert run(env, "needed", payload={**bash("psql -c 'DROP TABLE runs'"), "deny": ""}).stdout.startswith(
        '{"names": []'), "no block, nothing to approve"
    got = needed(env, mcp("mcp__plugin_gateway__execute", {"provider": "tracker", "subtool": "create-issue"}))
    assert got["names"] == ["tracker"], "a service a profile declares too"
    assert needed(env, bash("sudo psql -c 'DROP TABLE runs'"))["names"] == ["sql.drop-table"], \
        "the statement's grant, though sudo stays blocked"
    for payload in [bash("sudo -n true"), bash("touch PRODUCT.md"), bash("ls"), mcp("mcp__linear__get_issue"),
                    mcp("mcp__playwright__browser_type", {"text": "x"}),
                    mcp("mcp__plugin_gateway__execute", {"provider": "tracker", "subtool": "list-issues"}),
                    {"tool_name": "Write", "tool_input": {"file_path": "/x/DESIGN.md"}, "session_id": "s1"}]:
        assert needed(env, payload)["names"] == [], f"nothing the dialog can approve: {payload}"
    assert run(env, "grant", "s1", "sql.drop-table", "linear").returncode == 0
    got = needed(env, bash("psql -c 'DROP TABLE runs; DROP SCHEMA s'"))
    assert got["names"] == ["sql.drop-schema"] and got["what"] == "`DROP SCHEMA s` destroys database data", \
        "an approval the turn already has isn't asked for again"
    assert needed(env, mcp("mcp__linear__save_issue"))["names"] == [], "nor a service's"
    assert needed(env, bash("psql -c 'DROP TABLE runs'", session="s2"))["names"] == ["sql.drop-table"], \
        "another session's approval counts for nothing here"
    assert run(env, "needed").returncode == 2, "no payload"


def a_grant_is_the_turn_approval_a_message_would_write(base):
    env = new_home(base)
    drop, issue = bash("psql -c 'DROP TABLE runs'"), mcp("mcp__linear__save_issue")
    assert guard(env, "guard_bash.py", drop) == "deny" and guard(env, "guard_mcp.py", issue) == "deny"
    assert run(env, "grant", "s1", "sql.drop-table", "linear").returncode == 0
    assert guard(env, "guard_bash.py", drop) == "allow" and guard(env, "guard_mcp.py", issue) == "allow"
    assert guard(env, "guard_bash.py", bash("psql -c 'DROP TABLE runs'", session="s2")) == "deny", "this session only"
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        logged = [json.loads(line) for line in fh if '"approve-dialog"' in line]
    assert [e["detail"] for e in logged] == ["linear,sql.drop-table"], logged
    assert run(env, "revoke", "s1", "sql.drop-table", "linear").returncode == 0
    assert guard(env, "guard_bash.py", drop) == "allow" and guard(env, "guard_mcp.py", issue) == "allow", \
        "revoke never takes back a turn approval"
    schema = bash("psql -c 'DROP SCHEMA s'")
    assert run(env, "once", "s1", "sql.drop-schema", "linear").returncode == 0
    assert guard(env, "guard_bash.py", schema) == "allow", "a one-call approval lifts the block too"
    assert run(env, "revoke", "s1", "sql.drop-schema", "linear").returncode == 0
    assert guard(env, "guard_bash.py", schema) == "deny", "revoke takes back what once wrote"
    assert guard(env, "guard_mcp.py", issue) == "allow", "and leaves the turn approval once found already there"
    assert run(env, "once", "s1", "sql.drop-schema").returncode == 0
    assert run(env, "grant", "s1", "sql.drop-schema").returncode == 0, "the user allows it for the turn meanwhile"
    assert run(env, "revoke", "s1", "sql.drop-schema").returncode == 0
    assert guard(env, "guard_bash.py", schema) == "allow", "a one-call flow ending never takes back a turn approval"
    assert run(env, "revoke", "s1", "sql.truncate-table").returncode == 0, "revoking what isn't there is fine"
    with open(os.path.join(base, ".agents", "logs", "hooks.jsonl")) as fh:
        logged = [json.loads(line)["detail"] for line in fh if '"revoke-once"' in line]
    assert logged == ["", "sql.drop-schema", "", ""], f"the log names only what a revoke took back: {logged}"
    subprocess.run(["python3", os.path.join(KIT, "hooks", "prompt_approvals.py")], env=env, capture_output=True,
                   input=json.dumps({"prompt": "thanks", "session_id": "s1", "cwd": "/tmp"}), text=True)
    assert guard(env, "guard_mcp.py", issue) == "deny" and guard(env, "guard_bash.py", schema) == "deny", \
        "a dialog's approval ends with the user's next message too"
    approvals = os.path.join(base, ".agents", "approvals")
    assert run(env, "grant", "s1", "linear").returncode == 0
    before = sorted(os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(base) for f in fs)
    for args in [("grant", "s1"), ("grant", "../s1", "linear"), ("grant", "s1", "../../evil"), ("grant", "", "linear"),
                 ("grant", "..", "linear"), ("grant", "s1", "x", "a/b"), ("revoke", "s1", "linear", ".."),
                 ("grant", "s1", ""), ("grant",), ("allow", "s1", "linear")]:
        done = run(env, *args)
        assert done.returncode == 2 and done.stderr.startswith("approve:"), (args, done)
    after = sorted(os.path.relpath(os.path.join(d, f), base) for d, _, fs in os.walk(base) for f in fs)
    assert after == before, f"a refused call writes and removes nothing: {set(before) ^ set(after)}"
    for verb in ("grant", "once"):
        os.symlink("/nonexistent", os.path.join(approvals, "turn", "s1", "sql.drop-table"))  # a name it can't write
        done = run(env, verb, "s1", "sql.drop-schema", "sql.drop-table")
        assert done.returncode == 1 and "couldn't write the approval" in done.stderr, (verb, done)
        assert not os.path.exists(os.path.join(approvals, "turn", "s1", "sql.drop-schema")), \
            f"{verb} cut short takes back the names it wrote"


for test in (names_the_approvals_that_would_lift_a_block, a_grant_is_the_turn_approval_a_message_would_write):
    base = tempfile.mkdtemp(prefix="agents-test-approve-")
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
