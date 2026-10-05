#!/usr/bin/env python3
"""The flow phases the user switched off, for one branch, one repo or every repo.

The user switches a phase with the first line of a message, `phase off <phase> [branch|repo|global]` or
`phase on ...`, which prompt_approvals.py records here. The store is in ~/.agents/approvals, which guard_bash.py and
guard_files.py keep the agent out of: only the user's messages switch a phase.

The narrowest switch set wins (branch over repo over global), and validate off takes staging off: a staging guide
comes out of validation. A skip writes no stamp, so a phase switched back on needs a real one again.
"""
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import design_files  # noqa: E402
import review_stamp  # noqa: E402

PHASES = ("spec", "self-review", "validate", "staging", "verify", "ci", "follow-pr", "audit")
SCOPES = ("branch", "repo", "global")
STORE = os.path.join(design_files.APPROVALS_DIR, "phases")
PHASE_LINE = re.compile(r"\A[ \t]*phase[ \t]+(off|on)[ \t]+(\S+)(?:[ \t]+(\S+))?[ \t]*$", re.I | re.M)


class StoreError(Exception):
    pass


def checkout(cwd, timeout=5):
    """(repo, branch, git common dir) of the checkout in cwd; branch is None when detached, all None outside a repo."""
    out = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir", "--abbrev-ref", "HEAD"],
                         cwd=cwd, capture_output=True, text=True, timeout=timeout)
    lines = out.stdout.splitlines()
    if out.returncode != 0 or len(lines) != 2:
        return None, None, None
    return review_stamp.repo_name_of(lines[0]), (None if lines[1] == "HEAD" else lines[1]), lines[0]


def store_files(repo, branch):
    """[(scope, path)] from the widest scope to the narrowest, for the scopes that apply."""
    files = [("global", os.path.join(STORE, "global.json"))]
    if repo:
        files.append(("repo", os.path.join(STORE, "repo", repo + ".json")))
        if branch:
            files.append(("branch", os.path.join(STORE, "branch", repo, review_stamp.branch_key(branch) + ".json")))
    return files


def read(path):
    try:
        with open(path) as fh:
            switches = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise StoreError(f"Couldn't read the phase switches in {path}: {error}") from error
    if not isinstance(switches, dict) or any(p not in PHASES or v not in ("on", "off") for p, v in switches.items()):
        raise StoreError(f"Couldn't read the phase switches in {path}: expected {{phase: \"on\" | \"off\"}}")
    return switches


def phases_off(cwd, timeout=5):
    """{phase: why it is off} for the checkout in cwd. Raises StoreError when a switch file can't be read."""
    state = {}
    for scope, path in store_files(*checkout(cwd, timeout)[:2]):
        state.update({phase: (value, scope) for phase, value in read(path).items()})
    off = {phase: scope for phase, (value, scope) in state.items() if value == "off"}
    if "validate" in off:
        off["staging"] = "validate off"
    return off


def phases_off_or_error(cwd, timeout=5):
    """(phases off, None), or ({}, why) when the store can't be read: an unreadable store leaves every phase on."""
    try:
        return phases_off(cwd, timeout), None
    except StoreError as error:
        return {}, str(error)


def describe(off):
    return ", ".join(f"{phase} ({off[phase]})" for phase in PHASES if phase in off) or "none"


def apply_phase_line(prompt, cwd):
    """Record the switch the message's first line asks for. Returns what to tell the agent, or None when the
    message doesn't open with a phase line."""
    line = PHASE_LINE.match(prompt)
    if not line:
        return None
    action, phase, scope = line.group(1).lower(), line.group(2).lower(), (line.group(3) or "branch").lower()
    if phase not in PHASES:
        return (f"Unknown phase `{phase}` in the user's `phase {action}` line: nothing was switched. "
                f"Tell the user; the phases are {', '.join(PHASES)}.")
    if scope not in SCOPES:
        return (f"Unknown scope `{scope}` in the user's `phase {action}` line: nothing was switched. "
                f"Tell the user; the scopes are {', '.join(SCOPES)} (branch when left out).")
    repo, branch, common = checkout(cwd)
    if scope != "global" and not repo:
        return (f"The user's `phase {action} {phase} {scope}` needs a git repository, and {cwd} isn't in one: "
                "nothing was switched. Tell the user; `global` works anywhere.")
    if scope == "branch" and not branch:
        return (f"The user's `phase {action} {phase}` is for this branch, but HEAD is on no branch: nothing was "
                "switched. Tell the user; `repo` or `global` works here.")
    path = dict(store_files(repo, branch))[scope]
    try:
        switches = read(path)
        switches[phase] = action
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as fh:
            json.dump(switches, fh, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
        off = phases_off(cwd)
        if branch:  # bin/phase's cached label; other checkouts' age out within its 15 s
            try:
                os.remove(os.path.join(common, "agents", "phase", review_stamp.branch_key(branch) + ".json"))
            except FileNotFoundError:
                pass
    except (OSError, StoreError) as error:
        return f"Couldn't switch {phase} {action} as the user asked: {error}. Tell the user."
    where = {"branch": f"branch {branch} of {repo}", "repo": f"every branch of {repo}", "global": "every repo"}[scope]
    still = " A narrower switch keeps it on here." if action == "off" and phase not in off else ""
    return (f"The user switched {phase} {action} for {where}.{still} Phases off here now: {describe(off)}. "
            "Confirm it to the user in one line, and skip the steps of the phases off.")
