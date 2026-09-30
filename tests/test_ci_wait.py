#!/usr/bin/env python3
"""bin/ci-wait: waits for the checks of one commit and says how they ended, with a fake gh standing in for GitHub."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
CI_WAIT = os.path.join(KIT, "bin", "ci-wait")
SHA = "a" * 40
# Answers the two API calls ci-wait makes and `gh run view --log-failed`. Each check-runs call takes the next
# state from states.json (the last one repeats); a state with "error" makes gh fail as it does offline, and one with
# "raw" answers that text. Lists come in pages of 100, and only --paginate reads past the first.
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


def ci_wait(states, *args):
    base = tempfile.mkdtemp(prefix="agents-test-ci-wait-")
    try:
        os.makedirs(os.path.join(base, "bin"))
        gh = os.path.join(base, "bin", "gh")
        open(gh, "w").write(FAKE_GH)
        os.chmod(gh, 0o755)
        json.dump(states, open(os.path.join(base, "states.json"), "w"))
        env = {**os.environ, "PATH": os.path.join(base, "bin") + ":" + os.environ["PATH"], "FAKE_GH_DIR": base,
               "CI_WAIT_POLL_SECS": "0.2"}
        result = subprocess.run([CI_WAIT, "--sha", SHA, *args], capture_output=True, text=True, env=env, timeout=60)
        return result.returncode, result.stdout + result.stderr
    finally:
        shutil.rmtree(base, ignore_errors=True)


CASES = [  # (name, states, args, exit code, text that must appear, text that must not)
    ("all passed", [{"runs": [run("suite"), run("lint", conclusion="skipped")]}], [], 0, ["suite: success", "lint: skipped"], []),
    ("a failed Actions job shows its log tail", [{"runs": [run("suite", conclusion="failure", job=7), run("lint")]}], [], 1,
     ["suite: failure", "FAIL stop_asks_once: AssertionError", "log line 200"], ["log line 100\n", "log line 150\n"]),
    ("a failing status from another CI", [{"runs": [run("suite")], "statuses": [{"context": "buildkite", "state": "failure",
                                                                                  "target_url": "https://bk"}]}],
     [], 1, ["buildkite: failure"], []),
    ("running, then passed", [{"runs": [run("suite", status="in_progress")]}, {"runs": [run("suite")]}], ["--timeout", "20"], 0,
     ["suite: success"], []),
    ("still running at the timeout", [{"runs": [run("suite", status="queued")]}], ["--timeout", "1"], 2,
     ["suite: queued", "still running"], []),
    ("--once while running doesn't wait", [{"runs": [run("suite", status="in_progress")]}, {"runs": [run("suite")]}], ["--once"], 2,
     ["suite: in_progress"], ["suite: success"]),
    ("no checks after the grace period", [{"runs": []}], ["--timeout", "1"], 3, ["no checks"], []),
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

fail = 0
for name, states, args, code, present, absent in CASES:
    got, out = ci_wait(states, *args)
    ok = got == code and all(p in out for p in present) and not any(a in out for a in absent)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": exit {got}, wanted {code}\n     {out.strip()[-400:]}"))
sys.exit(fail)
