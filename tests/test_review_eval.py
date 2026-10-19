#!/usr/bin/env python3
"""review-mining/eval.py: cases are added once and checked against their checkout, and a run reviews each case from a
detached worktree with the real bundle.sh, scoring hits, misses, false alarms and errors. The model is a stub that
answers per commit."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
STUB = """#!/bin/bash
# Answers with $STUB_ANSWERS/<commit>.out, or fails like a model that errored.
cat > "$STUB_ANSWERS/$(git rev-parse HEAD).prompt"
echo "$(basename "$0") $*" >> "$STUB_ANSWERS/calls"
pwd -P > "$STUB_ANSWERS/$(git rev-parse HEAD).cwd"
answer="$STUB_ANSWERS/$(git rev-parse HEAD).out"
[ -f "$answer" ] || { echo "model unavailable" >&2; exit 1; }
cat "$answer"
"""


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True).stdout.strip()


def setup(base):
    """A HOME whose .agents is the kit except review-mining, which holds eval.py and the evals it writes; a repo with
    a base commit and one commit per case; stub codex and claude commands."""
    home = os.path.join(base, "home")
    agents = os.path.join(home, ".agents")
    os.makedirs(os.path.join(agents, "review-mining"))
    for entry in os.listdir(KIT):
        if entry != "review-mining":
            os.symlink(os.path.join(KIT, entry), os.path.join(agents, entry))
    os.symlink(os.path.join(KIT, "review-mining", "eval.py"), os.path.join(agents, "review-mining", "eval.py"))
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    git(repo, "init", "--quiet", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    commits = {}
    for name in ("base", "hit", "miss", "clean", "clean2"):
        with open(os.path.join(repo, "cart.py"), "a") as fh:
            fh.writelines(f"line_{name}_{n} = {n}\n" for n in range(40))
        git(repo, "add", "cart.py")
        git(repo, "commit", "--quiet", "-m", name)
        commits[name] = git(repo, "rev-parse", "HEAD")
    bin_dir = os.path.join(base, "bin")
    os.makedirs(bin_dir)
    for model in ("codex", "claude"):
        with open(os.path.join(bin_dir, model), "w") as fh:
            fh.write(STUB)
        os.chmod(os.path.join(bin_dir, model), 0o755)
    answers = os.path.join(base, "answers")
    os.makedirs(answers)
    env = {**os.environ, "HOME": home, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STUB_ANSWERS": answers}
    return env, repo, commits, answers


def evals(env, *args):
    return subprocess.run([os.path.join(env["HOME"], ".agents", "review-mining", "eval.py"), *args],
                          capture_output=True, text=True, env=env)


def cases(env):
    path = os.path.join(env["HOME"], ".agents", "review-mining", "evals", "cases.jsonl")
    return [json.loads(line) for line in open(path)] if os.path.exists(path) else []


def add_bug(env, repo, sha, base, line, lens="Correctness"):
    return evals(env, "add", "--checkout", repo, "--sha", sha, "--base", base, "--lens", lens,
                 "--goal", "Total the cart", "--file", "cart.py", "--line", str(line), "--summary", "total off by one")


def answer(answers, sha, found):
    with open(os.path.join(answers, f"{sha}.out"), "w") as fh:
        fh.write(f"Working notes: looked at cart.py:96 before the section.\n## Found\n{found}\n")


def adds_each_case_once(base):
    env, repo, commits, _ = setup(base)
    worktree = os.path.join(base, "review-worktree")
    git(repo, "worktree", "add", "--quiet", "--detach", worktree)
    assert add_bug(env, worktree, commits["hit"], commits["base"], 60, lens="correctness").returncode == 0
    git(repo, "worktree", "remove", worktree)
    again = add_bug(env, repo, commits["hit"], commits["base"], 60)
    assert again.returncode == 0 and "already" in again.stdout, again
    git(repo, "worktree", "add", "--quiet", "--detach", worktree, commits["clean"])
    clean = evals(env, "add", "--checkout", worktree, "--sha", "HEAD", "--base", "HEAD~1", "--lens", "Correctness",
                  "--goal", "Clean change", "--clean")
    git(repo, "worktree", "remove", worktree)
    assert clean.returncode == 0, clean
    bug, clean_case = cases(env)
    assert (bug["kind"], bug["sha"], bug["base"], bug["file"], bug["line"]) == \
        ("bug", commits["hit"], commits["base"], "cart.py", 60), bug
    assert bug["checkout"] == os.path.realpath(repo) and bug["lens"] == "Correctness", \
        "the main checkout, which outlives the worktree a case is added from, and the lens as bundle.sh names it"
    pinned = subprocess.run(["git", "-C", repo, "for-each-ref", "--format=%(objectname)", "refs/review-evals/"],
                            capture_output=True, text=True).stdout.split()
    assert sorted(pinned) == sorted({commits["hit"], commits["clean"]}), \
        f"each case's commit is pinned, so gc keeps it after its branch is deleted: {pinned}"
    assert clean_case["kind"] == "clean" and clean_case["sha"] == commits["clean"] and "file" not in clean_case, \
        "HEAD as the worktree named it, stored as a commit, so a moved branch can't change the case"


def refuses_a_limit_below_one(base):
    env, _, _, _ = setup(base)
    for limit in ("0", "-3"):
        out = evals(env, "run", "--model", "codex", "--limit", limit)
        assert out.returncode == 2 and "at least 1" in out.stderr, out


def refuses_cases_it_cant_replay(base):
    env, repo, commits, _ = setup(base)
    common = ["--checkout", repo, "--base", commits["base"], "--lens", "Correctness", "--goal", "g"]
    bug = ["--file", "cart.py", "--line", "3", "--summary", "s"]
    for args, message in (
            (["--sha", "f" * 40, *bug], "isn't a commit"),
            (["--sha", commits["hit"], "--file", "nope.py", "--line", "3", "--summary", "s"], "isn't a file"),
            (["--sha", commits["hit"], "--file", "", "--line", "3", "--summary", "s"], "isn't a file"),
            (["--sha", commits["hit"], "--file", ".", "--line", "3", "--summary", "s"], "isn't a file"),
            (["--sha", commits["hit"], *bug, "--clean"], "either --clean"),
            (["--sha", commits["hit"], "--file", "cart.py", "--line", "3"], "either --clean"),
            (["--sha", commits["hit"], "--file", "cart.py", "--line", "81", "--summary", "s"], "isn't a line"),
            (["--sha", commits["hit"], "--file", "cart.py", "--line", "0", "--summary", "s"], "isn't a line"),
            (["--sha", commits["base"], *bug], "no change to review"),
            (["--sha", commits["hit"], *bug, "--base", commits["miss"]], "no change to review"),
            (["--sha", commits["hit"], *bug, "--lens", "spec"], "isn't a lens")):
        out = evals(env, "add", *common, *args)
        assert out.returncode == 2 and message in out.stderr, (args, out)
    assert cases(env) == [], "nothing was written"


def scores_hits_misses_and_false_alarms(base):
    env, repo, commits, answers = setup(base)
    add_bug(env, repo, commits["hit"], commits["base"], 63)
    add_bug(env, repo, commits["miss"], commits["hit"], 96)
    evals(env, "add", "--checkout", repo, "--sha", commits["clean"], "--base", commits["miss"], "--lens", "Correctness",
          "--goal", "Clean change", "--clean")
    answer(answers, commits["hit"], "- Important: total off by one — shop/cart.py:45-48 — high")
    answer(answers, commits["miss"], "- Important: wrong total — cart.py:80 — high")
    evals(env, "add", "--checkout", repo, "--sha", commits["clean2"], "--base", commits["clean"], "--lens", "Correctness",
          "--goal", "Another clean change", "--clean")
    answer(answers, commits["clean"], "- Important: a — cart.py:130\n- Important: b — cart.py:140\n- Nit: c — cart.py:150")
    answer(answers, commits["clean2"], "No findings.")
    out = evals(env, "run", "--model", "codex")
    assert out.returncode == 0, out
    assert "- Correctness: recall 1/2, 1.0 Important per clean case, 0 errors, 0 skipped (no previous run)" in out.stdout, \
        f"63 is within 15 lines of 45-48's end, 80 is 16 short of 96, and the Nit isn't a false alarm, over two clean cases:\n{out.stdout}"
    prompt = open(os.path.join(answers, f"{commits['hit']}.prompt")).read()
    assert "Goal of the change: Total the cart" in prompt and "+line_hit_0 = 0" in prompt and "behavior bugs only" in prompt, \
        "the model gets bundle.sh's prompt for the case's commit, lens and goal"
    calls = open(os.path.join(answers, "calls")).read().splitlines()
    assert len(calls) == 4 and all("-s read-only" in call for call in calls), calls
    cwd = open(os.path.join(answers, f"{commits['hit']}.cwd")).read().strip()
    assert cwd != os.path.realpath(repo) and not os.path.exists(cwd), f"reviewed in a worktree, then removed: {cwd}"
    assert git(repo, "worktree", "list").count("\n") == 0, git(repo, "worktree", "list")
    runs = os.path.join(env["HOME"], ".agents", "review-mining", "evals", "runs")
    first = os.listdir(runs)
    assert len(first) == 1 and first[0].endswith("-codex.jsonl"), first
    assert [json.loads(line)["result"] for line in open(os.path.join(runs, first[0]))] == ["hit", "miss", "clean", "clean"]
    assert "no previous run (results" in out.stdout, out.stdout
    answer(answers, commits["hit"], "- Important: something else — cart.py:5 — high")
    assert evals(env, "run", "--model", "claude").returncode == 0, "the claude run isn't codex's previous run"
    again = evals(env, "run", "--model", "codex")
    assert "4 cases, same cases as the previous run" in again.stdout and "- Correctness: recall 0/2, 1.0 Important" \
        in again.stdout and "(previous run: recall 1/2, 1.0 Important per clean case, 0 errors, 0 skipped)" \
        in again.stdout, again.stdout
    limited = evals(env, "run", "--model", "codex", "--limit", "1")
    assert "2 cases, the cases differ from the previous run's" in limited.stdout and \
        "- Correctness: recall 0/1, 0.0 Important per clean case" in limited.stdout, \
        f"--limit 1 takes the newest bug case and the newest clean one, and says the set changed:\n{limited.stdout}"
    assert len(os.listdir(runs)) == 4, "each run keeps its own results file"
    answer(answers, commits["hit"], "- Important: total off by one — shop/cart.py:45-48 — high")
    add_bug(env, repo, commits["hit"], commits["base"], 63, lens="Security")
    widened = evals(env, "run", "--model", "codex")
    calls = open(os.path.join(answers, "calls")).read().splitlines()
    claude = next(call for call in calls if call.startswith("claude"))
    assert "Bash(git diff:*)" in claude and "--permission-mode dontAsk" in claude and "--no-session-persistence" \
        in claude, claude
    assert "- Security: recall 1/1" in widened.stdout and "(not in the previous run)" in widened.stdout, widened.stdout


def cases_with_the_same_review_share_one_pass(base):
    env, repo, commits, answers = setup(base)
    add_bug(env, repo, commits["hit"], commits["base"], 47)
    add_bug(env, repo, commits["hit"], commits["base"], 75)
    answer(answers, commits["hit"], "- Important: total off by one — cart.py:45-48 — high")
    out = evals(env, "run", "--model", "codex")
    assert "recall 1/2" in out.stdout, out.stdout
    assert len(open(os.path.join(answers, "calls")).read().splitlines()) == 1, "two comments on one PR, one review"


def a_failed_review_is_an_error_not_a_miss(base):
    env, repo, commits, answers = setup(base)
    add_bug(env, repo, commits["hit"], commits["base"], 60)
    add_bug(env, repo, commits["miss"], commits["hit"], 96)
    evals(env, "add", "--checkout", repo, "--sha", commits["clean"], "--base", commits["miss"], "--lens", "Correctness",
          "--goal", "Clean change", "--clean")
    with open(os.path.join(answers, f"{commits['miss']}.out"), "w") as fh:
        fh.write("I looked at cart.py:96 and ran out of time.\n")
    gone = os.path.join(base, "gone")
    shutil.copytree(repo, gone)
    add_bug(env, gone, commits["hit"], commits["base"], 10, lens="Security")
    shutil.rmtree(gone)
    out = evals(env, "run", "--model", "claude")
    assert out.returncode == 0, out
    assert "- Correctness: recall n/a, no clean case, 3 errors, 0 skipped" in out.stdout, \
        f"a failed model, an answer without ## Found and a clean case that failed are errors:\n{out.stdout}"
    assert "- Security: recall n/a, no clean case, 0 errors, 1 skipped" in out.stdout, out.stdout
    assert "model unavailable" in out.stderr and "no ## Found" in out.stderr and "no checkout" in out.stderr, out.stderr
    assert git(repo, "worktree", "list").count("\n") == 0, "worktrees go even when the review fails"
    no_model = evals({**env, "PATH": "/usr/bin:/bin"}, "run", "--model", "codex")
    assert "- Correctness: recall n/a, no clean case, 3 errors" in no_model.stdout and "No such file" \
        in no_model.stderr, no_model
    skills = os.path.join(env["HOME"], ".agents", "skills")
    os.unlink(skills)
    os.makedirs(os.path.join(skills, "self-review"))
    os.symlink(os.path.join(KIT, "skills", "self-review", "lenses.md"), os.path.join(skills, "self-review", "lenses.md"))
    with open(os.path.join(skills, "self-review", "bundle.sh"), "w") as fh:
        fh.write("#!/bin/bash\necho 'no such base' >&2; exit 1\n")
    os.chmod(os.path.join(skills, "self-review", "bundle.sh"), 0o755)
    answer(answers, commits["miss"], "- Important: x — cart.py:96")
    with open(os.path.join(env["HOME"], ".agents", "review-mining", "evals", "cases.jsonl"), "a") as fh:
        fh.write(json.dumps({**cases(env)[0], "lens": "Renamed lens", "line": 5}) + "\n")
    broken = evals(env, "run", "--model", "codex")
    assert "- Renamed lens: recall n/a, no clean case, 1 errors" in broken.stdout, broken.stdout
    assert "- Correctness: recall n/a, no clean case, 3 errors" in broken.stdout and "bundle.sh exit 1" in broken.stderr, broken


for test in (adds_each_case_once, refuses_cases_it_cant_replay, scores_hits_misses_and_false_alarms,
             cases_with_the_same_review_share_one_pass, a_failed_review_is_an_error_not_a_miss, refuses_a_limit_below_one):
    base = tempfile.mkdtemp(prefix="agents-test-review-eval-")
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
