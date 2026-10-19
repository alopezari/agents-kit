#!/usr/bin/env python3
"""bin/ci-wait: waits for the checks of one commit and says how they ended, with a fake gh standing in for GitHub."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
CI_WAIT = os.path.join(KIT, "bin", "ci-wait")
SHA = "a" * 40
# Answers the three API calls ci-wait makes and `gh run view --log-failed`. Each check-runs call for the commit takes the
# next state from states.json (the last one repeats); a state with "error" makes gh fail as it does offline, and one
# with "raw" answers that text. Lists come in pages of 100, and only --paginate reads past the first. The no-CI probe's
# GraphQL call reads "probe", the places that have checks among "main", "parent" and "pr" (missing: GitHub knows no
# such repository), and "probe_error" fails it; it must pass owner and name as placeholders gh fills in (-F).
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
state_dir = os.environ["FAKE_GH_DIR"]
args = sys.argv[1:]
open(os.path.join(state_dir, "calls.log"), "a").write(" ".join(args) + "\n")
states = json.load(open(os.path.join(state_dir, "states.json")))
counter = os.path.join(state_dir, "count")
count = int(open(counter).read()) if os.path.exists(counter) else 0
state = states[min(count, len(states) - 1)]
if args[:1] == ["run"]:
    print("\n".join("FAIL stop_asks_once: AssertionError" if n == 30 else f"log line {n}" for n in range(1, 201)))
    sys.exit(0)
if state.get("error"):
    print("error connecting to api.github.com", file=sys.stderr)
    sys.exit(1)
endpoint = args[-1]
if "graphql" in args:
    if state.get("probe_error"):
        print("HTTP 502", file=sys.stderr)
        sys.exit(1)
    if "probe" not in state:
        print(json.dumps({"data": {"repository": None}}))
        sys.exit(0)
    assert "-F owner={owner} -F name={repo}" in " ".join(args), args
    parent = next(a.split("=", 1)[1] for a in args if a.startswith("parent="))
    assert parent == os.environ["FAKE_PARENT"], parent
    rollup = lambda checked: {"statusCheckRollup": {"state": "SUCCESS"} if checked else None}
    probe = state["probe"]
    print(json.dumps({"data": {"repository": {
        "defaultBranchRef": {"target": rollup("main" in probe)}, "parent": rollup("parent" in probe),
        "pullRequests": {"nodes": [{"commits": {"nodes": [{"commit": rollup("pr" in probe)}]}}, {"commits": {"nodes": [
            {"commit": rollup(False)}]}}]}}}}))
    sys.exit(0)
if "/check-runs" in endpoint:
    open(counter, "w").write(str(count + 1))
    items = state.get("runs", [])
else:
    items = state.get("statuses", [])
if "raw" in state:
    print(state["raw"])
    sys.exit(0)
items = items if "--paginate" in args else items[:100]
print("\n".join(json.dumps(item) for item in items))
'''


def run(name, status="completed", conclusion="success", job=None):
    url = f"https://github.com/o/r/actions/runs/1/job/{job}" if job else "https://ci.example.com/build/1"
    return {"name": name, "status": status, "conclusion": conclusion if status == "completed" else None, "details_url": url}


def ci_wait(states, *args, grace, ci_config=None, unknown_sha=False, history="two", subdir=""):
    """With ci_config (None: no repo), runs in a repo whose last commit holds that file ("": none): two commits, one
    ("root"), or a depth-1 clone of two ("shallow"), from subdir of it. unknown_sha asks about a commit the repo doesn't
    have. Returns the exit code, the output and the number of no-CI probes."""
    base = tempfile.mkdtemp(prefix="agents-test-ci-wait-")
    try:
        cwd, sha, parent = base, SHA, ""
        if ci_config is not None:
            repo = os.path.join(base, "repo")
            git = lambda *a: subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t", *a],
                                            capture_output=True, text=True, check=True).stdout.strip()
            os.makedirs(os.path.join(repo, "src"))
            git("init", "-q")
            if history != "root":
                git("commit", "-q", "--allow-empty", "-m", "first")
            if ci_config:
                os.makedirs(os.path.dirname(os.path.join(repo, ci_config)) or repo, exist_ok=True)
                open(os.path.join(repo, ci_config), "w").write("on: push\n")
                git("add", "-A")
            git("commit", "-q", "--allow-empty", "-m", "last")
            sha = git("rev-parse", "HEAD")
            parent = sha if history == "root" else git("rev-parse", "HEAD^")
            if history == "shallow":
                clone = os.path.join(base, "clone")
                subprocess.run(["git", "clone", "-q", "--depth", "1", "file://" + repo, clone], check=True)
                repo = clone
            sha = SHA if unknown_sha else sha
            cwd = os.path.join(repo, subdir)
            os.makedirs(cwd, exist_ok=True)
        os.makedirs(os.path.join(base, "bin"))
        gh = os.path.join(base, "bin", "gh")
        open(gh, "w").write(FAKE_GH)
        os.chmod(gh, 0o755)
        json.dump(states, open(os.path.join(base, "states.json"), "w"))
        env = {**os.environ, "PATH": os.path.join(base, "bin") + ":" + os.environ["PATH"], "FAKE_GH_DIR": base,
               "CI_WAIT_POLL_SECS": "0.2", "CI_WAIT_GRACE_SECS": grace, "FAKE_PARENT": parent}
        result = subprocess.run([CI_WAIT, "--sha", sha, *args], capture_output=True, text=True, env=env, timeout=60,
                                cwd=cwd)
        calls = os.path.join(base, "calls.log")
        probes = open(calls).read().count(" graphql ") if os.path.exists(calls) else 0
        return result.returncode, result.stdout + result.stderr, probes
    finally:
        shutil.rmtree(base, ignore_errors=True)


CASES = [  # (name, states, args, exit code, text that must appear, text that must not[, grace in seconds])
    ("all passed", [{"runs": [run("suite"), run("lint", conclusion="skipped")]}], [], 0, ["suite: success", "lint: skipped"], []),
    ("a failed Actions job shows its log tail", [{"runs": [run("suite", conclusion="failure", job=7), run("lint")]}], [], 1,
     ["suite: failure", "FAIL stop_asks_once: AssertionError", "log line 200", f"ci-wait: failed on {SHA[:12]}: suite\n"],
     ["log line 100\n", "log line 150\n"]),
    ("--no-log names the failure without its log", [{"runs": [run("suite", conclusion="failure", job=7)]}], ["--no-log"], 1,
     ["suite: failure", "failed on"], ["log line"]),
    ("a failing status from another CI", [{"runs": [run("suite")], "statuses": [{"context": "buildkite", "state": "failure",
                                                                                  "target_url": "https://bk"}]}],
     [], 1, ["buildkite: failure"], []),
    ("running, then passed", [{"runs": [run("suite", status="in_progress")]}, {"runs": [run("suite")]}], ["--timeout", "20"], 0,
     ["suite: success"], []),
    ("still running at the timeout", [{"runs": [run("suite", status="queued")]}], ["--timeout", "1"], 2,
     ["suite: queued", "still running"], []),
    ("--once while running doesn't wait", [{"runs": [run("suite", status="in_progress")]}, {"runs": [run("suite")]}], ["--once"], 2,
     ["suite: in_progress"], ["suite: success"]),
    ("no checks after the grace period", [{"runs": []}], ["--timeout", "20"], 3, ["no checks"], [], "1"),
    ("no checks yet at a timeout inside the grace period", [{"runs": []}], ["--timeout", "0.5"], 2, ["no checks yet"], []),
    ("green on the last look before the timeout isn't settled", [{"runs": [run("suite", status="queued")]}, {"runs": [run("suite")]}],
     ["--timeout", "0.3"], 2, ["still running"], []),
    ("a state that isn't text is unreadable", [{"runs": [{"name": "suite", "status": [], "conclusion": None}]}], [], 4,
     ["couldn't read"], []),
    ("no checks yet, then checks that pass", [{"runs": []}, {"runs": [run("suite")]}], ["--timeout", "20"], 0, ["suite: success"], []),
    ("GitHub can't be read", [{"error": True}], [], 4, ["couldn't read"], []),
    ("a failure on the second page of checks", [{"runs": [run(f"job {n}") for n in range(100)] + [run("late", conclusion="failure")]}],
     [], 1, ["late: failure"], []),
    ("a check that registers after the others passed", [{"runs": [run("fast")]}, {"runs": [run("fast"), run("slow", status="queued")]},
                                                        {"runs": [run("fast"), run("slow", conclusion="failure")]}],
     ["--timeout", "20"], 1, ["slow: failure"], []),
    ("an answer missing a field is unreadable, not a failure", [{"raw": '{"status": "completed"}'}], [], 4, ["couldn't read"], []),
    ("an endless timeout is refused", [{"runs": [run("suite")]}], ["--timeout", "nan"], 64, ["not a number of seconds"], []),
    ("a flag without its value is refused", [{"runs": [run("suite")]}], ["--timeout"], 64, ["expected one argument"], []),
]

NO_CI = [  # (name, CI config file in the commit, state, args, exit code, text that must appear[, grace])
    ("a repo with no CI config and no checks anywhere runs no CI", "", {"runs": [], "probe": []}, [], 5, ["runs no CI"]),
    ("--once says the repo runs no CI too", "", {"runs": [], "probe": []}, ["--once"], 5, ["runs no CI"]),
    ("a workflow in the commit keeps the wait", ".github/workflows/ci.yml", {"runs": [], "probe": []},
     ["--timeout", "0.5"], 2, ["no checks yet"]),
    ("a workflow in the commit, no checks after the grace", ".github/workflows/ci.yml", {"runs": [], "probe": []},
     ["--timeout", "20"], 3, ["no checks"], "1"),
    ("a workflow in the commit, --once with no checks", ".github/workflows/ci.yml", {"runs": [], "probe": []}, ["--once"],
     3, ["no checks"]),
    ("a Jenkinsfile counts as CI config", "Jenkinsfile", {"runs": [], "probe": []}, ["--once"], 3, ["no checks"]),
    ("checks on the default branch keep the wait", "", {"runs": [], "probe": ["main"]}, ["--timeout", "0.5"], 2,
     ["no checks yet"]),
    ("checks on the parent keep the wait", "", {"runs": [], "probe": ["parent"]}, ["--timeout", "0.5"], 2,
     ["no checks yet"]),
    ("checks on a recent pull request keep the wait (CI that runs only on pull requests)", "",
     {"runs": [], "probe": ["pr"]}, ["--once"], 3, ["no checks"]),
    ("a probe GitHub can't answer keeps the wait", "", {"runs": [], "probe_error": True}, ["--timeout", "0.5"], 2,
     ["no checks yet"]),
    ("a repository GitHub doesn't know keeps the wait", "", {"runs": []}, ["--timeout", "0.5"], 2, ["no checks yet"]),
    ("a repo with no CI whose commit has checks still reports them", "", {"runs": [run("suite")], "probe": []}, [], 0,
     ["suite: success"]),
    *[(f"{config} counts as CI config", config, {"runs": [], "probe": []}, ["--once"], 3, ["no checks"])
      for config in [".circleci/config.yml", ".buildkite/pipeline.yml", ".teamcity/settings.kts", ".travis.yml",
                     ".gitlab-ci.yml", "azure-pipelines.yml", "azure-pipelines.yaml", ".drone.yml", "appveyor.yml",
                     ".appveyor.yml", ".cirrus.yml", ".semaphore/semaphore.yml", ".woodpecker/ci.yml",
                     ".woodpecker.yml", "bitrise.yml", "codemagic.yaml"]],
]

fail = 0
for name, ci_config, state, args, code, present, *grace in NO_CI:
    got, out, _ = ci_wait([state], *args, grace=grace[0] if grace else "30", ci_config=ci_config)
    ok = got == code and all(p in out for p in present) and (code == 5 or "runs no CI" not in out)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": exit {got}, wanted {code}\n     {out.strip()[-400:]}"))

# Outside a repo, or with a SHA the checkout doesn't have, nothing proves the repo runs no CI.
for name, ci_config, unknown in [("outside a repo, no checks keeps the wait", None, False),
                                 ("a commit the checkout doesn't have keeps the wait", "", True)]:
    got, out, _ = ci_wait([{"runs": [], "probe": []}], "--timeout", "0.5", grace="30", ci_config=ci_config,
                          unknown_sha=unknown)
    ok = got == 2 and "runs no CI" not in out
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": exit {got}\n     {out.strip()[-400:]}"))

# (name, keyword arguments, state, args, exit code, no-CI probes)
for name, how, state, args, code, probes in [
    ("run from a subdirectory, a workflow at the root keeps the wait", {"ci_config": ".github/workflows/ci.yml",
     "subdir": "src"}, {"runs": [], "probe": []}, ["--once"], 3, None),
    ("a repo's first commit stands in for its own parent", {"ci_config": "", "history": "root"},
     {"runs": [], "probe": []}, ["--once"], 5, 1),
    ("a shallow clone, whose parent is unknown, keeps the wait", {"ci_config": "", "history": "shallow"},
     {"runs": [], "probe": []}, ["--timeout", "0.5"], 2, 0),
    ("the probe runs once while polling", {"ci_config": "", "history": "two"}, {"runs": [], "probe": ["main"]},
     ["--timeout", "1"], 2, 1),
    ("a commit with checks is never probed", {"ci_config": ""}, {"runs": [run("suite")], "probe": []}, [], 0, 0),
]:
    got, out, made = ci_wait([state], *args, grace="30", **how)
    ok = got == code and (code == 5) == ("runs no CI" in out) and probes in (None, made)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": exit {got}, {made} probes\n     {out.strip()[-400:]}"))

# A long default grace: on a loaded runner one fake gh call can take over a second, which a short grace reads as
# "no checks" in cases about something else.
for name, states, args, code, present, absent, *grace in CASES:
    got, out, _ = ci_wait(states, *args, grace=grace[0] if grace else "30")
    ok = got == code and all(p in out for p in present) and not any(a in out for a in absent)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": exit {got}, wanted {code}\n     {out.strip()[-400:]}"))
sys.exit(fail)
