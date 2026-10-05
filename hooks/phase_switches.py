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
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fcntl  # noqa: E402

import design_files  # noqa: E402
import review_stamp  # noqa: E402

PHASES = ("spec", "self-review", "validate", "staging", "verify", "ci", "follow-pr", "audit")
SCOPES = ("branch", "repo", "global")
STORE = os.path.join(design_files.APPROVALS_DIR, "phases")
PHASE_LINE = re.compile(r"\A[ \t]*phase[ \t]+(off|on)\b(.*)", re.I)


class StoreError(Exception):
    pass


def checkout(cwd, timeout=5):
    """(repo, branch, git common dir) of the checkout in cwd; branch is None when detached, all None outside a repo.
    Raises OSError or subprocess.SubprocessError when git can't answer."""
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                            capture_output=True, text=True, timeout=timeout)
    if common.returncode != 0:
        # Only "not a repository" means no repo: any other failure must not drop the repo's and branch's switches.
        if "not a git repository" in common.stderr:
            return None, None, None
        raise OSError(f"git rev-parse failed in {cwd}: {common.stderr.strip()[-200:]}")
    repo = review_stamp.repo_name_of(common.stdout.strip())
    if repo in ("", ".", ".."):  # `..` would turn a repo's file into a path outside its directory
        raise OSError(f"can't name the repository of {cwd}")
    # The full ref: --short, like rev-parse --abbrev-ref, says heads/<branch> when a tag has the branch's name.
    head = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)
    if head.returncode not in (0, 1):  # 1: detached
        raise OSError(f"git symbolic-ref failed in {cwd}: {head.stderr.strip()[-200:]}")
    ref = head.stdout.strip()
    return repo, ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else None, common.stdout.strip()


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
    except (OSError, ValueError, RecursionError) as error:
        raise StoreError(f"Couldn't read the phase switches in {path}: {error}") from error
    if not isinstance(switches, dict) or any(v not in ("on", "off") for v in switches.values()):
        raise StoreError(f"Couldn't read the phase switches in {path}: expected {{phase: \"on\" | \"off\"}}")
    return switches


def phases_off(cwd, timeout=5):
    """{phase: why it is off} for the checkout in cwd. Raises StoreError when a switch file can't be read."""
    return phases_off_in(*checkout(cwd, timeout)[:2])


def phases_off_in(repo, branch):
    state = {}
    for scope, path in store_files(repo, branch):
        # A phase a newer kit added isn't an error: an older hook running beside it must not hold every gate.
        state.update({phase: (value, scope) for phase, value in read(path).items() if phase in PHASES})
    off = {phase: scope for phase, (value, scope) in state.items() if value == "off"}
    if "validate" in off:
        off["staging"] = "validate off"
    return off


def phases_off_or_error(cwd, timeout=5):
    """(phases off, None), or ({}, a sentence saying why) when the switches can't be read: then every phase is on."""
    try:
        return phases_off(cwd, timeout), None
    except (StoreError, OSError, subprocess.SubprocessError) as error:
        return {}, f"{error}, so every phase counts as on: tell the user"


def describe(off):
    return ", ".join(f"{phase} ({off[phase]})" for phase in PHASES if phase in off) or "none"


def apply_phase_line(prompt, cwd):
    """Record the switch the message's first line asks for. Returns what to tell the agent, or None when the
    message doesn't open with a phase line."""
    line = PHASE_LINE.match(prompt)
    if not line:
        return None
    action, words = line.group(1).lower(), line.group(2).lower().split()
    if not 1 <= len(words) <= 2:
        return (f"The user's message opens with `phase {action}` but not as `phase {action} <phase> [branch|repo|global]`: "
                f"nothing was switched. Tell the user; the phases are {', '.join(PHASES)}.")
    phase, scope = words[0], words[1] if len(words) == 2 else "branch"
    if phase not in PHASES:
        return (f"Unknown phase `{phase}` in the user's `phase {action}` line: nothing was switched. "
                f"Tell the user; the phases are {', '.join(PHASES)}.")
    if scope not in SCOPES:
        return (f"Unknown scope `{scope}` in the user's `phase {action}` line: nothing was switched. "
                f"Tell the user; the scopes are {', '.join(SCOPES)} (branch when left out).")
    try:
        repo, branch, common = checkout(cwd)
    except (OSError, subprocess.SubprocessError) as error:
        return f"Couldn't switch {phase} {action} as the user asked: git failed in {cwd} ({error}). Tell the user."
    if scope != "global" and not repo:
        return (f"The user's `phase {action} {phase} {scope}` needs a git repository, and {cwd} isn't in one: "
                "nothing was switched. Tell the user; `global` works anywhere.")
    if scope == "branch" and not branch:
        return (f"The user's `phase {action} {phase}` is for this branch, but HEAD is on no branch: nothing was "
                "switched. Tell the user; `repo` or `global` works here.")
    path = dict(store_files(repo, branch))[scope]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Two sessions switching at once: the lock keeps one from writing back what it read before the other wrote.
        with open(os.path.join(STORE, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            switches = read(path)
            switches[phase] = action
            fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(switches, fh, indent=1, sort_keys=True)
            os.replace(temporary, path)
    except (OSError, StoreError) as error:
        return (f"Couldn't switch {phase} {action} as the user asked: {error}. Nothing was switched. Tell the user; "
                "a file that can't be read is theirs to fix or delete.")
    if branch:  # bin/phase's cached label; other checkouts' age out within its 15 s
        try:
            os.remove(review_stamp.phase_cache(common, branch))
        except OSError:
            pass
    where = {"branch": f"branch {branch} of {repo}", "repo": f"every branch of {repo}", "global": "every repo"}[scope]
    try:
        off = phases_off_in(repo, branch)
    except StoreError as error:
        return (f"The user switched {phase} {action} for {where}, but {error}, so every phase counts as on here until "
                "they fix or delete it. Tell the user.")
    still = " A narrower switch keeps it on here." if action == "off" and phase not in off else ""
    return (f"The user switched {phase} {action} for {where}.{still} Phases off here now: {describe(off)}. "
            "Confirm it to the user in one line, and skip the steps of the phases off.")
