#!/usr/bin/env python3
"""bin/prune-merged: removes the worktrees and local branches whose work is already in origin's default branch, when
nothing would be lost, and says why it keeps each of the others."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
PRUNE = os.path.join(KIT, "bin", "prune-merged")
RESULTS = []


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def commit(cwd, message):
    sh(cwd, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", message)


def setup(base):
    """A main checkout on trunk, with an origin and an ignore file for dependencies, bytecode and .env."""
    origin, main = os.path.join(base, "origin.git"), os.path.join(base, "shop")
    sh(base, "git", "init", "-q", "--bare", "-b", "trunk", origin)
    sh(base, "git", "clone", "-q", origin, main)
    sh(main, "git", "switch", "-q", "-c", "trunk")
    open(os.path.join(main, ".gitignore"), "w").write("node_modules/\n__pycache__/\n.env\n")
    sh(main, "git", "add", ".gitignore")
    commit(main, "init")
    sh(main, "git", "push", "-q", "-u", "origin", "trunk")
    sh(main, "git", "remote", "set-head", "origin", "trunk")
    return main


def worktree(base, main, name, merged=True):
    """A worktree on branch `name` with one commit, merged into origin/trunk unless merged=False."""
    path = os.path.join(base, f"shop-{name}")
    sh(main, "git", "worktree", "add", "-q", "-b", name, path, "trunk")
    commit(path, f"work on {name}")
    if merged:
        sh(main, "git", "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", f"merge {name}", name)
        sh(main, "git", "push", "-q", "origin", "trunk")
    return path


def branches(main):
    return set(sh(main, "git", "branch", "--format=%(refname:short)").stdout.split())


def removes_merged_and_keeps_the_rest_with_reasons(base):
    main = setup(base)
    done = worktree(base, main, "done")
    os.makedirs(os.path.join(done, "node_modules", "x"))
    os.makedirs(os.path.join(done, "__pycache__"))
    os.makedirs(os.path.join(done, ".ruff_cache", "0.16.9"))  # ruff ignores its cache with its own .gitignore
    open(os.path.join(done, ".ruff_cache", ".gitignore"), "w").write("*\n")
    open(os.path.join(done, ".ruff_cache", "0.16.9", "x"), "w").write("cache\n")
    dirty = worktree(base, main, "dirty")
    open(os.path.join(dirty, "notes.txt"), "w").write("draft\n")
    secret = worktree(base, main, "secret")
    open(os.path.join(secret, ".env"), "w").write("TOKEN=1\n")
    wip = worktree(base, main, "wip", merged=False)
    locked = worktree(base, main, "locked")
    sh(main, "git", "worktree", "lock", locked)
    busy = worktree(base, main, "busy")
    sleeper = subprocess.Popen(["sleep", "30"], cwd=busy)
    sh(main, "git", "branch", "old", "trunk~1")  # merged long ago, checked out nowhere
    sh(main, "git", "switch", "-q", "-c", "unmerged-branch")
    commit(main, "not merged")
    sh(main, "git", "switch", "-q", "trunk")
    try:
        before = branches(main)
        dry = sh(main, PRUNE, "--dry-run")
        assert dry.returncode == 0 and os.path.isdir(done) and branches(main) == before, dry
        assert f"would remove {done}" in dry.stdout and "would delete branch done" in dry.stdout, dry.stdout
        out = sh(main, PRUNE)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert out.returncode == 0, out
    assert not os.path.exists(done) and "done" not in branches(main), out.stdout
    assert "old" not in branches(main), "a merged branch checked out nowhere goes"
    for path, reason in ((dirty, "changes"), (secret, ".env"), (wip, "not in origin/trunk"), (locked, "locked"),
                         (busy, "in use")):
        assert os.path.isdir(path), f"{path} must stay: {out.stdout}"
        line = next((line for line in out.stdout.splitlines() if path in line), "")
        assert line.startswith("kept") and reason in line, (reason, out.stdout)
    assert {"trunk", "unmerged-branch", "wip", "dirty", "secret", "locked", "busy"} <= branches(main), branches(main)


def keeps_the_worktree_it_runs_in(base):
    main = setup(base)
    here = worktree(base, main, "here")
    out = sh(here, PRUNE)
    assert out.returncode == 0 and os.path.isdir(here) and "runs there" in out.stdout, out


def a_failed_fetch_removes_nothing(base):
    main = setup(base)
    done = worktree(base, main, "done")
    sh(main, "git", "remote", "set-url", "origin", os.path.join(base, "gone.git"))
    out = sh(main, PRUNE)
    assert out.returncode == 1 and os.path.isdir(done) and "fetch" in out.stderr, out


def keeps_a_worktree_it_cannot_inspect(base):
    main = setup(base)
    gone = worktree(base, main, "gone")
    shutil.rmtree(gone)  # its directory deleted by hand: git still lists it
    out = sh(main, PRUNE)
    assert out.returncode == 0 and f"kept {gone}: couldn't inspect" in out.stdout, out


for test in (removes_merged_and_keeps_the_rest_with_reasons, keeps_the_worktree_it_runs_in,
             a_failed_fetch_removes_nothing, keeps_a_worktree_it_cannot_inspect):
    base = os.path.realpath(tempfile.mkdtemp(prefix="agents-test-prune-"))
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
