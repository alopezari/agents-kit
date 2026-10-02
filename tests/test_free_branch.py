#!/usr/bin/env python3
"""bin/free-branch: once the PR is open, the branch's worktree is removed (or detached, when the session runs in it)
so the user can switch to the branch in the main checkout."""
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
FREE = os.path.join(KIT, "bin", "free-branch")
RESULTS = []


def sh(cwd, *cmd):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


def setup(base, branch="feat/cart"):
    """A main checkout on trunk with an origin, and `branch` pushed from a worktree of its own."""
    origin, main, worktree = (os.path.join(base, name) for name in ("origin.git", "shop", "shop-worktree-cart"))
    sh(base, "git", "init", "-q", "--bare", origin)
    sh(base, "git", "clone", "-q", origin, main)
    sh(main, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
    sh(main, "git", "push", "-q", "origin", "HEAD")
    sh(main, "git", "worktree", "add", "-q", "-b", branch, worktree)
    sh(worktree, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "change")
    sh(worktree, "git", "push", "-q", "-u", "origin", branch)
    return main, worktree


def checked_out(main, branch):
    return f"branch refs/heads/{branch}" in sh(main, "git", "worktree", "list", "--porcelain").stdout


def removes_a_worktree_the_agent_made(base):
    main, worktree = setup(base)
    out = sh(base, FREE, worktree)  # run from elsewhere, as the session's own directory
    assert out.returncode == 0 and not os.path.exists(worktree) and not checked_out(main, "feat/cart"), out
    assert f"cd {main} && git switch feat/cart" in out.stdout, out.stdout


def detaches_the_session_worktree(base):
    main, worktree = setup(base)
    out = sh(worktree, FREE, worktree)
    assert out.returncode == 0 and os.path.isdir(worktree) and not checked_out(main, "feat/cart"), \
        f"the session runs there: removing it would break its hooks: {out}"
    assert sh(worktree, "git", "rev-parse", "--abbrev-ref", "HEAD").stdout.strip() == "HEAD", "detached"


def refuses_to_lose_work(base):
    main, worktree = setup(base)
    open(os.path.join(worktree, "notes.txt"), "w").write("x\n")
    untracked = sh(base, FREE, worktree)
    assert untracked.returncode == 1 and "notes.txt" in untracked.stderr and checked_out(main, "feat/cart"), untracked
    os.remove(os.path.join(worktree, "notes.txt"))
    open(os.path.join(worktree, ".gitignore"), "w").write(".env\nnode_modules/\n")
    sh(worktree, "git", "add", ".gitignore")
    modified = sh(base, FREE, worktree)
    assert modified.returncode == 1 and ".gitignore" in modified.stderr and checked_out(main, "feat/cart"), modified
    sh(worktree, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "ignore")
    sh(worktree, "git", "push", "-q")
    os.makedirs(os.path.join(worktree, "node_modules", "pkg"))
    open(os.path.join(worktree, "node_modules", "pkg", "index.js"), "w").write("x\n")
    open(os.path.join(worktree, ".env"), "w").write("TOKEN=only-here\n")
    secret = sh(base, FREE, worktree)
    assert secret.returncode == 1 and ".env" in secret.stderr and "node_modules" not in secret.stderr \
        and os.path.exists(os.path.join(worktree, ".env")), f"an ignored file that exists nowhere else: {secret}"
    detached = sh(worktree, FREE, worktree)
    assert detached.returncode == 0 and os.path.exists(os.path.join(worktree, ".env")), \
        f"detaching deletes nothing, so ignored files don't stop it: {detached}"
    sh(worktree, "git", "switch", "-q", "feat/cart")
    os.remove(os.path.join(worktree, ".env"))
    sh(worktree, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "unpushed")
    ahead = sh(base, FREE, worktree)
    assert ahead.returncode == 1 and "push them" in ahead.stderr and checked_out(main, "feat/cart"), ahead
    sh(worktree, "git", "switch", "-q", "-c", "feat/local")
    local = sh(base, FREE, worktree)
    assert local.returncode == 1 and "no upstream" in local.stderr and checked_out(main, "feat/local"), local


def leaves_the_main_checkout_alone(base):
    main, worktree = setup(base)
    out = sh(base, FREE, main)
    assert out.returncode == 1 and os.path.isdir(main) and "main checkout" in out.stdout + out.stderr, out


for test in (removes_a_worktree_the_agent_made, detaches_the_session_worktree, refuses_to_lose_work,
             leaves_the_main_checkout_alone):
    base = os.path.realpath(tempfile.mkdtemp(prefix="agents-test-free-branch-"))
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
