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
    hidden = worktree(base, main, "hidden")
    open(os.path.join(hidden, ".gitignore"), "a").write("")  # tracked, edited after git was told to stop looking
    sh(hidden, "git", "update-index", "--assume-unchanged", ".gitignore")
    open(os.path.join(hidden, ".gitignore"), "a").write("local edit\n")
    sh(main, "git", "symbolic-ref", "refs/heads/alias", "refs/heads/trunk")  # deleting it must not delete trunk
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
                         (busy, "in use"), (hidden, "assume-unchanged")):
        assert os.path.isdir(path), f"{path} must stay: {out.stdout}"
        line = next((line for line in out.stdout.splitlines() if path in line), "")
        assert line.startswith("kept") and reason in line, (reason, out.stdout)
    assert {"trunk", "alias", "unmerged-branch", "wip", "dirty", "secret", "locked", "busy", "hidden"} <= branches(main), \
        branches(main)


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


def forgets_the_phase_switches_of_the_branches_it_deletes(base):
    main = setup(base)
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}  # the store lives in HOME: never the user's own
    done, open_ = worktree(base, main, "done"), worktree(base, main, "open", merged=False)
    for path in (done, open_):
        switched = subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"],
                                  cwd=path, capture_output=True, text=True, env=env)
        assert switched.returncode == 0, switched.stdout + switched.stderr
    store = os.path.join(home, ".agents", "approvals", "phases", "branch", "shop")
    assert sorted(os.listdir(store)) == ["done.json", "open.json"], os.listdir(store)
    dry = subprocess.run([PRUNE, "--dry-run"], cwd=main, capture_output=True, text=True, env=env)
    assert "would delete branch done and its phase switches" in dry.stdout, dry.stdout
    assert sorted(os.listdir(store)) == ["done.json", "open.json"], "--dry-run changes nothing"
    label = os.path.join(main, ".git", "agents", "phase", "done.json")  # bin/phase's cached label, 15 s fresh
    os.makedirs(os.path.dirname(label), exist_ok=True)
    open(label, "w").write('{"label": "audit off"}')
    out = subprocess.run([PRUNE], cwd=main, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "deleted branch done and its phase switches" in out.stdout, out
    assert os.listdir(store) == ["open.json"], f"a branch kept keeps its switches: {os.listdir(store)}"
    assert not os.path.exists(label), "a new branch named done must not show the old branch's label"


def says_when_it_cannot_check_the_switches(base):
    main = setup(base)
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}
    done = worktree(base, main, "done")
    subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=done,
                   capture_output=True, text=True, env=env, check=True)
    sh(main, "git", "worktree", "remove", done)
    store = os.path.join(home, ".agents", "approvals", "phases", "branch", "shop")
    os.chmod(store, 0o600)  # can't be searched: whether done.json is there is unknown, not no
    try:
        dry = subprocess.run([PRUNE, "--dry-run"], cwd=main, capture_output=True, text=True, env=env)
    finally:
        os.chmod(store, 0o700)
    assert "would delete branch done, but couldn't check its phase switches:" in dry.stdout, dry


def keeps_the_switches_when_it_cannot_name_the_repo(base):
    main = setup(base)
    worktree(base, main, "done")
    sh(main, "git", "worktree", "remove", os.path.join(base, "shop-done"))
    # git answers prune-merged but not phase_switches.checkout: its failure is the one under test
    run = ("import importlib.machinery, sys; sys.argv = [sys.argv[1]]; "
           "loader = importlib.machinery.SourceFileLoader('prune', sys.argv[0]); prune = loader.load_module(); "
           "prune.phase_switches.checkout = lambda cwd: (_ for _ in ()).throw(OSError('git rev-parse failed')); "
           "sys.exit(prune.main())")
    out = subprocess.run(["python3", "-c", run, PRUNE], cwd=main, capture_output=True, text=True)
    assert out.returncode == 1, out
    assert "deleted branch done, but kept its phase switches: couldn't name the repo" in out.stdout, out.stdout
    assert "done" not in branches(main)


def deletes_the_branch_even_when_its_switches_stay(base):
    main = setup(base)
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}
    done = worktree(base, main, "done")
    subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=done,
                   capture_output=True, text=True, env=env, check=True)
    store = os.path.join(home, ".agents", "approvals", "phases", "branch", "shop")
    os.chmod(store, 0o500)  # the switch file can't be removed
    try:
        out = subprocess.run([PRUNE], cwd=main, capture_output=True, text=True, env=env)
    finally:
        os.chmod(store, 0o700)
    assert out.returncode == 1 and "deleted branch done, but kept its phase switches:" in out.stdout, out
    assert "done" not in branches(main) and os.listdir(store) == ["done.json"], (branches(main), os.listdir(store))


for test in (removes_merged_and_keeps_the_rest_with_reasons, keeps_the_worktree_it_runs_in,
             a_failed_fetch_removes_nothing, keeps_a_worktree_it_cannot_inspect,
             forgets_the_phase_switches_of_the_branches_it_deletes, deletes_the_branch_even_when_its_switches_stay,
             says_when_it_cannot_check_the_switches, keeps_the_switches_when_it_cannot_name_the_repo):
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
