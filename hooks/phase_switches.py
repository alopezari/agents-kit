#!/usr/bin/env python3
"""The flow phases the user switched off, for one branch, one repo or every repo.

The user switches a phase with the first line of a message, `phase off <phase> [branch|repo|global]` or
`phase on ...`, which prompt_approvals.py records here, or in Claude Code with `/flow off|on …`, which the kit's mod
runs through `phase_switches.py set`. The store is in ~/.agents/approvals, which guard_bash.py and
guard_files.py keep the agent out of: only the user switches a phase.

The narrowest switch set wins (branch over repo over global), and validate off takes staging off: a staging guide
comes out of validation. A skip writes no stamp, so a phase switched back on needs a real one again.

The same files hold how many re-check rounds the self-review runs after its first pass, set the same ways with
`review-rounds <0-9> [branch|repo|global]` or `/flow review-rounds …`, and read with `bin/phase review-rounds`.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fcntl  # noqa: E402

import design_files  # noqa: E402
import review_stamp  # noqa: E402
from hooklog import log  # noqa: E402

PHASES = ("spec", "self-review", "validate", "staging", "verify", "ci", "follow-pr", "audit", "second-model", "claude-review", "codex-review")
SCOPES = ("branch", "repo", "global")
STORE = os.path.join(design_files.APPROVALS_DIR, "phases")
PHASE_LINE = re.compile(r"\A[ \t]*phase[ \t]+(off|on)(?=\s|\Z)(.*)", re.I)
ROUNDS = "review-rounds"
ROUNDS_LINE = re.compile(r"\A[ \t]*review-rounds(?=\s|\Z)(.*)", re.I)
DEFAULT_ROUNDS = 1


class StoreError(Exception):
    pass


def checkout(cwd, timeout=5):
    """(repo, branch, git common dir) of the checkout in cwd; branch is None when detached, all None outside a repo.
    Raises OSError or subprocess.SubprocessError when git can't answer."""
    deadline = time.monotonic() + timeout  # both calls share the caller's budget: the guard's runs out at 10 s
    env = {**os.environ, "LC_ALL": "C"}  # the "not a git repository" check reads English
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd,
                            capture_output=True, text=True, timeout=timeout, env=env)
    if common.returncode != 0:
        # Only "not a repository" means no repo: any other failure must not drop the repo's and branch's switches.
        if "not a git repository (or any of the parent directories)" in common.stderr:  # not a broken worktree
            return None, None, None
        raise OSError(f"git rev-parse failed in {cwd}: {common.stderr.strip()[-200:]}")
    repo = review_stamp.repo_name_of(common.stdout.strip())
    if repo in ("", ".", ".."):  # `..` would turn a repo's file into a path outside its directory
        raise OSError(f"can't name the repository of {cwd}")
    # The full ref: --short, like rev-parse --abbrev-ref, says heads/<branch> when a tag has the branch's name.
    head = subprocess.run(["git", "symbolic-ref", "--quiet", "HEAD"], cwd=cwd, capture_output=True, text=True,
                          timeout=max(deadline - time.monotonic(), 0.1), env=env)
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
    if not isinstance(switches, dict) or any(not valid_switch(k, v) for k, v in switches.items()):
        raise StoreError(f"Couldn't read the phase switches in {path}: expected {{phase: \"on\" | \"off\"}}, and "
                         f"{ROUNDS} a number from 0 to 9")
    return switches


def valid_switch(key, value):
    if key == ROUNDS:
        return type(value) is int and 0 <= value <= 9  # bool is an int subclass: true isn't a count
    return value in ("on", "off")


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


def review_rounds(cwd, timeout=5):
    """(re-check rounds after the self-review's first pass, the scope that set them or "default"). Raises StoreError
    when a switch file can't be read."""
    return review_rounds_in(*checkout(cwd, timeout)[:2])


def review_rounds_in(repo, branch):
    rounds = DEFAULT_ROUNDS, "default"
    for scope, path in store_files(repo, branch):
        switches = read(path)
        if ROUNDS in switches:
            rounds = switches[ROUNDS], scope
    return rounds


def phases_off_or_error(cwd, timeout=5):
    """(phases off, None), or ({}, a sentence saying why) when the switches can't be read: then every phase is on."""
    try:
        return phases_off(cwd, timeout), None
    except (StoreError, OSError, subprocess.SubprocessError, ValueError) as error:
        return {}, f"{error}, so every phase counts as on: tell the user"


def describe(off):
    return ", ".join(f"{phase} ({off[phase]})" for phase in PHASES if phase in off) or "none"


def apply_phase_line(prompt, cwd):
    """Record the switch the message's first line asks for. Returns what to tell the agent, or None when the
    message doesn't open with a phase or review-rounds line."""
    rounds = ROUNDS_LINE.match(prompt)
    if rounds:
        switched, said, hint = switch(ROUNDS, rounds.group(1).lower().split(), cwd,
                                      f"`{ROUNDS} <0-9> [branch|repo|global]`")
        then = ("Confirm it to the user in one line; the self-review runs that many re-checks." if switched else
                f"Tell the user; {hint}")
        return f"The user's `{ROUNDS}` line: {said} {then}"
    line = PHASE_LINE.match(prompt)
    if not line:
        return None
    action, words = line.group(1).lower(), line.group(2).lower().split()
    switched, said, hint = switch(action, words, cwd, f"`phase {action} <phase> [branch|repo|global]`")
    then = ("Confirm it to the user in one line, and skip the steps of the phases off." if switched else
            f"Tell the user; {hint}")
    return f"The user's `phase {action}` line: {said} {then}"


def switch(action, words, cwd, form):
    """Record `<action> <words>` for the checkout in cwd, asked in `form`: (whether it was recorded, what happened,
    and when it wasn't, what the user can do instead). The action is off, on or review-rounds."""
    if not 1 <= len(words) <= 2:
        return False, f"Not {form}: nothing was switched.", \
            f"the form is {form}, branch when left out." if action == ROUNDS else f"the phases are {', '.join(PHASES)}."
    phase, scope = words[0], words[1] if len(words) == 2 else "branch"
    if action == ROUNDS:
        if not re.fullmatch(r"[0-9]", phase):  # not isdigit(), which takes "²"
            return False, f"`{phase}` isn't a number of re-check rounds from 0 to 9: nothing was switched.", \
                f"the form is {form}, branch when left out."
        key, value, what = ROUNDS, int(phase), f"{ROUNDS} to {phase}"
    elif phase not in PHASES:
        return False, f"Unknown phase `{phase}`: nothing was switched.", f"the phases are {', '.join(PHASES)}."
    else:
        key, value, what = phase, action, f"{phase} {action}"
    if scope not in SCOPES:
        return False, f"Unknown scope `{scope}`: nothing was switched.", f"the form is {form}, branch when left out."
    try:
        repo, branch, common = checkout(cwd)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return False, f"Couldn't switch {what}, git failed: {error}. Nothing was switched.", \
            "try again once git works there."
    if scope != "global" and not repo:
        return False, f"Can't switch {what} for the {scope}: {cwd} isn't in a git repository, so nothing was " \
            "switched.", "the global scope works anywhere."
    if scope == "branch" and not branch:
        return False, f"Can't switch {what} for this branch: HEAD in {cwd} is on no branch, so nothing was " \
            "switched.", "the repo or global scope works there."
    path = dict(store_files(repo, branch))[scope]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Two sessions switching at once: the lock keeps one from writing back what it read before the other wrote.
        with open(os.path.join(STORE, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            switches = read(path)
            switches[key] = value
            fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(switches, fh, indent=1, sort_keys=True)
            os.replace(temporary, path)
    except (OSError, StoreError) as error:
        return False, f"Couldn't switch {what}: {error}. Nothing was switched.", \
            "fix or delete the file the error names."
    if branch:  # bin/phase's cached label; other checkouts' age out within its 15 s
        try:
            os.remove(review_stamp.phase_cache(common, branch))
        except OSError:
            pass
    where = {"branch": f"branch {branch} of {repo}", "repo": f"every branch of {repo}", "global": "every repo"}[scope]
    try:
        off = phases_off_in(repo, branch)
        rounds, set_in = review_rounds_in(repo, branch)
    except StoreError as error:
        return True, (f"Switched {what} for {where}, but {str(error)[0].lower() + str(error)[1:]}, so every "
                      "phase counts as on here until that file is fixed or deleted."), None
    if action == ROUNDS:
        still = " A narrower setting overrides it here." if set_in != scope else ""
        return True, f"Switched {what} for {where}.{still} Review re-check rounds here now: {rounds} ({set_in}).", None
    still = (" A narrower switch keeps it on here." if action == "off" and phase not in off else
             " A narrower switch keeps it off here." if action == "on" and phase in off else "")
    return True, f"Switched {phase} {action} for {where}.{still} Phases off here now: {describe(off)}.", None


REFUSED = 3  # not 1: Python exits 1 on a crash, which may come after the write


def branch_switches(repo, branch):
    """The file holding a branch's own switches, or None when it has none. Raises OSError when it can't tell."""
    path = dict(store_files(repo, branch))["branch"]
    try:
        os.stat(path)  # not os.path.exists, which says no when the store can't be searched
    except FileNotFoundError:
        return None
    return path


def forget_branch(repo, branch, common):
    """Remove a deleted branch's switches and bin/phase's cached label for it, so a new branch of that name starts with
    every phase on. True when it had switches. Raises OSError."""
    try:
        os.remove(review_stamp.phase_cache(common, branch))
    except FileNotFoundError:
        pass
    try:
        with open(os.path.join(STORE, ".lock"), "w") as lock:  # not under a switch() writing it back
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.remove(dict(store_files(repo, branch))["branch"])
    except FileNotFoundError:  # no store, or no switches for this branch
        return False
    return True


def main():
    """`set <off|on> <phase> [branch|repo|global]` or `set review-rounds <0-9> [branch|repo|global]`, a scope also as
    `--repo`: the /flow command's way in. The shell guard keeps the agent from running it; the mod runs it only for a
    command the user typed."""
    if sys.argv[1:2] != ["set"] or len(sys.argv) < 3:
        print("usage: phase_switches.py set <off|on> <phase> | review-rounds <0-9> [branch|repo|global|--repo|--global]",
              file=sys.stderr)
        return 2
    action = sys.argv[2].lower()
    words = [word[2:] if word[2:] in SCOPES and word.startswith("--") else word for word in map(str.lower, sys.argv[3:])]
    if action not in ("off", "on", ROUNDS):
        print(f"`{action}` must be off, on or {ROUNDS}: nothing was switched.")
        return REFUSED
    cwd = os.getcwd()
    form = f"`/flow {ROUNDS} <0-9> [--repo|--global]`" if action == ROUNDS else f"`/flow {action} <phase> [--repo|--global]`"
    switched, said, hint = switch(action, words, cwd, form)
    if switched:
        log("phase_switches", "phase-switch", {"cwd": cwd}, "/flow: " + said)
    print(said if switched else f"{said} {hint[0].upper() + hint[1:]}")
    return 0 if switched else REFUSED


if __name__ == "__main__":
    sys.exit(main())
