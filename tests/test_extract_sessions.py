#!/usr/bin/env python3
"""usage/extract_sessions.py: a Claude Code transcript split into phases of the flow, per branch."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
TEXT = {"type": "text", "text": "ok"}


def at(minute, second=0):
    return f"2026-10-01T10:{minute:02d}:{second:02d}Z"


def user(minute, text, branch, uuid=None):
    return {"type": "user", "timestamp": at(minute), "cwd": "/repo", "gitBranch": branch, "uuid": uuid or f"u{minute}{branch}",
            "message": {"role": "user", "content": text}}


def assistant(minute, msg_id, branch, blocks, out=10, second=0):
    """One line per content block, each repeating the message's usage, as Claude Code writes them."""
    usage = {"input_tokens": 1, "output_tokens": out, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 100}
    return [{"type": "assistant", "timestamp": at(minute, second), "cwd": "/repo", "gitBranch": branch, "uuid": f"{msg_id}-{i}",
             "message": {"id": msg_id, "model": "claude-opus-5-5", "usage": usage, "content": [block]}}
            for i, block in enumerate(blocks)]


def skill(name):
    return {"type": "tool_use", "id": f"t-{name}", "name": "Skill", "input": {"skill": name}}


def edit(path):
    return {"type": "tool_use", "id": f"t-{path}", "name": "Write" if "spec-" in path else "Edit", "input": {"file_path": path}}


def bash(command, tool_id="b"):
    return {"type": "tool_use", "id": f"t-{tool_id}", "name": "Bash", "input": {"command": command}}


def session(home, rows, renames=(), cwd="repo", subagents=()):
    """Rows' "/repo" is a checkout of o/repo at HOME/repo; with `cwd`, the session ran in HOME/<cwd> instead.
    `subagents` are (id, agent type, lines, model given at spawn or None)."""
    checkout = os.path.join(home, "repo")
    os.makedirs(checkout)
    subprocess.run(["git", "init", "-q", checkout], check=True)
    subprocess.run(["git", "-C", checkout, "remote", "add", "origin", "git@github.com:o/repo.git"], check=True)
    os.makedirs(os.path.join(home, ".claude", "projects", "p"))
    with open(os.path.join(home, ".claude", "projects", "p", "s1.jsonl"), "w") as fh:
        fh.writelines(json.dumps({**r, "cwd": os.path.join(home, cwd)} if r.get("cwd") else r) + "\n" for r in rows)
    for agent in subagents:
        subagent(home, *agent)
    os.makedirs(os.path.join(home, ".agents", "logs"))
    with open(os.path.join(home, ".agents", "logs", "quality.jsonl"), "w") as fh:
        fh.writelines(json.dumps({"kind": "rename", "repo": repo, "name": old, "to": new}) + "\n" for repo, old, new in renames)
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found, = list(module.claude_sessions())
    return found


def spending(phases):
    """The phases that spent tokens: a prompt alone also puts its phase on a branch."""
    return {b: {p: v for p, v in ps.items() if v["in"] or v["out"]} for b, ps in phases.items()}


def phases_per_branch_and_skill(home):
    rows = [user(0, "build the thing", "a"),
            *assistant(1, "m0", "a", [TEXT, skill("spec"), edit("/tmp/agents-specs/spec-a.md")], out=7),
            *assistant(2, "m1", "a", [TEXT, edit("/repo/app.py")]),       # the first code edit ends the spec
            {**user(0, "replayed by a resume", "a"), "timestamp": "2026-09-30T10:00:00Z", "uuid": "old"},
            *assistant(3, "m2", "a", [TEXT, skill("self-review")]),        # the skill in a later block owns the message
            {"type": "file-history-snapshot", "timestamp": at(4), "uuid": "fh"},  # no branch: still branch a's time
            *assistant(5, "m3", "a", [TEXT]),
            user(40, "<command-name>/validate</command-name>", "a"),       # typed after 35 idle minutes
            *assistant(41, "m4", "a", [TEXT]),
            *assistant(43, "m5", "b", [TEXT]),                              # another branch starts at build
            *assistant(44, "m6", "a", [TEXT]),                              # and so does coming back
            *assistant(45, "m2", "a", [TEXT, skill("self-review")]),       # a replayed message changes nothing
            ]
    found = session(home, rows)
    a, b = found["phases"]["a"], found["phases"]["b"]
    assert sorted(a) == ["build", "self-review", "spec", "validate"], a
    assert a["spec"] == {"in": 1, "out": 7, "cache_read": 1000, "cache_write": 100, "minutes": 1, "prompts": 1, "questions": 0,
                         "corrections": 0, "wait_minutes": 0, "reply_minutes": 0}, a["spec"]
    assert (a["build"]["out"], a["build"]["minutes"]) == (20, 2), f"m1 and m6, 1 minute each: {a['build']}"
    assert (a["self-review"]["out"], a["self-review"]["minutes"]) == (20, 3), \
        f"m2 and m3, minutes 2 to 5 through the branchless entry and past the replayed timestamp: {a['self-review']}"
    assert a["validate"]["minutes"] == 1, "the 35-minute gap is the user away, not work"
    assert sorted(b) == ["build"] and b["build"]["out"] == 10, b
    assert found["tokens"]["out"] == 67, "each message counted once, not once per content block or replay"


def a_rename_keeps_the_phase(home):
    rows = [user(0, "go", "session/x"), *assistant(1, "m0", "session/x", [skill("validate")]), *assistant(2, "m1", "feature/cart", [TEXT]),
            *assistant(3, "m2", "other", [skill("validate")]), *assistant(4, "m3", "next", [TEXT])]
    found = session(home, rows, renames=[("repo", "session/x", "session/y"), ("repo", "session/y", "feature/cart"),
                                         ("elsewhere", "other", "next")])
    phases = spending(found["phases"])
    assert sorted(phases["feature/cart"]) == ["validate"], f"a chain of renames is one change: {phases}"
    assert sorted(phases["next"]) == ["build"], f"another repo's rename doesn't apply here: {phases}"


def what_ends_a_phase(home):
    rows = [user(0, "go", "a"),
            *assistant(1, "m0", "a", [skill("spec"), edit("/repo/app.py")]),           # starts the spec and builds at once
            *assistant(2, "m1", "a", [TEXT]),
            *assistant(3, "m2", "a", [skill("spec")]),
            *assistant(4, "m3", "a", [edit("/repo/spec-parser.py")]),                  # code, despite its name
            *assistant(5, "m4", "a", [skill("validate")]),
            {**user(6, "<command-name>/self-review</command-name>", "a"), "gitBranch": ""},  # no branch: no change
            *assistant(7, "m5", "a", [TEXT])]
    phases = session(home, rows)["phases"]["a"]
    assert (phases["build"]["out"], phases["spec"]["out"], phases["validate"]["out"]) == (30, 10, 20), phases


def branches_started_by_commands(home):
    """The session directory stays on one branch (or detached) while the agent starts each change in a worktree."""
    rows = [user(0, "go", "phase-cost"),
            *assistant(1, "m0", "phase-cost", [skill("self-review")]),
            *assistant(2, "m1", "phase-cost", [bash("cd ~/kit && git worktree add -q -b short-outputs $SP/kit-so origin/main")]),
            *assistant(3, "m2", "phase-cost", [TEXT]),                      # its gitBranch hasn't changed: still short-outputs
            *assistant(4, "m3", "HEAD", [TEXT]),                            # the session directory detached
            *assistant(5, "m4", "HEAD", [bash("git fetch -q; git switch -qc guard origin/main"), skill("validate")]),
            *assistant(6, "m5", "HEAD", [bash("git branch -m guard guard-says-the-order")]),
            *assistant(7, "m6", "HEAD", [bash("gh pr create --body-file - <<'EOF'\ngit switch -c bogus\nEOF", "m6"),
                                         bash("cd $(mktemp -d) && git init -q && git checkout -q -b scratch", "m6b")]),
            *assistant(8, "m7", "HEAD", [bash("git -C ../wt branch -m old-elsewhere new-elsewhere")]),  # not this branch
            *assistant(9, "m8", "HEAD", [bash("git -C '../my wt' switch -q existing"), bash("git switch -c taken", "m8b")]),
            {"type": "user", "timestamp": at(9, 1), "gitBranch": "HEAD", "uuid": "r8b",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-m8b", "is_error": True,
                                                      "content": "fatal: a branch named 'taken' already exists"}]}},
            *assistant(10, "m9", "HEAD", [bash("git switch -q after-error >/dev/null && python3 broken.py", "m9")]),
            {"type": "user", "timestamp": at(10, 1), "gitBranch": "HEAD", "uuid": "r9",
             "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-m9", "is_error": True,
                                                      "content": "Traceback: broken.py"}]}}]
    found = session(home, rows, cwd="repo-worktree-session-gone")
    assert found["branches"] == ["HEAD", "after-error", "existing", "guard", "guard-says-the-order", "new-elsewhere", "phase-cost",
                                 "short-outputs"], "a heredoc's body and a throwaway repo's branches aren't the session's: " + str(found["branches"])
    phases = found["phases"]
    assert sorted(phases["short-outputs"]) == ["build"] and phases["short-outputs"]["build"]["out"] == 20, phases
    assert phases["existing"]["build"]["out"] == 10 and phases["after-error"]["build"]["out"] == 10, \
        f"m8 on the branch switched to; a later step's error doesn't undo m9's switch: {phases}"
    assert "guard" not in phases and phases["guard-says-the-order"]["validate"]["out"] == 40, \
        f"a rename carries the branch's phase and its spending to the new name: {phases}"
    assert found["repos"] == {"o/repo": ["repo"]}, f"a removed worktree's repo comes from its main checkout: {found['repos']}"


def writing_the_spec_marks_the_work_before_it(home):
    """Agents write most specs without invoking the skill again; the spec file is what every spec leaves behind."""
    rows = [user(0, "go", "session/x"),
            *assistant(1, "m0", "session/x", [TEXT]),                                   # reading the issue and the code
            *assistant(2, "m1", "session/x", [TEXT]),
            *assistant(3, "m2", "session/x", [edit("/g/agents/spec-repo-x.md")]),       # the spec, written with Write
            *assistant(4, "m3", "session/x", [bash("git branch -m kit-change")]),
            *assistant(5, "m4", "kit-change", [edit("/repo/app.py")]),                  # the first code edit ends it
            *assistant(6, "m5", "kit-change", [TEXT]),
            *assistant(7, "m6", "kit-change", [{**edit("/g/agents/spec-repo-kit-change.md"), "name": "Edit"}]),
            *assistant(8, "m7", "kit-change", [edit("/repo/app.py")]),
            *assistant(9, "m8", "other", [TEXT]),
            *assistant(10, "m9", "other", [skill("self-review")])]
    phases = session(home, rows)["phases"]
    assert (phases["kit-change"]["spec"]["out"], phases["kit-change"]["build"]["out"]) == (40, 40), \
        f"m0-m3 are the spec (m0 and m1 before the file existed), m4-m7 build: a spec edit mid-build is not a new spec: {phases}"
    assert sorted(phases["other"]) == ["build", "self-review"], f"without a spec, the work before a skill stays build: {phases}"


def what_counts_as_starting_the_code(home):
    rows = [user(0, "go", "main"),
            *assistant(1, "m0", "main", [edit("/repo/app.py"), bash("git switch -q -c b1 origin/main")]),  # edit on main
            *assistant(2, "m1", "b1", [edit("/private/tmp/claude-501/x/probe.py")]),                      # scratch: not code
            *assistant(3, "m2", "b1", [edit("/g/agents/spec-repo-b1.md")]),
            *assistant(4, "m3", "b1", [edit("/repo/app.py")]),
            *assistant(5, "m4", "b1", [bash("git branch -m b2")]),                                       # renamed after code
            *assistant(6, "m5", "b2", [{**edit("/g/agents/spec-repo-b2.md"), "name": "Edit"}]),
            *assistant(7, "m6", "b2", [bash("git switch -q -c b1 origin/main")]),                     # the old name, reused
            *assistant(8, "m7", "b1", [edit("/g/agents/spec-repo-b1.md")])]
    phases = spending(session(home, rows)["phases"])
    assert not phases["main"] and (phases["b2"]["spec"]["out"], phases["b2"]["build"]["out"]) == (30, 30), \
        f"m0 (it ends on b1) to m2 spec; after the code edit a rename keeps it started, so the later spec edit is build: {phases}"
    assert sorted(phases["b1"]) == ["spec"], f"a new branch under a renamed one's old name starts fresh: {phases}"


def short_gaps_add_up(home):
    rows = [user(1, "go", "a")] + [row for i in range(31) for row in assistant(1 + 2 * i // 60, f"m{i}", "a", [TEXT], second=2 * i % 60)]
    found = session(home, rows)
    assert found["phases"]["a"]["build"]["minutes"] == 1.0, found["phases"]["a"]


def the_users_attention_per_branch(home):
    ask = {"type": "tool_use", "id": "t-ask", "name": "AskUserQuestion", "input": {"questions": []}}
    answer = {"type": "user", "timestamp": at(20), "cwd": "/repo", "gitBranch": "a", "uuid": "ans",
              "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-ask", "content": "Option A"}]}}
    tool_result = {**answer, "timestamp": at(21), "uuid": "tr",
                   "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t-b", "content": "done"}]}}
    rows = [user(3, "build the thing", "a"),                                  # the first prompt waited on nobody
            *assistant(4, "m0", "a", [TEXT, ask]),
            answer,                                                           # 16 minutes to answer the question
            tool_result,                                                      # not the user
            *assistant(22, "m1", "a", [TEXT, bash("ls")]),
            {**user(24, "Base directory for this skill: /x", "a"), "isMeta": True},  # a skill's text, not a prompt
            user(30, "no, that's wrong", "a"),                                # 6 minutes after the skill's text
            {**user(30, "<local-command-stdout>Kept model</local-command-stdout>", "a"), "uuid": "out"},  # the harness's
            user(31, "<command-message>validate</command-message>\n<command-name>/validate</command-name>", "a"),  # typed
            *assistant(31, "m2", "b", [TEXT]),
            {**user(31, "go on", "b"), "timestamp": "2026-10-01T12:01:00Z"}]  # 90 minutes later, on branch b
    phases = session(home, rows)["phases"]
    totals = {b: {k: round(sum(p.get(k, 0) for p in ps.values()), 1) for k in ("prompts", "questions", "corrections", "wait_minutes",
                                                                             "reply_minutes")} for b, ps in phases.items()}
    assert totals["a"] == {"prompts": 3, "questions": 1, "corrections": 1, "wait_minutes": 23, "reply_minutes": 22}, totals["a"]
    assert totals["b"] == {"prompts": 1, "questions": 0, "corrections": 0, "wait_minutes": 90, "reply_minutes": 15}, \
        f"an away gap is all idle time but at most 15 reply minutes: {totals['b']}"

def attention(phases):
    return {b: {k: round(sum(p.get(k, 0) for p in ps.values()), 1) for k in ("prompts", "corrections", "wait_minutes")}
            for b, ps in phases.items()}


def what_the_user_did_not_type(home):
    rows = [user(3, "build the thing", "a"),
            *assistant(4, "m0", "a", [TEXT]),
            {**user(10, "This session is being continued from a previous conversation.", "a"), "isCompactSummary": True},
            {**user(11, "The lane plugin sent a message", "a"), "origin": {"kind": "plugin", "name": "lane"}},
            user(12, "<bash-stdout>ok</bash-stdout>", "a"),
            *assistant(12, "m1", "a", [TEXT, bash("make")]),
            user(20, "[Request interrupted by user for tool use]", "a"),  # 8 minutes of the agent's work, not a wait
            {**user(21, "no, the other file", "a"), "origin": {"kind": "human"}}]
    assert attention(session(home, rows)["phases"]) == {"a": {"prompts": 3, "corrections": 2, "wait_minutes": 1}}


def another_agents_run_is_not_the_user(home):
    rows = [{**r, "entrypoint": "sdk-cli"} for r in [user(3, "Task: cap the review rounds", "a"),
                                                       *assistant(4, "m0", "a", [TEXT]), user(9, "go on", "a")]]
    assert attention(session(home, rows)["phases"]) == {"a": {"prompts": 0, "corrections": 0, "wait_minutes": 0}}


def a_session_of_typed_commands(home):
    rows = [user(3, "<command-message>validate</command-message>\n<command-name>/validate</command-name>", "a"),
            *assistant(4, "m0", "a", [TEXT])]
    found = session(home, rows)
    assert attention(found["phases"]) == {"a": {"prompts": 1, "corrections": 0, "wait_minutes": 0}}
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.summarize(found)["first_prompt"] == "", "the summary has no prompt text to quote, and doesn't fail"


def the_kit_names_count_as_their_former_phases(home):
    rows = [user(0, "build the thing", "a"),
            *assistant(1, "m0", "a", [skill("kit:kit-spec")]),
            *assistant(2, "m1", "a", [edit("/repo/app.py")]),
            *assistant(3, "m2", "a", [skill("kit-review")]),
            *assistant(4, "m3", "a", [skill("kit-write-pr-description")]),  # runs inside a phase, starts none
            user(5, "<command-message>kit-validate</command-message>\n<command-name>/kit-validate</command-name>", "a"),
            *assistant(6, "m4", "a", [TEXT]),
            *assistant(7, "m5", "a", [skill("kit-create-pr")]),
            *assistant(8, "m6", "a", [skill("kit:kit-follow-pr")]),
            *assistant(9, "m7", "a", [skill("kit-ship")]),
            *assistant(10, "m8", "b", [skill("create-pr")])]                  # a session from before the rename
    found = spending(session(home, rows)["phases"])
    assert sorted(found["a"]) == ["build", "create-pr", "follow-pr", "self-review", "ship", "spec", "validate"], found["a"]
    assert found["a"]["self-review"]["out"] == 20, f"kit-write-pr-description stays in the review: {found['a']['self-review']}"
    assert sorted(found["b"]) == ["create-pr"], found["b"]



def subagent(home, agent_id, agent_type, lines, model):
    """A subagent's transcript and meta file beside the session's, where Claude Code keeps them."""
    folder = os.path.join(home, ".claude", "projects", "p", "s1", "subagents")
    os.makedirs(folder, exist_ok=True)
    meta = {"agentType": agent_type, "description": "d", **({"model": model} if model else {})}
    json.dump(meta, open(os.path.join(folder, f"agent-{agent_id}.meta.json"), "w"))
    if lines is None:
        return  # a meta file whose transcript is missing
    with open(os.path.join(folder, f"agent-{agent_id}.jsonl"), "w") as fh:
        fh.writelines(json.dumps(line) + "\n" for line in lines)


def build_turn(minute, msg_id, model, usage, blocks=1, cwd="/repo"):
    return [{"type": "assistant", "isSidechain": True, "timestamp": at(minute), "cwd": cwd, "gitBranch": "feat",
             "effort": "high", "uuid": f"{msg_id}-{i}", "message": {"id": msg_id, "model": model, "usage": usage, "content": [TEXT]}}
            for i in range(blocks)]


def builds_come_from_their_transcripts(home):
    usage = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 3000,
             "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000}}
    rows = [user(0, "build it", "feat"), *assistant(1, "m0", "feat", [TEXT])]
    found = session(home, rows, subagents=[
        ("b1", "kit:build-standard", [*build_turn(2, "x1", "claude-sonnet-5-5", usage, blocks=2),
                                      *build_turn(9, "x2", "claude-sonnet-5-5", usage)], "sonnet"),
        ("r1", "kit:review-tests", build_turn(3, "y1", "claude-opus-5-5", usage), None),
        ("b2", "kit:build-low", [*build_turn(4, "z1", "claude-opus-5-5", usage), *build_turn(5, "z2", "claude-haiku-9", usage)], None)])
    builds = sorted(found["builds"], key=lambda b: b["agent"])
    assert [b["agent"] for b in builds] == ["kit:build-low", "kit:build-standard"], "only build agents, review agents left out"
    standard = builds[1]
    assert standard["models"] == {"claude-sonnet-5-5": 2} and standard["effort"] == ["high"], standard
    assert standard["tokens"] == {"in": 20, "out": 2000, "cache_read": 200000, "cache_write_5m": 4000, "cache_write_1h": 2000}, \
        "a message repeated once per content block counts once: " + str(standard["tokens"])
    # per message: 10 x $2 + 1000 x $10 + 100000 x $0.10 + 2000 x $2.50 + 1000 x $4, per million tokens
    assert abs(standard["usd"] - 2 * 0.02902) < 1e-9, standard["usd"]
    assert standard["minutes"] == 7 and standard["branch"] == "feat" and standard["assigned_model"] == "sonnet", standard
    assert builds[0]["usd"] is None, "a model the table doesn't price leaves the build without dollars, not under-counted"
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.summarize(found)["builds"] == found["builds"]


def opus_prices_and_usage_without_the_cache_split(home):
    split = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 100000, "cache_creation_input_tokens": 3000,
             "cache_creation": {"ephemeral_5m_input_tokens": 2000, "ephemeral_1h_input_tokens": 1000}}
    unsplit = {k: v for k, v in split.items() if k != "cache_creation"}
    found = session(home, [user(0, "build it", "feat"), *assistant(1, "m0", "feat", [TEXT])], subagents=[
        ("b1", "kit:build-high", build_turn(2, "x1", "claude-opus-5-5", split), None),
        ("b2", "kit:build-low", build_turn(3, "y1", "claude-sonnet-5-5", unsplit), "sonnet"),
        ("b3", "kit:build-standard", None, "opus")])
    high, low = sorted(found["builds"], key=lambda b: b["agent"])
    assert [high["agent"], low["agent"]] == ["kit:build-high", "kit:build-low"], "a build whose transcript is missing is skipped"
    assert abs(high["usd"] - (10 * 4 + 1000 * 20 + 100000 * 0.20 + 2000 * 5 + 1000 * 8) / 1e6) < 1e-9, high["usd"]
    assert high["assigned_model"] is None
    assert (low["tokens"]["cache_write_5m"], low["tokens"]["cache_write_1h"]) == (3000, 0), "no split: every write at 5 minutes"
    assert abs(low["usd"] - (10 * 2 + 1000 * 10 + 100000 * 0.10 + 3000 * 2.50) / 1e6) < 1e-9, low["usd"]


def advisor_calls_and_what_the_table_cannot_price(home):
    first = {"input_tokens": 2, "output_tokens": 300, "cache_read_input_tokens": 70000, "cache_creation_input_tokens": 1000,
             "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000}, "type": "message"}
    advice = {"input_tokens": 75000, "output_tokens": 3000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
              "type": "advisor_message", "model": "claude-fable-5-1"}
    second = {**first, "output_tokens": 2000, "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 0}}
    # Totals as Claude Code writes them: the advisor's call left out, the cache-write split of the first call only.
    advised = {"input_tokens": 4, "output_tokens": 2300, "cache_read_input_tokens": 140000, "cache_creation_input_tokens": 2000,
               "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000},
               "speed": "standard", "inference_geo": "global", "iterations": [first, advice, second]}
    plain = {"input_tokens": 10, "output_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    found = session(home, [user(0, "build it", "feat"), *assistant(1, "m0", "feat", [TEXT])], subagents=[
        # Streamed as Claude Code writes it: the first line a partial snapshot, the final usage on the last.
        ("b1", "kit:build-high", build_turn(2, "x1", "claude-opus-5-5[1m]", {**first, "output_tokens": 16, "type": None},
                                            cwd=os.path.join(home, "repo"))
         + build_turn(2, "x1", "claude-opus-5-5[1m]", advised, cwd=os.path.join(home, "repo")), None),
        ("b2", "kit:build-low", build_turn(3, "y1", "claude-opus-5-5", {**plain, "speed": "fast", "iterations": [plain, advice]}), "opus"),
        ("b3", "kit:build-standard", build_turn(4, "z1", "claude-sonnet-5-5", {**plain, "inference_geo": "us"}), "sonnet")])
    high, low, standard = sorted(found["builds"], key=lambda b: b["agent"])
    assert high["models"] == {"claude-opus-5-5[1m]": 1} and high["repo"] == "repo", high
    assert high["tokens"] == {"in": 4, "out": 2300, "cache_read": 140000, "cache_write_5m": 1000, "cache_write_1h": 1000}, high["tokens"]
    fable = (75000 * 10 + 3000 * 50) / 1e6
    assert high["advisor"]["calls"] == 1 and abs(high["advisor"]["usd"] - fable) < 1e-9, high["advisor"]
    opus = (4 * 4 + 2300 * 20 + 140000 * 0.20 + 1000 * 5 + 1000 * 8) / 1e6
    assert abs(high["usd"] - (opus + fable)) < 1e-9, high["usd"]
    assert low["usd"] is None and standard["usd"] is None, "fast mode and US-only inference cost more than the table says"
    assert low["advisor"] == {"calls": 1, "usd": None}, low["advisor"]
    assert low["repo"] == "repo", "a build whose worktree is gone takes the session's repo"


def clone(path, remote):
    os.makedirs(path)
    subprocess.run(["git", "init", "-q", path], check=True)
    subprocess.run(["git", "-C", path, "remote", "add", "origin", remote], check=True)


def repos_of_sessions(home, directories):
    """The repos the extractor gives one session started in each of `directories`, one transcript each; keyed by
    directory."""
    os.makedirs(os.path.join(home, ".claude", "projects", "p"), exist_ok=True)
    for i, directory in enumerate(directories):
        rows = [user(0, "go", "main"), *assistant(1, "m0", "main", [TEXT])]
        with open(os.path.join(home, ".claude", "projects", "p", f"s{i}.jsonl"), "w") as fh:
            fh.writelines(json.dumps({**r, "cwd": directory}) + "\n" for r in rows)
    os.environ["HOME"] = home
    spec = importlib.util.spec_from_file_location("extract_sessions", os.path.join(KIT, "usage", "extract_sessions.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = {s["cwd"]: s["repos"] for s in module.claude_sessions()}
    shutil.rmtree(os.path.join(home, ".claude"))
    return found


def an_orca_worktree_takes_its_repo_from_git(home):
    main = os.path.join(home, "src", "shop")
    clone(main, "git@github.com:o/shop.git")
    subprocess.run(["git", "-C", main, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"], check=True)
    nested, flat = os.path.join(home, "orca", "workspaces", "shop", "cart"), os.path.join(home, "mine", "cart-2")
    for path in (nested, flat):
        subprocess.run(["git", "-C", main, "worktree", "add", "-q", "--detach", path], check=True)
    found = repos_of_sessions(home, [nested, flat])
    assert found == {nested: {"o/shop": ["shop"]}, flat: {"o/shop": ["shop"]}}, found


def a_removed_orca_worktree_takes_its_repo_from_the_main_checkout(home):
    main = os.path.join(home, "src", "shop")
    clone(main, "git@github.com:o/shop.git")
    removed = [os.path.join(home, "orca", "workspaces", "shop", "cart"),
               os.path.join(home, "orca", "workspaces", "shop", "fix-worktree-cache")]  # Orca's rule, not Xirp's
    for order in ([main, *removed], [*removed, main]):  # the checkout's transcript among the first read, then the last
        found = repos_of_sessions(home, order)
        assert found == {path: {"o/shop": ["shop"]} for path in order}, f"sessions in {order}: {found}"
    # Flat and custom layouts keep no repo name, even when a directory in the path is named like a repo; a Xirp-shaped
    # worktree whose main checkout is gone keeps Xirp's rule.
    clone(os.path.join(home, "src", "workspaces"), "git@github.com:o/workspaces.git")
    flat, custom = os.path.join(home, "orca", "workspaces", "shop-cart"), os.path.join(home, "custom", "shop", "cart")
    xirp = os.path.join(home, "x", "shop", "kit-worktree-gone")
    found = repos_of_sessions(home, [main, os.path.join(home, "src", "workspaces"), flat, custom, xirp])
    assert found[flat] == {} and found[custom] == {} and found[xirp] == {}, found


def a_removed_orca_worktree_finds_its_checkout_anywhere_a_session_ran(home):
    main = os.path.join(home, "src", "shop")
    clone(main, "git@github.com:o/shop.git")
    removed = os.path.join(home, "orca", "workspaces", "shop", "cart")
    # The checkout appears only after a session's first directory: a later cwd of a Claude transcript ...
    later = os.path.join(home, ".claude", "projects", "p", "later.jsonl")
    os.makedirs(os.path.dirname(later), exist_ok=True)
    with open(later, "w") as fh:
        fh.writelines(json.dumps({"type": "progress", "cwd": d}) + "\n" for d in (os.path.join(home, "gone"), main))
    found = repos_of_sessions(home, [removed])
    assert found[removed] == {"o/shop": ["shop"]}, f"a checkout named in a transcript's later line: {found}"
    # ... or only a Codex rollout.
    rollout = os.path.join(home, ".codex", "sessions", "r.jsonl")
    os.makedirs(os.path.dirname(rollout))
    with open(rollout, "w") as fh:
        fh.write(json.dumps({"type": "session_meta", "payload": {"cwd": main}}) + "\n")
    db = sqlite3.connect(os.path.join(home, ".codex", "state_5.sqlite"))
    db.execute("create table threads (rollout_path, model, reasoning_effort, tokens_used, source, created_at)")
    db.execute("insert into threads values (?, 'm', 'high', 1, 'cli', strftime('%s','now'))", (rollout,))
    db.commit()
    db.close()
    found = repos_of_sessions(home, [removed])
    assert found[removed] == {"o/shop": ["shop"]}, f"a checkout only a Codex session ran in: {found}"


def a_removed_orca_worktree_without_one_main_checkout_stays_unattributed(home):
    clone(os.path.join(home, "a", "shop"), "git@github.com:o/shop.git")
    clone(os.path.join(home, "b", "shop"), "git@github.com:p/shop.git")
    clone(os.path.join(home, "c", "shop"), "git@github.com:o/shop.git")
    clone(os.path.join(home, "src", "cafe"), "git@github.com:o/cafe.git")  # no session started there
    removed = os.path.join(home, "orca", "workspaces", "shop", "cart")
    found = repos_of_sessions(home, [os.path.join(home, "a", "shop"), os.path.join(home, "b", "shop"), removed])
    assert found[removed] == {}, f"two checkouts named shop with different remotes: {found}"
    found = repos_of_sessions(home, [os.path.join(home, "a", "shop"), os.path.join(home, "c", "shop"), removed])
    assert found[removed] == {"o/shop": ["shop"]}, f"two clones of one remote are one repo: {found}"
    elsewhere = os.path.join(home, "orca", "workspaces", "cafe", "menu")
    found = repos_of_sessions(home, [os.path.join(home, "a", "shop"), elsewhere])
    assert found[elsewhere] == {}, f"no session ran in a checkout named cafe: {found}"


def a_session_without_builds_lists_none(home):
    found = session(home, [user(0, "hi", "a"), *assistant(1, "m0", "a", [TEXT])])
    assert found["builds"] == [], found["builds"]


for test in (phases_per_branch_and_skill, a_rename_keeps_the_phase, what_ends_a_phase, branches_started_by_commands,
             writing_the_spec_marks_the_work_before_it, what_counts_as_starting_the_code, short_gaps_add_up,
             the_users_attention_per_branch, what_the_user_did_not_type, another_agents_run_is_not_the_user,
             a_session_of_typed_commands, the_kit_names_count_as_their_former_phases,
             builds_come_from_their_transcripts, opus_prices_and_usage_without_the_cache_split,
             advisor_calls_and_what_the_table_cannot_price, a_session_without_builds_lists_none,
             an_orca_worktree_takes_its_repo_from_git, a_removed_orca_worktree_takes_its_repo_from_the_main_checkout,
             a_removed_orca_worktree_finds_its_checkout_anywhere_a_session_ran,
             a_removed_orca_worktree_without_one_main_checkout_stays_unattributed):
    base = tempfile.mkdtemp(prefix="agents-test-extract-sessions-")
    home = os.environ["HOME"]
    try:
        test(base)
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {error}"))
    finally:
        os.environ["HOME"] = home
        shutil.rmtree(base, ignore_errors=True)

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
