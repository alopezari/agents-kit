"""Summarize recent real agent sessions (Claude Code + Codex Desktop) for model/effort/advisor analysis.

Usage: extract_sessions.py [N] [OUTPUT.json]   (defaults: 50, ./sessions.json)
Skips non-interactive runs, subagent/guardian threads and anything under /tmp.
"""
import glob
import json
import os
import re
import sys
from datetime import datetime

HOME = os.path.expanduser("~")
EXCLUDE_CWD = ("/private/tmp/claude-501", "/tmp/")
CORRECTION = re.compile(
    r"^(no[,. ]|eso no|mal\b|está mal|sigue (fallando|sin)|no funciona|otra vez|te has equivocado|revert|deshaz|"
    r"that's wrong|not what|still (fails|broken|not)|doesn't work|wrong)|\[Request interrupted",
    re.I,
)


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


def claude_sessions():
    for f in glob.glob(f"{HOME}/.claude/projects/*/*.jsonl"):
        sid = os.path.basename(f)[:-6]
        prompts, models, efforts, times, tools, advisor = [], {}, set(), [], 0, 0
        tokens = {"in": 0, "out": 0, "cache_read": 0}
        cwd = None
        branches = set()
        for line in open(f, errors="ignore"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("isSidechain"):
                continue
            if e.get("timestamp"):
                times.append(e["timestamp"])
            cwd = cwd or e.get("cwd")
            if e.get("gitBranch"):
                branches.add(e["gitBranch"])
            msg = e.get("message") or {}
            if e.get("type") == "user" and msg.get("role") == "user":
                t = text_of(msg.get("content"))
                if is_real_prompt(t):
                    prompts.append(t)
            elif e.get("type") == "assistant":
                m = msg.get("model")
                if m and m != "<synthetic>":
                    models[m] = models.get(m, 0) + 1
                if e.get("effort"):
                    efforts.add(str(e["effort"]))
                u = msg.get("usage") or {}
                tokens["in"] += u.get("input_tokens", 0)
                tokens["out"] += u.get("output_tokens", 0)
                tokens["cache_read"] += u.get("cache_read_input_tokens", 0)
                blocks = [c for c in msg.get("content") or [] if isinstance(c, dict)]
                tools += sum(1 for c in blocks if c.get("type") == "tool_use")
                advisor += sum(1 for c in blocks if "advisor" in str(c.get("type", "")) or c.get("name") == "advisor")
        if prompts and times and not (cwd or "").startswith(EXCLUDE_CWD):
            yield dict(harness="claude-code", id=sid, cwd=cwd, start=min(times), end=max(times), prompts=prompts,
                       models=models, effort=sorted(efforts), tokens=tokens, tool_calls=tools, advisor_calls=advisor,
                       branches=sorted(branches))


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
                       originator=meta.get("originator"), branches=[b for b in [(meta.get("git") or {}).get("branch")] if b])


def summarize(s):
    corrections = [p[:200] for p in s["prompts"][1:] if CORRECTION.search(p.strip())]
    return {
        "harness": s["harness"], "id": s["id"], "project": (s["cwd"] or "").replace(HOME, "~"),
        "start": s["start"][:16], "start_iso": s["start"], "branches": s.get("branches", []), "minutes": round((ts(s["end"]) - ts(s["start"])).total_seconds() / 60),
        "models": s["models"], "effort": s["effort"], "tokens": s["tokens"], "tool_calls": s["tool_calls"],
        "user_turns": len(s["prompts"]), "advisor_calls": s.get("advisor_calls", 0), "first_prompt": s["prompts"][0][:1500],
        "later_prompts": [p[:300] for p in s["prompts"][1:8]], "correction_signals": corrections[:6],
    }


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
