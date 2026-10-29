#!/usr/bin/env python3
"""bin/quality-log: well-formed entries are written, malformed ones refused without touching the log."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile
import time

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
KIT_FILES = ("bin/quality-log", "bin/repo-name", "bin/triage", "review-mining/taxonomy.md", "hooks/review_stamp.py", "hooks/hooklog.py")


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
    assert "shared" not in lens, "no --shared means the overlap is unknown, not zero"


def records_the_findings_the_other_model_shared(base):
    home = setup(base)
    assert log(home, "lens", "correctness", "--findings", "5", "--confirmed", "3", "--model", "codex", "--shared", "2").returncode == 0
    assert log(home, "lens", "correctness", "--findings", "3", "--confirmed", "2", "--model", "claude", "--shared", "0").returncode == 0
    assert log(home, "lens", "tests", "--findings", "2", "--confirmed", "1", "--model", "codex", "--shared", "1").returncode == 0, "all shared"
    codex, claude, all_shared = entries(home)
    assert (codex["model"], codex["shared"], claude["shared"], all_shared["shared"]) == ("codex", 2, 0, 1), entries(home)


def keeps_lens_names_to_the_lenses_there_are(base):
    home = setup(base)
    for name, key in [("Correctness", "correctness"), ("Performance and scale", "performance"),
                      ("UX, accessibility and i18n", "ux-a11y-i18n"), ("ux-a11y-i18n", "ux-a11y-i18n"), ("spec", "spec"),
                      ("correctness-recheck", "correctness-recheck")]:
        assert log(home, "lens", name, "--findings", "1", "--confirmed", "1").returncode == 0, name
        assert log(home, "escape", "ci", "--verdict", "confirmed", "--category", "P3.2a", "--lens", name, "--pr", "1").returncode == 0, name
        lens, escape = entries(home)[-2:]
        assert (lens["name"], escape["lens"]) == (key, key), (name, lens, escape)
    assert not os.path.exists(os.path.join(home, ".agents", "bin", "__pycache__")), "loading bin/triage left a cache bin/docs trips on"


def refuses_malformed_entries(base):
    home = setup(base)
    says = {"more shared than confirmed": "more than --confirmed", "shared without the model": "--shared needs --model",
            "a lens that doesn't exist": "isn't a lens", "an escape's unknown lens": "isn't a lens"}
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
        (["lens", "correctness", "--findings", "3", "--confirmed", "2", "--model", "codex", "--shared", "3"], "more shared than confirmed"),
        (["lens", "correctness", "--findings", "3", "--confirmed", "2", "--shared", "1"], "shared without the model"),
        (["lens", "data", "--findings", "1", "--confirmed", "1"], "a lens that doesn't exist"),
        (["lens", "performance-scale", "--findings", "1", "--confirmed", "1"], "a name between key and heading"),
        (["escape", "bot", "--verdict", "confirmed", "--category", "P3.2a", "--lens", "data", "--pr", "1"], "an escape's unknown lens"),
    ]:
        out = log(home, *args)
        assert out.returncode == 2 and out.stderr, (why, out)
        assert says.get(why, "") in out.stderr, (why, out.stderr)
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


def records_the_spec_and_diff_tiers(base):
    """Each row keeps the spec's predicted tier and the diff's beside the effective one, so mismatches can be counted."""
    home, repo = os.path.join(base, "home"), os.path.join(base, "shop")
    os.makedirs(os.path.join(home, ".agents"))
    for part in ("bin", "hooks", "skills", "review-mining"):
        os.symlink(os.path.join(KIT, part), os.path.join(home, ".agents", part))
    env = {**os.environ, "HOME": home}
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q", "-b", "main", repo], check=True)
    open(os.path.join(repo, "README.md"), "w").write("# Shop\n")
    subprocess.run([*git, "-C", repo, "add", "-A"], check=True)
    subprocess.run([*git, "-C", repo, "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*git, "-C", repo, "checkout", "-q", "-b", "round"], check=True)
    open(os.path.join(repo, "README.md"), "w").write("# Shop\n\nRounds prices.\n")
    spec = subprocess.run([os.path.join(KIT, "skills", "kit-spec", "path.sh")], cwd=repo, env=env, capture_output=True,
                          text=True, check=True).stdout.strip()
    open(spec, "w").write("# Round\n\nGoal: x.\n\nRisk: high — adds a schema\n")

    def row():
        result = subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), "test", "e2e", "--issues", "0"], cwd=repo,
                                env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return entries(home)[-1]

    got = row()
    assert (got["tier"], got.get("spec_tier"), got.get("diff_tier")) == ("high", "high", "low"), got
    open(spec, "w").write("# Round\n\nGoal: x.\n\nRisk: low — one line\n")  # the prediction the diff proves wrong
    open(os.path.join(repo, "app.py"), "w").write("if user_has_capability('edit'):\n    x = 2\n")
    got = row()
    assert (got["tier"], got.get("spec_tier"), got.get("diff_tier")) == ("high", "low", "high"), got
    os.remove(os.path.join(home, ".agents", "bin"))  # the same kit, with a triage that fails
    shutil.copytree(os.path.join(KIT, "bin"), os.path.join(home, ".agents", "bin"))
    stub = os.path.join(home, ".agents", "bin", "triage")  # quality-log still loads it for the lens names
    source = open(stub).read()
    open(stub, "w").write(source.replace("\ndef main():\n", "\ndef main():\n    sys.exit('triage: broken')\n", 1))
    got = row()
    assert {"tier", "spec_tier", "diff_tier"} <= got.keys() and (got["tier"], got["spec_tier"], got["diff_tier"]) == (None, None, None), got


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


def records_each_findings_outcome(base):
    home = setup(base)
    assert log(home, "lens", "correctness", "--findings", "8", "--confirmed", "5", "--model", "claude", "--shared", "2",
               "--outcomes", "fixed=3,wrong=2,intentional=1,pre-existing=1,open=1").returncode == 0
    assert log(home, "lens", "tests", "--findings", "2", "--confirmed", "2").returncode == 0, "outcomes stay optional"
    with_outcomes, without = entries(home)
    assert with_outcomes["outcomes"] == {"fixed": 3, "wrong": 2, "intentional": 1, "minor": 0, "pre-existing": 1,
                                         "open": 1, "uncertain": 0}, with_outcomes
    assert with_outcomes["shared"] == 2, "--shared and --outcomes are checked and written together"
    assert "outcomes" not in without, without


def refuses_malformed_outcomes(base):
    """Each case adds up to --findings and matches --confirmed, so only the rule it names can refuse it."""
    home = setup(base)
    lens = ["lens", "correctness", "--findings", "3", "--confirmed", "3", "--outcomes"]
    for args, message in [
        ([*lens, "fixed=2,noise=1"], "isn't one of"),
        ([*lens, "fixed=2,minor=x"], "isn't a whole number"),
        ([*lens, "fixed=4,minor=-1"], "isn't a whole number"),
        ([*lens, "fixed=1,fixed=1,minor=2"], "given twice"),
        ([*lens, "fixed=3,minor"], "has no count"),
        ([*lens, ""], "isn't one of"),
        ([*lens, "fixed=2,minor=2"], "add up to 4, not --findings 3"),
        ([*lens, "fixed=1,wrong=2"], "count 1 confirmed, not --confirmed 3"),
        ([*lens, "fixed=3", "--outcomes", "minor=3"], "--outcomes is given twice"),
    ]:
        out = log(home, *args)
        assert out.returncode == 2 and message in out.stderr, (args, out)
    assert entries(home) == [], "nothing was written"


def a_lens_with_no_findings_logs_without_outcomes(base):
    home = setup(base)
    assert log(home, "lens", "correctness", "--findings", "0", "--confirmed", "0").returncode == 0
    assert log(home, "lens", "correctness", "--findings", "0", "--confirmed", "0", "--outcomes", "fixed=0").returncode == 0
    without, zeros = entries(home)
    assert "outcomes" not in without and set(zeros["outcomes"].values()) == {0}, (without, zeros)


TEST_TYPES = ("unit", "e2e", "integration", "browser", "query-budget", "accessibility", "property-based", "mutation",
              "migration-round-trip", "visual", "design", "regex-worst-case")


def accepts_each_canonical_test_type(base):
    home = setup(base)
    for name in TEST_TYPES:
        out = log(home, "test", name, "--issues", "0")
        assert out.returncode == 0, (name, out.stderr)
    assert [row["name"] for row in entries(home)] == list(TEST_TYPES), entries(home)


def refuses_other_test_type_spellings(base):
    """The monthly job saw regex, regex-timing and regex-worst-case as three types: one name per family now."""
    home = setup(base)
    for name in ("regex", "regex-timing", "property", "typecheck", "Unit"):
        out = log(home, "test", name, "--issues", "0")
        assert out.returncode == 2, (name, out)
        missing = [valid for valid in TEST_TYPES if valid not in out.stderr]
        assert not missing, (name, missing, out.stderr)
    assert entries(home) == [], "nothing was written"


def marks_a_flaky_run(base):
    home = setup(base)
    for args in (["--issues", "0", "--flake"], ["--issues", "2", "--flake", "--secs", "40"], ["--issues", "1"]):
        out = log(home, "test", "unit", *args)
        assert out.returncode == 0, (args, out.stderr)
    flake_only, flake_and_issue, plain = entries(home)
    assert flake_only["flake"] is True and flake_only["issues"] == 0, flake_only
    assert (flake_and_issue["flake"], flake_and_issue["issues"]) == (True, 2), flake_and_issue
    assert "flake" not in plain, "no --flake writes no field, as the rows before it"


def pr_repo(base):
    """A HOME linking the kit, and a repo whose branch edits a code file, edits a test (one assertion removed, one skip
    added) and deletes another test, against main."""
    home, repo = os.path.join(base, "home"), os.path.join(base, "shop")
    os.makedirs(os.path.join(home, ".agents"))
    for part in ("bin", "hooks", "skills", "review-mining"):
        os.symlink(os.path.join(KIT, part), os.path.join(home, ".agents", part))
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo]
    subprocess.run(["git", "init", "-q", "-b", "main", repo], check=True)
    os.makedirs(os.path.join(repo, "tests"))
    files = {"app.py": "def total(n):\n    return n * 2\n",
             "tests/test_app.py": "def test_total():\n    assert total(2) == 4\n    assert total(0) == 0\n"
                                  "    assert total(3) == 6\n",
             "tests/test_old.py": "def test_old():\n    assert 1\n    assert 2\n"}
    for path, text in files.items():
        open(os.path.join(repo, path), "w").write(text)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "base"], check=True)
    subprocess.run([*git, "checkout", "-q", "-b", "feature"], check=True)
    open(os.path.join(repo, "app.py"), "w").write("def total(n):\n    if n < 0:\n        raise ValueError(n)\n    return n + n\n")
    open(os.path.join(repo, "tests/test_app.py"), "w").write(
        "@pytest.mark.skip\ndef test_total():\n    assert total(0) == 1\n    assert total(3)\n")
    open(os.path.join(repo, "tests/test_neg.py"), "w").write("def test_neg():\n    assert total(-1) == -2\n")
    os.remove(os.path.join(repo, "tests/test_old.py"))
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "change"], check=True)
    subprocess.run([*git, "remote", "add", "origin", "git@github.com:Owner/Shop.git"], check=True)
    # gh answers from gh.json when the test writes one, else fails as a logged-out gh does: no test reaches GitHub.
    os.makedirs(os.path.join(home, "shim"))
    shim = os.path.join(home, "shim", "gh")
    open(shim, "w").write(f"#!/bin/sh\ncat {os.path.join(home, 'gh.json')} 2>/dev/null || exit 1\n")
    os.chmod(shim, 0o755)
    return home, repo


def log_pr(home, repo, *args):
    env = {**os.environ, "HOME": home, "PATH": os.path.join(home, "shim") + os.pathsep + os.environ["PATH"]}
    return subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), "pr", *args], cwd=repo, env=env,
                          capture_output=True, text=True)


def gh_answers(home, url, head="feature"):
    with open(os.path.join(home, "gh.json"), "w") as fh:
        json.dump({"url": url, "headRefName": head}, fh)


def records_a_prs_size_and_origin(base):
    home, repo = pr_repo(base)
    out = log_pr(home, repo, "42")
    assert out.returncode == 0, out.stderr
    row = entries(home)[-1]
    assert (row["kind"], row["name"], row["repo"], row["branch"]) == ("pr", 42, "shop", "feature"), row
    assert {"ts", "harness", "tier", "spec_tier", "diff_tier"} <= row.keys(), row
    # app.py: +3 -1; test_app.py: +3 -3; test_neg.py (added): +2; test_old.py (deleted): -3.
    assert {key: row.get(key) for key in ("added", "deleted", "files", "test_files", "tests_deleted")} == \
        {"added": 8, "deleted": 7, "files": 4, "test_files": 2, "tests_deleted": 1}, row
    assert (row["host"], row["remote"]) == ("github.com", "Owner/Shop"), row
    for url, expected in (("https://ghe.example.com/owner/shop.git", ("ghe.example.com", "owner/shop")),
                          ("https://github.com/owner/shop", ("github.com", "owner/shop")),
                          ("ssh://git@ghe.example.com:2222/team/app.git", ("ghe.example.com", "team/app"))):
        subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", url], check=True)
        assert log_pr(home, repo, "42").returncode == 0
        assert (entries(home)[-1]["host"], entries(home)[-1]["remote"]) == expected, (url, entries(home)[-1])
    # An SSH host alias names no real host: the PR's own URL from gh wins over the origin.
    subprocess.run(["git", "-C", repo, "remote", "set-url", "origin", "git@github-work:Owner/Shop.git"], check=True)
    gh_answers(home, "https://github.com/Owner/Shop/pull/42")
    assert log_pr(home, repo, "42").returncode == 0
    assert (entries(home)[-1]["host"], entries(home)[-1]["remote"]) == ("github.com", "Owner/Shop"), entries(home)[-1]


def refuses_a_pr_from_another_branch(base):
    home, repo = pr_repo(base)
    gh_answers(home, "https://github.com/Owner/Shop/pull/42", head="other")
    out = log_pr(home, repo, "42")
    assert out.returncode == 2 and "other" in out.stderr, out
    assert entries(home) == [], "nothing was written"


def records_the_verify_signals(base):
    home, repo = pr_repo(base)
    env = {**os.environ, "HOME": home}
    report = subprocess.run([os.path.join(home, ".agents", "bin", "reports"), "path", "verify"], cwd=repo, env=env,
                            capture_output=True, text=True, check=True).stdout.strip()

    def signals(text):
        if text is None:
            if os.path.exists(report):
                os.remove(report)
        else:
            open(report, "w").write(text)
        out = log_pr(home, repo, "7")
        assert out.returncode == 0, out.stderr
        row = entries(home)[-1]
        return row["verify"], row["red_check"], row["coverage"]

    def verify_report(verdict, *lines):
        return f"# Verify: {verdict}\n\n2026-10-27 10:00:00 · `verify` in `{repo}`\n\n```\n" + "\n".join(lines) + "\n```\n"

    failed = "error: [red check] The changed tests (tests/test_app.py) all pass without your change (on the merge-base), so"
    passed = "ran: red check: pytest on 1 changed test files: 1 of 2 cases fail on the merge-base, pass with the change"
    skipped = "skipped: red check (npm test: package.json has no test script)"
    not_run = "error: red check did not run: phpunit not found"
    assert signals(None) == ("none", None, None), "no report"
    assert signals(verify_report("FAIL (exit 1)", passed, failed)) == ("fail", "failed", None)
    assert signals(verify_report("PASS", skipped, passed)) == ("pass", "passed", None), "a mixed report: one stack ran it"
    assert signals(verify_report("PASS", skipped)) == ("pass", "skipped", None)
    assert signals(verify_report("FAIL (exit 1)", not_run)) == ("fail", "not_run", None)
    assert signals(verify_report("PASS, but nothing was checked", "ran: nothing to check: no changed files")) == \
        ("empty", None, None)
    assert signals(verify_report(
        "PASS", "ran: pytest tests/test_app.py",
        "ran: diff coverage (coverage.py): 8 of 10 changed lines covered by the related tests (80%); uncovered: app.py:3-4",
        "skipped: diff coverage (c8 not installed in node_modules/.bin)",
        "ran: diff coverage (phpunit): 1 of 5 changed lines covered by the related tests (20%)")) == \
        ("pass", None, {"covered": 9, "measured": 15})

    fingerprint = subprocess.run(["python3", os.path.join(KIT, "hooks", "review_stamp.py"), "fingerprint"], cwd=repo,
                                 env=env, capture_output=True, text=True, check=True).stdout.strip()
    stamps = os.path.join(repo, ".git", "agents", "stamps", "feature")
    os.makedirs(stamps, exist_ok=True)
    open(os.path.join(stamps, "verify.stamp"), "w").write(fingerprint)
    assert log_pr(home, repo, "7").returncode == 0
    assert entries(home)[-1]["verify_current"] is True, entries(home)[-1]
    open(os.path.join(repo, "app.py"), "a").write("# edited after verify\n")
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, "commit", "-qam", "edit"], check=True)
    assert log_pr(home, repo, "7").returncode == 0
    assert entries(home)[-1]["verify_current"] is False, "the stamp no longer covers the change"


def records_weakened_tests(base):
    home, repo = pr_repo(base)
    assert log_pr(home, repo, "7").returncode == 0
    assert entries(home)[-1]["tampered"] == {"removed": 1, "loosened": 1, "changed": 1, "skipped_or_focused": 1}, \
        entries(home)[-1]


def a_renamed_test_is_not_a_deleted_one(base):
    home, repo = pr_repo(base)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo]
    subprocess.run([*git, "checkout", "-q", "main", "--", "tests/test_app.py"], check=True)  # similar enough to follow
    subprocess.run([*git, "mv", "tests/test_app.py", "tests/test_cart.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "rename"], check=True)
    assert log_pr(home, repo, "7").returncode == 0
    assert (entries(home)[-1]["test_files"], entries(home)[-1]["tests_deleted"]) == (2, 1), entries(home)[-1]
    subprocess.run([*git, "mv", "tests/test_cart.py", "cart_notes.py"], check=True)  # out of the test paths: gone
    subprocess.run([*git, "commit", "-qm", "move out"], check=True)
    assert log_pr(home, repo, "7").returncode == 0
    assert (entries(home)[-1]["test_files"], entries(home)[-1]["tests_deleted"]) == (1, 2), entries(home)[-1]


def a_dirty_tree_nulls_what_it_would_misread(base):
    home, repo = pr_repo(base)
    clean = log_pr(home, repo, "7")
    assert clean.returncode == 0, clean.stderr
    committed = entries(home)[-1]
    assert isinstance(committed["structure"], dict), committed
    open(os.path.join(repo, "tests", "test_app.py"), "w").write("def test_total():\n    pass\n")
    open(os.path.join(repo, "scratch.py"), "w").write("x = 1\n")
    assert log_pr(home, repo, "7").returncode == 0
    row = entries(home)[-1]
    for key in ("tampered", "verify_current", "tier", "spec_tier", "diff_tier", "structure"):
        assert row[key] is None, (key, row)
    for key in ("added", "deleted", "files", "test_files", "tests_deleted"):
        assert row[key] == committed[key], (key, "sizes count the committed change", row)
    subprocess.run(["git", "-C", repo, "checkout", "-q", "--", "tests/test_app.py"], check=True)
    subprocess.run(["git", "-C", repo, "config", "status.showUntrackedFiles", "no"], check=True)
    assert log_pr(home, repo, "7").returncode == 0
    assert entries(home)[-1]["tampered"] is None, ("an untracked file the user's git config hides", entries(home)[-1])


def a_signal_it_cant_compute_is_null(base):
    home, repo = pr_repo(base)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo]
    subprocess.run([*git, "checkout", "-q", "--orphan", "unrelated"], check=True)  # shares no commit with main
    subprocess.run([*git, "commit", "-q", "-m", "orphan"], check=True)
    report = subprocess.run([os.path.join(home, ".agents", "bin", "reports"), "path", "verify"], cwd=repo,
                            env={**os.environ, "HOME": home}, capture_output=True, text=True, check=True).stdout.strip()
    os.makedirs(report)  # a report that can't be read
    out = log_pr(home, repo, "9")
    assert out.returncode == 0, out.stderr
    row = entries(home)[-1]
    for key in ("added", "deleted", "files", "test_files", "tests_deleted", "tampered", "verify", "verify_current",
                "red_check", "coverage", "structure"):
        assert key in row and row[key] is None, (key, row)
    assert (row["name"], row["remote"]) == (9, "Owner/Shop"), row
    outside = os.path.join(base, "nowhere")
    os.makedirs(outside)
    out = log_pr(home, outside, "9")
    assert out.returncode == 0, out.stderr
    row = entries(home)[-1]
    assert (row["kind"], row["host"], row["remote"], row["added"], row["structure"]) == ("pr", None, None, None, None), row


def undecodable_output_nulls_the_signal(base):
    home, repo = pr_repo(base)
    with open(os.path.join(repo, "tests", "test_bytes.py"), "wb") as fh:
        fh.write(b"def test_bytes():\n    assert name() == '\xff'\n")  # Latin-1, not UTF-8
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, "add", "-A"], check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", repo, "commit", "-qm", "bytes"], check=True)
    out = log_pr(home, repo, "9")
    assert out.returncode == 0, out.stderr
    row = entries(home)[-1]
    assert row["tampered"] is None and row["added"] is not None, row


def records_the_structure_facts(base):
    if not (shutil.which("lizard") and shutil.which("jscpd")) and not os.environ.get("CI"):
        print("skip records_the_structure_facts: lizard or jscpd not installed")
        return
    home, repo = pr_repo(base)
    assert log_pr(home, repo, "7").returncode == 0
    # total gains its `if` (0 → 1); test_total changes, test_neg is new and test_old removed, none of them branching.
    assert entries(home)[-1]["structure"] == {
        "complexity": {"added": 1, "removed": 0, "max_function": 1, "functions": 4, "complete": True},
        "new_clones": {"count": 0, "complete": True}}, entries(home)[-1]


def stand_in_structure_facts(home, script):
    """The kit's structure-facts replaced by this bash script, in a bin of its own linking every other tool."""
    bin_dir = os.path.join(home, ".agents", "bin")
    if os.path.islink(bin_dir):
        os.remove(bin_dir)
        os.makedirs(bin_dir)
        for name in os.listdir(os.path.join(KIT, "bin")):
            if name != "structure-facts":
                os.symlink(os.path.join(KIT, "bin", name), os.path.join(bin_dir, name))
    path = os.path.join(bin_dir, "structure-facts")
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + script)
    os.chmod(path, 0o755)


def keeps_only_structure_facts_of_the_right_shape(base):
    home, repo = pr_repo(base)
    complexity = {"added": 3, "removed": 2, "max_function": 3, "functions": 4, "complete": False}
    clones = {"count": 2, "complete": True}

    def structure(script, **env):
        stand_in_structure_facts(home, script)
        before = len(entries(home))
        out = subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), "pr", "7"], cwd=repo,
                             env={**os.environ, "HOME": home, **env,
                                  "PATH": os.path.join(home, "shim") + os.pathsep + os.environ["PATH"]},
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert len(entries(home)) == before + 1 and entries(home)[-1]["added"] == 8, "the rest of the line is written"
        return entries(home)[-1]["structure"]

    def prints(value, code=0):
        return f"echo \"$@\" > {os.path.join(base, 'args')}\ncat <<'EOF'\n{json.dumps(value)}\nEOF\nexit {code}\n"

    assert structure(prints({"complexity": complexity, "new_clones": clones})) == \
        {"complexity": complexity, "new_clones": clones}
    assert open(os.path.join(base, "args")).read() == "--json main\n", "run against review_stamp.py base"
    subprocess.run(["git", "-C", repo, "branch", "-q", "parent", "main"], check=True)
    subprocess.run(["git", "-C", repo, "config", "branch.feature.agentsBase", "parent"], check=True)
    structure(prints({"complexity": complexity, "new_clones": clones}))
    assert open(os.path.join(base, "args")).read() == "--json parent\n", "a recorded base, not main"
    subprocess.run(["git", "-C", repo, "config", "--unset", "branch.feature.agentsBase"], check=True)
    assert structure(prints({"complexity": None, "new_clones": clones})) == {"complexity": None, "new_clones": clones}
    for value, code, expected, why in (
            ({"complexity": complexity, "new_clones": clones}, 1, None, "a nonzero exit"),
            ({}, 0, None, "an empty object"),
            ([], 0, None, "a list"),
            ({"complexity": complexity}, 0, None, "a part missing"),
            ({"complexity": {**complexity, "removed": -1}, "new_clones": clones}, 0,
             {"complexity": None, "new_clones": clones}, "a negative count"),
            ({"complexity": complexity, "new_clones": {**clones, "count": "2"}}, 0,
             {"complexity": complexity, "new_clones": None}, "a string count"),
            ({"complexity": {**complexity, "functions": True}, "new_clones": clones}, 0,
             {"complexity": None, "new_clones": clones}, "a boolean count"),
            ({"complexity": {**complexity, "complete": 1}, "new_clones": clones}, 0,
             {"complexity": None, "new_clones": clones}, "a non-boolean complete"),
            ({"complexity": complexity, "new_clones": {**clones, "lines": 9}}, 0,
             {"complexity": complexity, "new_clones": None}, "a key the shape lacks")):
        assert structure(prints(value, code)) == expected, why
    assert structure("echo 'not json'\n") is None, "garbage"
    assert structure(f"echo '{'[' * 5000}{']' * 5000}'\n") is None, "nesting too deep for the parser"
    # The tree changes while it measures: what it measured may not be what was pushed.
    assert structure(f"touch {os.path.join(repo, 'late.py')}\n" + prints({"complexity": complexity, "new_clones": clones})) \
        is None, "a tree dirtied while measuring"
    assert entries(home)[-1]["verify_current"] is None and entries(home)[-1]["tier"] is None, entries(home)[-1]
    os.remove(os.path.join(repo, "late.py"))
    started = time.monotonic()
    # A child of the stand-in holds the pipe: only killing the process group ends the wait.
    assert structure("sleep 60\necho '{}'\n", QUALITY_LOG_STRUCTURE_TIMEOUT="1") is None, "a timeout"
    assert time.monotonic() - started < 45, "the timeout didn't stop the wait"
    open(os.path.join(repo, "scratch.py"), "w").write("x = 1\n")
    os.remove(os.path.join(base, "args"))
    assert structure(prints({"complexity": complexity, "new_clones": clones})) is None, "a dirty tree"
    assert not os.path.exists(os.path.join(base, "args")), "a dirty tree still ran structure-facts"


def a_timeout_stops_the_tools_too(base):
    """structure-facts runs lizard and jscpd in sessions of their own, which a kill of its group doesn't reach."""
    home, repo = pr_repo(base)
    tools = os.path.join(base, "tools")
    os.makedirs(tools)
    with open(os.path.join(tools, "lizard"), "w") as fh:
        fh.write("#!/bin/bash\nsleep 97\n")
    os.chmod(os.path.join(tools, "lizard"), 0o755)
    env = {**os.environ, "HOME": home, "QUALITY_LOG_STRUCTURE_TIMEOUT": "3", "STRUCTURE_FACTS_LIZARD_TIMEOUT": "120",
           "PATH": os.pathsep.join((tools, os.path.join(home, "shim"), os.environ["PATH"]))}
    out = subprocess.run([os.path.join(home, ".agents", "bin", "quality-log"), "pr", "7"], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and entries(home)[-1]["structure"] is None, (out.stderr, entries(home)[-1])
    time.sleep(0.5)
    left = subprocess.run(["pgrep", "-f", "sleep 97"], capture_output=True, text=True).stdout.split()
    for pid in left:
        os.kill(int(pid), 9)
    assert not left, "the tool outlived the timeout"


def refuses_a_pr_number_that_isnt_one(base):
    home, repo = pr_repo(base)
    for args in (["0"], ["-3"], ["abc"], ["1.5"], [""], ["#12"], [], ["12", "--added", "5"], ["12", "13"]):
        out = log_pr(home, repo, *args)
        assert out.returncode == 2 and out.stderr, (args, out)
    assert entries(home) == [], "nothing was written"


for test in kit_home.selected((writes_well_formed_entries, records_the_findings_the_other_model_shared, keeps_lens_names_to_the_lenses_there_are,
                               refuses_malformed_entries, a_session_on_a_worktree_logs_to_the_main_checkout,
                               other_kit_layouts_keep_their_rows_in_the_kit, records_each_findings_outcome,
                               refuses_malformed_outcomes, a_lens_with_no_findings_logs_without_outcomes, records_the_spec_and_diff_tiers,
                               accepts_each_canonical_test_type, refuses_other_test_type_spellings, marks_a_flaky_run,
                               records_a_prs_size_and_origin, records_the_verify_signals, records_weakened_tests,
                               a_signal_it_cant_compute_is_null, refuses_a_pr_number_that_isnt_one, refuses_a_pr_from_another_branch,
                               a_dirty_tree_nulls_what_it_would_misread, a_renamed_test_is_not_a_deleted_one, undecodable_output_nulls_the_signal,
                               records_the_structure_facts, keeps_only_structure_facts_of_the_right_shape, a_timeout_stops_the_tools_too)):
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
