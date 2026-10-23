#!/usr/bin/env python3
"""bin/prune-merged: removes the worktrees and local branches whose work is already in origin's default branch, when
nothing would be lost, and says why it keeps each of the others."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import os
import re
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
PRUNE = os.path.join(KIT, "bin", "prune-merged")
RESULTS = []
# prune-merged deletes phase switches in HOME's store: never the user's own, whatever the test sets.
os.environ["HOME"] = os.path.realpath(tempfile.mkdtemp(prefix="agents-test-prune-home-"))
KINDS = ("spec", "verify", "review", "validation", "checks-guide", "staging-guide", "follow-pr", "pr-body")


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


def branch_store(home):
    """The store's folder of branch switches for the one repo whose phases the test switched."""
    root = os.path.join(home, ".agents", "approvals", "phases", "branch")
    (repo,) = os.listdir(root)
    return os.path.join(root, repo)


def kit_files(main, key, guide=None):
    """Every file the kit keeps for a branch key, in .git/agents and in $TMPDIR/agents-specs, its evidence and stamps
    holding a file each; the checks guide holds `guide` when given."""
    paths = []
    for where in (os.path.join(main, ".git", "agents"), os.path.join(os.environ["TMPDIR"], "agents-specs")):
        os.makedirs(where, exist_ok=True)
        for kind in KINDS:
            paths.append(os.path.join(where, f"{kind}-shop-{key}.md"))
            open(paths[-1], "w").write(guide if guide is not None and kind == "checks-guide" else f"# {kind}\n")
        paths.append(os.path.join(where, f"browser-ab-spec-shop-{key}.json"))
        open(paths[-1], "w").write("{}\n")
        paths.append(os.path.join(where, f"evidence-shop-{key}"))
        os.makedirs(paths[-1])
        open(os.path.join(paths[-1], "E1.txt"), "w").write("ran\n")
    paths.append(os.path.join(main, ".git", "agents", "stamps", key))
    os.makedirs(paths[-1])
    open(os.path.join(paths[-1], "self-review.stamp"), "w").write("abc\n")
    return paths


def line_of(out, branch):
    """The line prune-merged printed about `branch`, or ""."""
    said = re.compile(rf"(deleted|would delete|kept) branch {re.escape(branch)}(?=[ ,:]|$)")
    return next((line for line in out.stdout.splitlines() if said.match(line)), "")


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
    store = branch_store(home)
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


def forgets_only_the_switches_of_its_own_repo(base):
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}
    checkouts = []
    for owner in ("a", "b"):  # two repos whose main checkouts are both named shop
        os.makedirs(os.path.join(base, owner))
        main = setup(os.path.join(base, owner))
        fix = worktree(os.path.join(base, owner), main, "fix")
        subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=fix,
                       capture_output=True, text=True, env=env, check=True)
        checkouts.append((main, fix))
    (a, a_fix), (b, b_fix) = checkouts
    sh(a, "git", "worktree", "remove", a_fix)
    out = subprocess.run([PRUNE], cwd=a, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "deleted branch fix and its phase switches" in out.stdout, out
    read = ("import sys; sys.path.insert(0, sys.argv[1]); import phase_switches; "
            "print(phase_switches.phases_off(sys.argv[2]))")
    left = subprocess.run(["python3", "-c", read, os.path.join(KIT, "hooks"), b_fix], capture_output=True, text=True,
                          env=env)
    assert left.stdout.strip() == "{'audit': 'branch'}", f"the other shop keeps its fix's switches: {left}"


def forgets_the_switches_of_a_renamed_branch_never_read(base):
    main = setup(base)
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}
    done = worktree(base, main, "old")
    subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=done,
                   capture_output=True, text=True, env=env, check=True)
    sh(done, "git", "branch", "-m", "old", "done")  # nothing reads done's switches before prune
    sh(main, "git", "worktree", "remove", done)
    dry = subprocess.run([PRUNE, "--dry-run"], cwd=main, capture_output=True, text=True, env=env)
    assert "would delete branch done and its phase switches" in dry.stdout, dry
    out = subprocess.run([PRUNE], cwd=main, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and "deleted branch done and its phase switches" in out.stdout, out
    assert os.listdir(branch_store(home)) == [], "old's file must not wait for a branch named old"

    gone = worktree(base, main, "gone")
    subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=gone,
                   capture_output=True, text=True, env=env, check=True)
    sh(gone, "git", "branch", "-m", "gone", "unread")
    sh(main, "git", "worktree", "remove", gone)
    label = os.path.join(main, ".git", "agents", "phase", "unread.json")
    os.makedirs(os.path.dirname(label), exist_ok=True)
    for path in (label, label[:-len(".json")] + ".lock"):
        open(path, "w").close()
    reflog = os.path.join(main, ".git", "logs", "refs", "heads", "unread")
    os.chmod(reflog, 0)
    try:
        out = subprocess.run([PRUNE], cwd=main, capture_output=True, text=True, env=env)
    finally:
        if os.path.exists(reflog):
            os.chmod(reflog, 0o644)
    assert out.returncode == 1 and "deleted branch unread, but kept the phase switches of its earlier name: " \
        "couldn't follow its rename" in out.stdout, out
    assert os.listdir(os.path.dirname(label)) == [], "the deleted branch's label and lock go even so"


def says_when_it_cannot_check_the_switches(base):
    main = setup(base)
    home = os.path.join(base, "home")
    os.makedirs(home)
    env = {**os.environ, "HOME": home}
    done = worktree(base, main, "done")
    subprocess.run(["python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "audit"], cwd=done,
                   capture_output=True, text=True, env=env, check=True)
    sh(main, "git", "worktree", "remove", done)
    store = branch_store(home)
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
           "import importlib.util; loader = importlib.machinery.SourceFileLoader('prune', sys.argv[0]); "
           "prune = importlib.util.module_from_spec(importlib.util.spec_from_loader('prune', loader)); "
           "loader.exec_module(prune); "
           "prune.phase_switches.checkout = lambda cwd: (_ for _ in ()).throw(OSError('git rev-parse failed')); "
           "sys.exit(prune.main())")
    out = subprocess.run(["python3", "-B", "-c", run, PRUNE], cwd=main, capture_output=True, text=True)
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
    store = branch_store(home)
    os.chmod(store, 0o500)  # the switch file can't be removed
    try:
        out = subprocess.run([PRUNE], cwd=main, capture_output=True, text=True, env=env)
    finally:
        os.chmod(store, 0o700)
    assert out.returncode == 1 and "deleted branch done, but kept its phase switches:" in out.stdout, out
    assert "done" not in branches(main) and os.listdir(store) == ["done.json"], (branches(main), os.listdir(store))


def deletes_the_files_kept_for_the_branches_it_deletes(base):
    main = setup(base)
    done = worktree(base, main, "done")
    sh(main, "git", "worktree", "remove", done)
    sh(main, "git", "branch", "fix/a", "trunk")  # merged; its files under the old dash key fix-a belong to...
    live = worktree(base, main, "fix-a", merged=False)  # ...the branch named fix-a, which is live
    gone = kit_files(main, "done") + kit_files(main, "fix~a")
    kept = kit_files(main, "fix-a") + kit_files(main, "open")
    worktree(base, main, "open", merged=False)
    dry = sh(main, PRUNE, "--dry-run")
    assert dry.returncode == 0 and all(os.path.exists(path) for path in gone + kept), dry
    for path in gone:
        assert path in dry.stdout, f"--dry-run lists {path}: {dry.stdout}"
    out = sh(main, PRUNE)
    assert out.returncode == 0, out
    assert "done" not in branches(main) and "fix/a" not in branches(main), out.stdout
    left = [path for path in gone if os.path.exists(path)]
    assert not left, f"left behind: {left}\n{out.stdout}"
    assert all(path in out.stdout for path in gone), out.stdout
    assert all(os.path.exists(path) for path in kept), f"a live branch's files stay: {out.stdout}"
    assert os.path.isdir(live)


def deletes_the_files_of_a_renamed_branch_but_not_of_a_copy_source(base):
    main = setup(base)
    renamed = worktree(base, main, "feat/old")
    sh(renamed, "git", "branch", "-m", "feat/old", "done")
    sh(main, "git", "worktree", "remove", renamed)
    gone = kit_files(main, "feat~old") + kit_files(main, "feat-old")
    source = worktree(base, main, "orig", merged=False)
    sh(source, "git", "branch", "-m", "orig", "src")
    sh(source, "git", "branch", "-c", "src", "copy")  # copies src's reflog, orig's rename included
    commit(source, "more on src")  # src stays live: its worktree has work not in origin
    sh(main, "git", "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff", "-m", "merge copy", "copy")
    sh(main, "git", "push", "-q", "origin", "trunk")
    kept = kit_files(main, "orig")
    out = sh(main, PRUNE)
    assert out.returncode == 0 and {"done", "copy"}.isdisjoint(branches(main)), out
    assert not [path for path in gone if os.path.exists(path)], f"the earlier names' files go: {out.stdout}"
    assert all(os.path.exists(path) for path in kept), f"a copy's source keeps its earlier name's files: {out.stdout}"


def keeps_a_branch_whose_steps_after_the_merge_have_not_passed(base):
    main = setup(base)
    table = "\n## Results (2026-10-0{})\n\n| Step | Result | Evidence |\n|---|---|---|\n{}"
    head = "# Checks guide\n\n## After the merge\n\n### P1. Load the job\n\n### P2. One full run\n"
    guides = {
        "no-result": (head + table.format(1, "| P1 | PASS | p1.txt |\n"), "P2"),
        "rerun": (head + table.format(1, "| P1 | PASS | p1.txt |\n| P2 | PASS | p2.txt |\n")
                  + table.format(2, "| P2 | NOT RUN | again after the fix |\n"), "P2"),
        "failed": (head + table.format(1, "| P1 | FAIL | p1.txt |\n| P2 | PASS | p2.txt |\n"), "P1"),
        "sub-step": (head + table.format(1, "| P1 | PASS | p1.txt |\n| P2a | PASS | p2a.txt |\n| P2b | NOT RUN | |\n"), "P2"),
        "passed": (head + "\n### M1. Pay on a phone\n" + table.format(1, "| P1 | PASS | p1.txt |\n| P2a | PASS | p2a.txt |\n"
                                                                          "| P2b | PASS | p2b.txt |\n"), None),
    }
    files = {}
    for name, (guide, _) in guides.items():
        sh(main, "git", "branch", name, "trunk")
        files[name] = kit_files(main, name, guide)
    sh(main, "git", "branch", "legacy", "trunk")  # a guide from before checks-guide, under its old name only
    files["legacy"] = kit_files(main, "legacy")
    for where in (os.path.join(main, ".git", "agents"), os.path.join(os.environ["TMPDIR"], "agents-specs")):
        os.remove(os.path.join(where, "checks-guide-shop-legacy.md"))
        files["legacy"].remove(os.path.join(where, "checks-guide-shop-legacy.md"))
        open(os.path.join(where, "staging-guide-shop-legacy.md"), "w").write(head)
    sh(main, "git", "branch", "unreadable", "trunk")
    files["unreadable"] = kit_files(main, "unreadable", head + table.format(1, "| P1 | PASS | p1.txt |\n| P2 | PASS | p2.txt |\n"))
    unreadable = os.path.join(main, ".git", "agents", "checks-guide-shop-unreadable.md")
    os.chmod(unreadable, 0)
    try:
        dry = sh(main, PRUNE, "--dry-run")
        out = sh(main, PRUNE)
    finally:
        os.chmod(unreadable, 0o644)
    for name, (_, pending) in guides.items():
        if pending:
            for run in (dry, out):
                line = line_of(run, name)
                assert line.startswith(f"kept branch {name}:") and "after the merge" in line and pending in line, \
                    (name, run.stdout)
            assert name in branches(main) and all(os.path.exists(path) for path in files[name]), (name, out.stdout)
    assert "P1, P2" in line_of(out, "legacy") and "legacy" in branches(main), out.stdout
    assert all(os.path.exists(path) for path in files["legacy"]), out.stdout
    line = line_of(out, "unreadable")
    assert line.startswith("kept branch unreadable: couldn't read its checks guide") and unreadable in line, out.stdout
    assert "unreadable" in branches(main) and all(os.path.exists(path) for path in files["unreadable"])
    assert "passed" not in branches(main) and not any(os.path.exists(path) for path in files["passed"]), out.stdout
    assert line_of(dry, "passed").startswith("would delete branch passed"), dry.stdout
    assert out.returncode == 0, out


def names_the_files_it_cannot_delete(base):
    main = setup(base)
    for name in ("stuck", "other"):
        sh(main, "git", "branch", name, "trunk")
    stuck, other = kit_files(main, "stuck"), kit_files(main, "other")
    evidence = os.path.join(main, ".git", "agents", "evidence-shop-stuck")
    os.chmod(evidence, 0o500)  # E1.txt can't be removed from it
    try:
        out = sh(main, PRUNE)
    finally:
        os.chmod(evidence, 0o700)
    assert out.returncode == 1, out
    line = line_of(out, "stuck")
    assert line.startswith("deleted branch stuck") and f"couldn't delete {evidence}" in line, out.stdout
    assert os.path.exists(os.path.join(evidence, "E1.txt")), "the file it couldn't delete is still there"
    assert not [path for path in stuck if path != evidence and os.path.exists(path)], out.stdout
    assert not any(os.path.exists(path) for path in other) and "other" not in branches(main), \
        f"the other branches still go: {out.stdout}"


for test in (removes_merged_and_keeps_the_rest_with_reasons, keeps_the_worktree_it_runs_in,
             a_failed_fetch_removes_nothing, keeps_a_worktree_it_cannot_inspect,
             forgets_the_phase_switches_of_the_branches_it_deletes, forgets_only_the_switches_of_its_own_repo, forgets_the_switches_of_a_renamed_branch_never_read, deletes_the_branch_even_when_its_switches_stay,
             says_when_it_cannot_check_the_switches, keeps_the_switches_when_it_cannot_name_the_repo,
             deletes_the_files_kept_for_the_branches_it_deletes, deletes_the_files_of_a_renamed_branch_but_not_of_a_copy_source,
             keeps_a_branch_whose_steps_after_the_merge_have_not_passed, names_the_files_it_cannot_delete):
    base = os.path.realpath(tempfile.mkdtemp(prefix="agents-test-prune-"))
    # the copies of reports a sandbox keeps in $TMPDIR/agents-specs: never the user's own, nor another test's.
    os.environ["TMPDIR"] = os.path.join(base, "tmp")
    os.makedirs(os.environ["TMPDIR"])
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
shutil.rmtree(os.environ["HOME"], ignore_errors=True)
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
