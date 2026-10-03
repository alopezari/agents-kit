#!/usr/bin/env python3
"""bin/quality-log: well-formed entries are written, malformed ones refused without touching the log."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
KIT_FILES = ("bin/quality-log", "bin/repo-name", "review-mining/taxonomy.md", "hooks/review_stamp.py")


def setup(base):
    """A HOME of its own, so the log written is the test's, not the real one."""
    home = os.path.join(base, "home")
    os.makedirs(os.path.join(home, ".agents", "bin"))
    os.makedirs(os.path.join(home, ".agents", "review-mining"))
    for rel in KIT_FILES:
        os.makedirs(os.path.dirname(os.path.join(home, ".agents", rel)), exist_ok=True)
        shutil.copy(os.path.join(KIT, rel), os.path.join(home, ".agents", rel))
    return home


def log(home, *args):
    env = {**os.environ, "HOME": home}
    return subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), *args], cwd=home, capture_output=True,
                          text=True, env=env)


def entries(home):
    path = os.path.join(home, ".agents", "logs", "quality.jsonl")
    return [json.loads(line) for line in open(path)] if os.path.exists(path) else []


def writes_well_formed_entries(base):
    home = setup(base)
    assert log(home, "lens", "Correctness", "--findings", "10", "--confirmed", "6", "--secs", "600", "--model", "codex").returncode == 0
    assert log(home, "test", "e2e", "--issues", "1", "--secs", "30", "--notes", "found the 0.005 rounding").returncode == 0
    assert log(home, "escape", "bot", "--verdict", "confirmed", "--category", "P3.2a", "--lens", "Tests",
               "--pr", "42").returncode == 0
    lens, test, escape = entries(home)
    assert (lens["name"], lens["findings"], lens["confirmed"], lens["secs"], lens["model"]) == ("correctness", 10, 6, 600, "codex"), lens
    assert (test["issues"], test["notes"]) == (1, "found the 0.005 rounding"), test
    assert (escape["name"], escape["verdict"], escape["lens"], escape["pr"]) == ("bot", "confirmed", "tests", 42), escape


def refuses_malformed_entries(base):
    home = setup(base)
    for args, why in [
        (["lens", "correctness 10 6 claude", "--findings", "--confirmed", "--secs", "600"], "values pasted into the name"),
        (["lens", "correctness", "--findings", "--confirmed", "6"], "a flag taken as a value"),
        (["lens", "correctness", "--findings", "ten", "--confirmed", "6"], "a count that isn't a number"),
        (["lens", "correctness", "--confirmed", "6"], "a required count missing"),
        (["lens", "correctness", "--findings", "1", "--confirmed", "1", "--model", "gpt"], "an unknown model"),
        (["lens", "correctness", "--findings", "1", "--confirmed", "1", "--sec", "5"], "an unknown flag"),
        (["escape", "bot", "--verdict", "maybe", "--category", "C.bug", "--lens", "correctness", "--pr", "1"], "an unknown verdict"),
        (["escape", "bot", "--verdict", "confirmed", "--category", "", "--lens", "correctness", "--pr", "1"], "an empty category"),
        (["escape", "ci", "--verdict", "confirmed", "--category", "tests", "--lens", "tests", "--pr", "1"], "not a taxonomy code"),
        (["test", "e2e positive/negative (validate)", "--issues", "0"], "triage's label, not a test type"),
        (["test", "e2e"], "no issue count"),
    ]:
        out = log(home, *args)
        assert out.returncode == 2 and out.stderr, (why, out)
    assert entries(home) == [], "nothing was written"


def a_session_on_a_worktree_logs_to_the_main_checkout(base):
    """A session whose ~/.agents links to a kit worktree kept its rows there, and lost them when the worktree went."""
    kit, worktree, home = (os.path.join(base, name) for name in ("kit", "worktree", "home"))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", kit], check=True)
    subprocess.run([*git, "-C", kit, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([*git, "-C", kit, "worktree", "add", "-q", "-b", "feature", worktree], check=True)
    for rel in KIT_FILES:
        os.makedirs(os.path.dirname(os.path.join(worktree, rel)), exist_ok=True)
        shutil.copy(os.path.join(KIT, rel), os.path.join(worktree, rel))
    os.makedirs(home)
    os.symlink(worktree, os.path.join(home, ".agents"))
    session = {key: value for key, value in os.environ.items() if key != "AGENTS_KIT_UNDER_TEST"}
    rename = ["python3", "-c", f"import sys; sys.path.insert(0, {os.path.join(worktree, 'hooks')!r}); import review_stamp; "
              "review_stamp.log_rename('repo', 'session-x', 'feature/x')"]

    def write(env):
        env = {**env, "HOME": home}
        assert subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), "test", "e2e", "--issues", "0"], cwd=home,
                              env=env, capture_output=True).returncode == 0
        assert subprocess.run(rename, env=env, capture_output=True).returncode == 0

    def rows(checkout):
        path = os.path.join(checkout, "logs", "quality.jsonl")
        return [json.loads(line)["kind"] for line in open(path)] if os.path.exists(path) else []

    # A GIT_DIR inherited from the caller names another repository; -C alone doesn't override it.
    other = os.path.join(base, "other")
    subprocess.run([*git, "init", "-q", other], check=True)
    write({**session, "GIT_DIR": os.path.join(other, ".git")})
    assert (rows(kit), rows(worktree), rows(other)) == (["test", "rename"], [], []), (rows(kit), rows(worktree), rows(other))
    # Under the suite (AGENTS_KIT_UNDER_TEST), rows stay in the HOME it made, never in the real log.
    write(os.environ)
    assert (rows(kit), rows(worktree)) == (["test", "rename"], ["test", "rename"]), (rows(kit), rows(worktree))


def other_kit_layouts_keep_their_rows_in_the_kit(base):
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    session = {key: value for key, value in os.environ.items() if key != "AGENTS_KIT_UNDER_TEST"}

    def write(home, kit, **env):
        for rel in KIT_FILES:
            os.makedirs(os.path.dirname(os.path.join(kit, rel)), exist_ok=True)
            shutil.copy(os.path.join(KIT, rel), os.path.join(kit, rel))
        return subprocess.run([os.path.join(kit, "bin", "quality-log"), "test", "e2e", "--issues", "0"], cwd=home,
                              env={**session, "HOME": home, **env}, capture_output=True, text=True)

    # A plain ~/.agents inside a HOME that is itself a git repository (dotfiles).
    dotfiles = os.path.join(base, "dotfiles")
    subprocess.run([*git, "init", "-q", dotfiles], check=True)
    assert write(dotfiles, os.path.join(dotfiles, ".agents")).returncode == 0
    assert os.path.exists(os.path.join(dotfiles, ".agents", "logs", "quality.jsonl")), os.listdir(dotfiles)
    # A kit whose git dir lives elsewhere (--separate-git-dir, as a submodule's does).
    home = os.path.join(base, "home")
    kit = os.path.join(home, ".agents")
    subprocess.run([*git, "init", "-q", "--separate-git-dir", os.path.join(base, "kit.git"), kit], check=True)
    subprocess.run([*git, "-C", kit, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    assert write(home, kit).returncode == 0
    assert os.path.exists(os.path.join(kit, "logs", "quality.jsonl")), os.listdir(base)
    # A worktree of it: git names the git dir as the main worktree, which isn't a checkout to write into.
    worktree = os.path.join(base, "worktree")
    subprocess.run([*git, "-C", kit, "worktree", "add", "-q", "-b", "feature", worktree], check=True)
    linked_home = os.path.join(base, "linked-home")
    os.makedirs(linked_home)
    os.symlink(worktree, os.path.join(linked_home, ".agents"))
    out = write(linked_home, worktree)
    # It says so instead of quietly keeping rows where a removal would lose them.
    assert out.returncode == 0 and "couldn't find the kit's main checkout" in out.stderr, out
    assert os.path.exists(os.path.join(worktree, "logs", "quality.jsonl")), os.listdir(worktree)


for test in (writes_well_formed_entries, refuses_malformed_entries, a_session_on_a_worktree_logs_to_the_main_checkout,
             other_kit_layouts_keep_their_rows_in_the_kit):
    base = tempfile.mkdtemp(prefix="agents-test-quality-log-")
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
