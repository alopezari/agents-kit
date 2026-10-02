"""Summarize recent real agent sessions (Claude Code + Codex Desktop) for model/effort/advisor analysis.

Usage: extract_sessions.py [N] [OUTPUT.json]   (defaults: 50, ./sessions.json)
Skips non-interactive runs, subagent/guardian threads and anything under /tmp.
"""
import functools
import glob
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
EXCLUDE_CWD = ("/private/tmp/claude-501", "/tmp/")
CORRECTION = re.compile(
    r"^(no[,. ]|eso no|mal\b|está mal|sigue (fallando|sin)|no funciona|otra vez|te has equivocado|revert|deshaz|"
    r"that's wrong|not what|still (fails|broken|not)|doesn't work|wrong)|\[Request interrupted",
    re.I,
)
# The skills that start a phase of the flow; others (write-pr-description, clarity…) run inside one.
FLOW_SKILLS = {"spec", "self-review", "validate", "create-pr", "follow-pr", "ship"}
TYPED_SKILL = re.compile(r"<command-name>/?([\w:-]+)</command-name>")
SPEC_FILE = re.compile(r"spec-.*\.md$")
# A longer gap between transcript entries is the user away, not work.
ACTIVE_GAP_MINUTES = 15
# A git command where a shell command begins, so a script that merely mentions one doesn't count. A session's
# gitBranch is only its own directory's branch, while its agent often starts each change in a worktree of its own.
GIT_COMMAND = re.compile(r"(?:^|&&|\|\||[;|(])\s*git\s+([^;&|\n)]*)", re.M)
# A heredoc's body is a script's or a PR body's text, not commands this shell runs.
HEREDOC_BODY = re.compile(r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?^\s*\2\s*$", re.S | re.M)
BRANCH_NAME = re.compile(r"[\w][\w./+-]*$")
# Files no repo holds: an edit there is neither the spec nor code.
SCRATCH_PATH = re.compile(r"^(/private)?/tmp/|^/var/folders/|^" + re.escape(HOME + "/.claude/"))
# Xirp names a session's worktree <main checkout>-worktree-<session>; once it is removed, the main checkout says
# which repo it was.
REMOVED_WORKTREE = re.compile(r"(.*)-worktree-[^/]+")
REMOTE_REPO = re.compile(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") in ("text", "input_text"))
    return ""


def is_real_prompt(t):
    t = t.strip()
    return bool(t) and not t.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:", "<task-notification", "[SYSTEM", "<environment_context", "<user_instructions", "# AGENTS.md", "<permissions", "<turn_aborted"))


def phase_events(entry, failed=frozenset()):
    """In block order: ("skill", name) for each flow skill the entry starts (a Skill call by the agent, `/<skill>`
    typed by the user), ("spec", None) for each Edit/Write of a spec (spec-*.md, in .git/agents/ or a temp dir),
    ("edit", None) for each of a file in a repo, and ("branch", (new, renamed)) for each branch a Bash command starts,
    switches to or renames (see branch_change), unless git refused it: `failed` holds the tool-use ids whose result is
    a git error."""
    msg = entry.get("message") or {}
    if entry.get("type") == "user":
        return [("skill", n) for n in (name.split(":")[-1] for name in TYPED_SKILL.findall(text_of(msg.get("content"))))
                if n in FLOW_SKILLS]
    events = []
    for c in msg.get("content") or []:
        if not isinstance(c, dict):
            continue
        tool, args = c.get("name"), c.get("input") or {}
        name = str(args.get("skill")).split(":")[-1] if tool == "Skill" else None
        path = str(args.get("file_path"))
        if name in FLOW_SKILLS:
            events.append(("skill", name))
        elif tool in ("Edit", "Write", "NotebookEdit") and SPEC_FILE.match(os.path.basename(path)):
            events.append(("spec", None))
        elif tool in ("Edit", "Write", "NotebookEdit") and not SCRATCH_PATH.search(path):
            events.append(("edit", None))
        elif tool == "Bash" and c.get("id") not in failed:
            events += [("branch", change) for change in branch_changes(str(args.get("command")))]
    return events


def branch_change(args):
    """(new branch, renamed) a git command's arguments move to: `switch [-c] name`, `checkout -b name`,
    `worktree add -b name path` (renamed None), `branch -m [old] new` (renamed: old, "" for the current branch);
    None for anything else."""
    try:
        words = shlex.split(args)
    except ValueError:  # an unclosed quote: the command continues past what GIT_COMMAND kept
        words = args.split()
    words = [w for w in words if not re.match(r"\d*[<>]", w)]  # redirections
    if words[:1] == ["-C"]:
        words = words[2:]
    if words[:1] == ["worktree"]:
        command, words = " ".join(words[:2]), words[2:]
    else:
        command, words = (words[0] if words else ""), words[1:]
    flags = [w for w in words if w.startswith("-")]
    plain = [w for w in words if not w.startswith("-")]
    creating = {"switch": "cC", "checkout": "bB", "worktree add": "bB"}.get(command)
    name, renamed = None, None
    if command == "branch" and any(f[:2] in ("-m", "-M") for f in flags) and plain:
        name, renamed = (plain[1], plain[0]) if len(plain) > 1 else (plain[0], "")
    elif creating:
        for i, word in enumerate(words[:-1]):
            if word.startswith("-") and not word.startswith("--") and word[-1] in creating:
                name = words[i + 1]
        if not name and command == "switch" and plain and not {"-d", "--detach"} & set(flags):
            name = plain[0]
    return (name, renamed) if name and BRANCH_NAME.match(name) else None


def branch_changes(command):
    """The branch changes a Bash command makes, in order; none for one that sets up a throwaway repo (`git init`)."""
    command = HEREDOC_BODY.sub("", command)
    if re.search(r"\bgit\s+init\b", command):
        return []
    return [change for args in GIT_COMMAND.findall(command) if (change := branch_change(args))]


@functools.lru_cache(maxsize=None)
def checkout(cwd):
    """(GitHub owner/name, the kit's repo name: the main checkout's directory) for a session directory, from its
    main checkout once the worktree is gone; None outside a repo with an origin remote."""
    removed = REMOVED_WORKTREE.match(cwd)
    for directory in dict.fromkeys([cwd, removed.group(1) if removed else cwd]):
        if not os.path.isdir(directory):
            continue
        def git(*args):
            try:
                return subprocess.run(["git", "-C", directory, *args], capture_output=True, text=True, timeout=30).stdout.strip()
            except (OSError, subprocess.TimeoutExpired):  # the session keeps no repo instead of the job stopping
                return ""
        remote = REMOTE_REPO.search(git("config", "--get", "remote.origin.url"))
        common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
        if remote and common:
            name = os.path.basename(os.path.dirname(common)) if os.path.basename(common) == ".git" else os.path.basename(common).removesuffix(".git")
            return remote.group(1), name
    return None


def branch_renames():
    """(repo, old branch) -> new names, from the quality log's rename lines: a renamed branch is the same change."""
    try:
        lines = open(f"{HOME}/.agents/logs/quality.jsonl").read().splitlines()
    except OSError:
        return {}
    renames = {}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("kind") == "rename":
            renames.setdefault((e.get("repo"), e.get("name")), set()).add(e.get("to"))
    return renames


def renamed(renames, repos, old, new):
    """Whether `new` is `old` renamed, maybe several times, in one of the session's repos (by the kit's name)."""
    names, pending = set(), [old]
    while pending:
        current = pending.pop()
        if current not in names:
            names.add(current)
            pending += [n for repo in repos for n in renames.get((repo, current), ())]
    return new in names


def no_spending():
    return {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0, "minutes": 0}


def add_spending(into, spent, sign=1):
    for key, value in spent.items():
        into[key] += sign * value


def failed_calls(entries):
    """Tool calls whose result is an error git printed. A chain fails as a whole, so a later step's error alone
    doesn't undo a switch that happened."""
    return {c.get("tool_use_id") for e in entries if e.get("type") == "user"
            for c in (e.get("message") or {}).get("content") or []
            if isinstance(c, dict) and c.get("is_error") and "fatal:" in json.dumps(c.get("content"))}


def split_into_phases(entries, renames, repos):
    """branch -> phase -> tokens and active minutes. A message belongs to the last flow skill started on its branch
    ("build" before any, and again after the spec once code is edited); a change of branch starts again at
    "build" unless it is a rename (`git branch -m`, or one logged), which carries the branch's spending to the new
    name. The branch changes when the session directory's gitBranch does or a command starts one. An entry without a
    branch counts for the current one and changes nothing.
    Agents write most specs without starting the skill again, so writing the spec file also starts the spec. Either
    way, before the branch's first code edit or other flow skill, the branch's work so far (reading the issue and
    the code) joins the spec."""
    events_of, failed = {}, failed_calls(entries)  # message id -> its events across lines: one line per content block
    for e in entries:
        mid = (e.get("message") or {}).get("id")
        if e.get("type") == "assistant" and mid:
            events_of[mid] = events_of.get(mid, []) + phase_events(e, failed)
    phases, counted = {}, set()
    started, before_code = set(), {}  # branches past their first code edit or flow skill; build spent before that

    def carry(old, new):
        for name, spent in phases.pop(old, {}).items():
            add_spending(phases.setdefault(new, {}).setdefault(name, no_spending()), spent)
        add_spending(before_code.setdefault(new, no_spending()), before_code.pop(old, no_spending()))
        if old in started:
            started.discard(old)
            started.add(new)

    branch, directory_branch, phase, last = "", "", "build", None
    for e in entries:
        msg = e.get("message") or {}
        mid = msg.get("id") if e.get("type") == "assistant" else None
        first_line = bool(mid) and mid not in counted
        events = events_of.get(mid, []) if first_line else ([] if mid else phase_events(e, failed))
        if e.get("gitBranch") and e["gitBranch"] != directory_branch:
            directory_branch = e["gitBranch"]
            events = [("branch", (directory_branch, None))] + events
        for kind, value in events if e.get("gitBranch") or mid else []:
            if kind == "branch":
                new, renamed_from = value
                if renamed_from not in (None, "", branch):  # another branch renamed; the current one stays
                    carry(renamed_from, new)
                elif new != branch:
                    if renamed_from is not None or renamed(renames, repos, branch, new):
                        carry(branch, new)
                    elif branch:
                        phase = "build"
                    branch = new
            elif (kind == "spec" or value == "spec") and branch not in started:
                moved = before_code.pop(branch, no_spending())
                add_spending(phases.setdefault(branch, {}).setdefault("spec", no_spending()), moved)
                add_spending(phases[branch].setdefault("build", no_spending()), moved, sign=-1)
                phase = "spec"
            elif kind == "skill":
                phase = value
                if value != "spec":
                    started.add(branch)
            elif kind == "edit":
                phase = "build" if phase == "spec" else phase
                started.add(branch)
        spent = no_spending()
        if e.get("timestamp"):
            now = ts(e["timestamp"])
            gap = (now - last).total_seconds() / 60 if last else 0
            if 0 < gap <= ACTIVE_GAP_MINUTES:
                spent["minutes"] += gap
            last = max(last, now) if last else now  # a resumed session replays older timestamps
        if first_line:
            counted.add(mid)
            u = msg.get("usage") or {}
            spent["in"] += u.get("input_tokens", 0)
            spent["out"] += u.get("output_tokens", 0)
            spent["cache_read"] += u.get("cache_read_input_tokens", 0)
            spent["cache_write"] += u.get("cache_creation_input_tokens", 0)
        add_spending(phases.setdefault(branch, {}).setdefault(phase, no_spending()), spent)
        if phase == "build" and branch not in started:
            add_spending(before_code.setdefault(branch, no_spending()), spent)
    rounded = {b: {p: {**v, "minutes": round(v["minutes"], 1)} for p, v in ps.items()} for b, ps in phases.items() if b}
    kept = {b: {p: v for p, v in ps.items() if any(v.values())} for b, ps in rounded.items()}
    return {b: ps for b, ps in kept.items() if ps}


def claude_sessions():
    renames = branch_renames()
    for f in glob.glob(f"{HOME}/.claude/projects/*/*.jsonl"):
        sid = os.path.basename(f)[:-6]
        prompts, models, efforts, times, tools, advisor = [], {}, set(), [], 0, 0
        tokens = {"in": 0, "out": 0, "cache_read": 0}
        cwd = None
        branches, repos = set(), {}
        entries, lines_seen, messages_seen = [], set(), set()
        for line in open(f, errors="ignore"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("isSidechain") or e.get("uuid") in lines_seen and e.get("uuid"):
                continue  # a subagent's line, or one a resumed session replays
            lines_seen.add(e.get("uuid"))
            entries.append(e)
            if e.get("timestamp"):
                times.append(e["timestamp"])
            cwd = cwd or e.get("cwd")
            if e.get("cwd") and checkout(e["cwd"]):
                remote, local = checkout(e["cwd"])
                repos.setdefault(remote, set()).add(local)
            if e.get("gitBranch"):
                branches.add(e["gitBranch"])
            msg = e.get("message") or {}
            if e.get("type") == "user" and msg.get("role") == "user":
                t = text_of(msg.get("content"))
                if is_real_prompt(t):
                    prompts.append(t)
            elif e.get("type") == "assistant":
                blocks = [c for c in msg.get("content") or [] if isinstance(c, dict)]
                tools += sum(1 for c in blocks if c.get("type") == "tool_use")
                advisor += sum(1 for c in blocks if "advisor" in str(c.get("type", "")) or c.get("name") == "advisor")
                if msg.get("id") in messages_seen:
                    continue  # the same message again: one line per content block, each repeating its usage
                messages_seen.add(msg.get("id"))
                m = msg.get("model")
                if m and m != "<synthetic>":
                    models[m] = models.get(m, 0) + 1
                if e.get("effort"):
                    efforts.add(str(e["effort"]))
                u = msg.get("usage") or {}
                tokens["in"] += u.get("input_tokens", 0)
                tokens["out"] += u.get("output_tokens", 0)
                tokens["cache_read"] += u.get("cache_read_input_tokens", 0)
        failed = failed_calls(entries)
        branches.update(change[0] for e in entries for kind, change in phase_events(e, failed) if kind == "branch")
        if prompts and times and not (cwd or "").startswith(EXCLUDE_CWD):
            yield dict(harness="claude-code", id=sid, cwd=cwd, start=min(times), end=max(times), prompts=prompts,
                       models=models, effort=sorted(efforts), tokens=tokens, tool_calls=tools, advisor_calls=advisor,
                       branches=sorted(branches), repos={r: sorted(names) for r, names in repos.items()},
                       phases=split_into_phases(entries, renames, {n for names in repos.values() for n in names}))


def codex_sessions():
    import shutil
    import sqlite3
    import tempfile
    # A WAL database opened with mode=ro fails when Codex isn't running (no -shm to attach to), so read a copy.
    snap = tempfile.mkdtemp(prefix="codex-state-")
    for suffix in ("", "-wal"):
        if os.path.exists(f"{HOME}/.codex/state_5.sqlite{suffix}"):
            shutil.copy(f"{HOME}/.codex/state_5.sqlite{suffix}", f"{snap}/state.sqlite{suffix}")
    db = sqlite3.connect(f"{snap}/state.sqlite")
    rows = db.execute("select rollout_path, model, reasoning_effort, tokens_used from threads "
                      "where source in ('vscode','cli') and created_at > strftime('%s','now','-90 days')").fetchall()
    db.close()
    shutil.rmtree(snap)
    for f, model, effort, tokens_used in rows:
        if not f or not os.path.exists(f):
            continue
        for s in _codex_rollout(f):
            if model:
                s["models"] = {model: max(1, sum(s["models"].values()))}
            if effort:
                s["effort"] = [effort]
            s["tokens"]["total_incl_cache"] = tokens_used
            yield s


def _codex_rollout(f):
    for _ in [0]:
        prompts, models, efforts, times, tools = [], {}, set(), [], 0
        tokens = {"in": 0, "out": 0, "cache_read": 0}
        meta = {}
        for line in open(f, errors="ignore"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            p = e.get("payload") or {}
            if e.get("timestamp"):
                times.append(e["timestamp"])
            if e.get("type") == "session_meta":
                meta = p
            elif e.get("type") == "turn_context":
                if p.get("model"):
                    models[p["model"]] = models.get(p["model"], 0) + 1
                if p.get("effort"):
                    efforts.add(str(p["effort"]))
            elif e.get("type") == "event_msg" and p.get("type") == "user_message":
                t = p.get("message") or ""
                if is_real_prompt(t) and t not in prompts:
                    prompts.append(t)
            elif e.get("type") == "response_item" and p.get("type") == "message" and p.get("role") == "user":
                t = text_of(p.get("content"))
                if is_real_prompt(t) and t not in prompts:
                    prompts.append(t)
            elif e.get("type") == "event_msg" and p.get("type") == "token_count":
                info = (p.get("info") or {}).get("total_token_usage") or {}
                if info:
                    tokens = {"in": info.get("input_tokens", 0), "out": info.get("output_tokens", 0),
                              "cache_read": info.get("cached_input_tokens", 0)}
            elif e.get("type") == "response_item" and p.get("type") in ("function_call", "custom_tool_call", "local_shell_call"):
                tools += 1
        src = meta.get("source")
        cwd = meta.get("cwd") or ""
        if isinstance(src, dict) or meta.get("originator") == "codex_exec" or cwd.startswith(EXCLUDE_CWD):
            continue  # subagents, auto-review and non-interactive runs
        if prompts and times:
            yield dict(harness="codex", id=os.path.basename(f)[:-6], cwd=cwd, start=min(times), end=max(times),
                       prompts=prompts, models=models, effort=sorted(efforts), tokens=tokens, tool_calls=tools,
                       originator=meta.get("originator"), branches=[b for b in [(meta.get("git") or {}).get("branch")] if b],
                       repos={checkout(cwd)[0]: [checkout(cwd)[1]]} if cwd and checkout(cwd) else {})


def summarize(s):
    corrections = [p[:200] for p in s["prompts"][1:] if CORRECTION.search(p.strip())]
    return {
        "harness": s["harness"], "id": s["id"], "project": (s["cwd"] or "").replace(HOME, "~"),
        "start": s["start"][:16], "start_iso": s["start"], "branches": s.get("branches", []), "repos": s.get("repos", {}), "minutes": round((ts(s["end"]) - ts(s["start"])).total_seconds() / 60),
        "models": s["models"], "effort": s["effort"], "tokens": s["tokens"], "tool_calls": s["tool_calls"],
        "user_turns": len(s["prompts"]), "advisor_calls": s.get("advisor_calls", 0), "first_prompt": s["prompts"][0][:1500],
        "later_prompts": [p[:300] for p in s["prompts"][1:8]], "correction_signals": corrections[:6],
        "phases": s.get("phases", {}),
    }


if __name__ == "__main__":
    N = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    OUT = sys.argv[2] if len(sys.argv) > 2 else "sessions.json"
    sessions = sorted(list(claude_sessions()) + list(codex_sessions()), key=lambda s: s["start"], reverse=True)
    last = [summarize(s) for s in sessions[:N]]
    json.dump(last, open(OUT, "w"), indent=1, ensure_ascii=False)
    print(f"total real sessions: {len(sessions)}; kept {len(last)}; from {last[-1]['start']} to {last[0]['start']}")
    from collections import Counter
    print(Counter(s["harness"] for s in last))
    print(Counter(m for s in last for m in s["models"]))
    print("effort:", Counter(e for s in last for e in s["effort"]), "advisor calls:", sum(s["advisor_calls"] for s in last))
