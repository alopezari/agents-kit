#!/usr/bin/env python3
"""The flow phases the user switched off, for one branch, one repo or every repo.

The user switches a phase with the first line of a message, `phase off <phase> [branch|repo|global]` or
`phase on ...`, which prompt_approvals.py records here, or in Claude Code with `/flow off|on …`, which the kit's mod
runs through `phase_switches.py set`. The store is in ~/.agents/approvals, which guard_bash.py and
guard_files.py keep the agent out of: only the user switches a phase.

A repo is the path of its git common dir, so its worktrees share switches and a clone elsewhere doesn't; a renamed
branch keeps its switches. The narrowest switch set wins (branch over repo over global). A skip writes no stamp, so a
phase switched back on needs a real one again.

The same files hold how many re-check rounds the review runs after its first pass, set the same ways with
`review-rounds <0-9> [branch|repo|global]` or `/flow review-rounds …`, and read with `bin/phase review-rounds`.
"""
import hashlib
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

PHASES = ("spec", "review", "validate", "verify", "ci", "follow-pr", "audit", "second-model", "claude-review", "codex-review")
SCOPES = ("branch", "repo", "global")
STORE = os.path.join(design_files.APPROVALS_DIR, "phases")
# A phase's former name, still taken from the user and read from switches stored under it.
FORMER_NAMES = {"self-review": "review"}
PHASE_LINE = re.compile(r"\A[ \t]*phase[ \t]+(off|on)(?=\s|\Z)(.*)", re.I)
ROUNDS = "review-rounds"
ROUNDS_LINE = re.compile(r"\A[ \t]*review-rounds(?=\s|\Z)(.*)", re.I)
DEFAULT_ROUNDS = 1
RENAMED = re.compile(r"Branch: renamed refs/heads/(.+) to refs/heads/")


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


def repo_key(common):
    """The repo's name in the store: its directory name, plus a short hash of its git common dir's path, so two repos
    with one name keep their own switches and every worktree of one shares them."""
    real = os.path.realpath(common)
    return f"{review_stamp.repo_name_of(common)}-{hashlib.sha256(os.fsencode(real)).hexdigest()[:8]}"


def store_files(common, branch):
    """[(scope, path)] from the widest scope to the narrowest, for the scopes that apply to a branch of the repo whose
    git common dir is `common`."""
    files = [("global", os.path.join(STORE, "global.json"))]
    if common:
        key = repo_key(common)
        files.append(("repo", os.path.join(STORE, "repo", key + ".json")))
        if branch:
            files.append(("branch", os.path.join(STORE, "branch", key, review_stamp.branch_key(branch) + ".json")))
    return files


def followed_checkout(cwd, timeout):
    """(git common dir, branch) of the checkout in cwd, once a renamed branch has its earlier name's switches."""
    deadline = time.monotonic() + timeout
    _, branch, common = checkout(cwd, timeout)
    if branch:
        follow_rename(common, branch, deadline)
    return common, branch


def follow_rename(common, branch, deadline):
    """Move the switches of this branch's earlier name to it, the first time it is read or written after
    `git branch -m`: before then the guard may read it, so no stamp path can do this. Raises OSError or
    subprocess.SubprocessError when its reflog or the move fails."""
    if earlier_switches(common, branch, deadline):
        with open(os.path.join(STORE, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            source = earlier_switches(common, branch, deadline)  # again: another process may have moved them
            if source:
                os.rename(source, dict(store_files(common, branch))["branch"])
                log("phase_switches", "phase-switch-rename", {"cwd": common},
                    f"moved {os.path.basename(source)} to the renamed branch {branch}")


def earlier_switches(common, branch, deadline):
    """The switch file of this branch's newest earlier name that has one, or None when the branch has its own. A name
    that is a branch again keeps its file: a fallback read would hand it to whichever branch read it first."""
    if branch_switches(common, branch):
        return None
    for name in earlier_names(common, branch, deadline):
        source = branch_switches(common, name) if name != branch else None
        if source and not branch_exists(common, name, deadline):
            return source
    return None


def earlier_names(common, branch, deadline):
    """Earlier names of this branch, newest first: git carries a branch's reflog across renames. Read from the file, as
    this runs on the guard's path, and through git only where refs live in a reftable, which has no such files."""
    if os.path.isdir(os.path.join(common, "reftable")):
        shown = subprocess.run(["git", "--git-dir", common, "reflog", "show", "--format=%gs", f"refs/heads/{branch}",
                                "--"], capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"},
                               timeout=max(deadline - time.monotonic(), 0.1))
        if shown.returncode != 0 and "bad revision" not in shown.stderr:  # bad revision: no commit yet
            raise OSError(f"git reflog failed for branch {branch}: {shown.stderr.strip()[-200:]}")
        messages = shown.stdout.splitlines()
    else:
        try:
            with open(os.path.join(common, "logs", "refs", "heads", branch), errors="replace") as fh:
                messages = [line.partition("\t")[2] for line in fh][::-1]
        except FileNotFoundError:  # no commit yet, or core.logAllRefUpdates off: git kept no record of a rename
            return []
        except OSError as error:
            raise OSError(f"Couldn't read the reflog of branch {branch} to follow a rename: {error}") from error
    return [match.group(1) for match in map(RENAMED.match, messages) if match]


def branch_exists(common, name, deadline):
    shown = subprocess.run(["git", "--git-dir", common, "show-ref", "--verify", "--quiet", f"refs/heads/{name}"],
                           capture_output=True, text=True, timeout=max(deadline - time.monotonic(), 0.1))
    if shown.returncode not in (0, 1):
        raise OSError(f"git show-ref failed for branch {name}: {shown.stderr.strip()[-200:]}")
    return shown.returncode == 0


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
    return phases_off_in(*followed_checkout(cwd, timeout))


def phases_off_in(common, branch):
    state = {}
    for scope, path in store_files(common, branch):
        switches = read(path)
        # Within one file the current name wins over the former one; across files the narrowest scope still does.
        switches = {**{FORMER_NAMES[k]: v for k, v in switches.items() if k in FORMER_NAMES}, **switches}
        # A phase a newer kit added isn't an error: an older hook running beside it must not hold every gate.
        state.update({phase: (value, scope) for phase, value in switches.items() if phase in PHASES})
    return {phase: scope for phase, (value, scope) in state.items() if value == "off"}


def review_rounds(cwd, timeout=5):
    """(re-check rounds after the review's first pass, the scope that set them or "default"). Raises StoreError
    when a switch file can't be read."""
    return review_rounds_in(*followed_checkout(cwd, timeout))


def review_rounds_in(common, branch):
    rounds = DEFAULT_ROUNDS, "default"
    for scope, path in store_files(common, branch):
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
        then = ("Confirm it to the user in one line; the review runs that many re-checks." if switched and not hint else
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
    and when it wasn't, or a review-rounds value was but doesn't apply yet, what the user can do). The action is off,
    on or review-rounds."""
    if not 1 <= len(words) <= 2:
        return False, f"Not {form}: nothing was switched.", \
            f"the form is {form}, branch when left out." if action == ROUNDS else f"the phases are {', '.join(PHASES)}."
    phase, scope = words[0], words[1] if len(words) == 2 else "branch"
    phase = FORMER_NAMES.get(phase, phase)
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
    try:
        if branch:  # before the write reads its file: a renamed branch keeps what its earlier name had
            follow_rename(common, branch, time.monotonic() + 5)
    except (OSError, subprocess.SubprocessError) as error:
        return False, f"Couldn't switch {what}: {error}. Nothing was switched.", "try again once that is fixed."
    path = dict(store_files(common, branch))[scope]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Two sessions switching at once: the lock keeps one from writing back what it read before the other wrote.
        with open(os.path.join(STORE, ".lock"), "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            switches = read(path)
            switches[key] = value
            for former in (f for f, current in FORMER_NAMES.items() if current == key):
                switches.pop(former, None)
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
        off = phases_off_in(common, branch)
        rounds, set_in = review_rounds_in(common, branch)
    except StoreError as error:
        problem = str(error)[0].lower() + str(error)[1:]
        if action == ROUNDS:  # the review reads an unreadable store as the default, not as the value just saved
            return True, (f"Saved {what} for {where}, but {problem}, so the review runs {DEFAULT_ROUNDS} re-check "
                          "round (the default) here until that file is fixed or deleted."), \
                "fix or delete the file the error names, and the saved value applies."
        return True, (f"Switched {what} for {where}, but {problem}, so every phase counts as on here until that file "
                      "is fixed or deleted."), None
    if action == ROUNDS:
        still = " A narrower setting overrides it here." if set_in != scope else ""
        return True, f"Switched {what} for {where}.{still} Review re-check rounds here now: {rounds} ({set_in}).", None
    still = (" A narrower switch keeps it on here." if action == "off" and phase not in off else
             " A narrower switch keeps it off here." if action == "on" and phase in off else "")
    return True, f"Switched {phase} {action} for {where}.{still} Phases off here now: {describe(off)}.", None


REFUSED = 3  # not 1: Python exits 1 on a crash, which may come after the write


def branch_switches(common, branch):
    """The file holding a branch's own switches, or None when it has none. Raises OSError when it can't tell."""
    path = dict(store_files(common, branch))["branch"]
    try:
        os.stat(path)  # not os.path.exists, which says no when the store can't be searched
    except FileNotFoundError:
        return None
    return path


def forget_branch(common, branch):
    """Remove a deleted branch's switches, and bin/phase's cached label for it with its refresh lock, so a new branch of
    that name starts with every phase on and gets its own label at once. True when it had switches. Raises OSError."""
    cache = review_stamp.phase_cache(common, branch)
    for path in (cache, cache[:-len(".json")] + ".lock"):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    try:
        with open(os.path.join(STORE, ".lock"), "w") as lock:  # not under a switch() writing it back
            fcntl.flock(lock, fcntl.LOCK_EX)
            os.remove(dict(store_files(common, branch))["branch"])
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
