#!/usr/bin/env python3
"""review-mining/test_types.py: the monthly job's per-type test counts, with older spellings folded into one name."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import json
import os
import re
import shutil
import subprocess
import tempfile

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
RESULTS = []
ZERO = {"runs": 0, "runs_with_issue": 0, "flaky_runs": 0, "issues": 0, "timed_runs": 0, "secs": 0}


def row(name, issues, kind="test", **fields):
    return json.dumps({"ts": "2026-10-01T10:00:00+0200", "kind": kind, "name": name, "issues": issues, **fields})


FIXTURE = "\n".join([
    row("regex", 1, secs=10), row("regex-timing", 0, secs=20), row("regex-worst-case", 2, secs=30),
    row("property", 0, secs=5), row("property-based", 3, secs=7),
    row("unit", 2, secs=60),  # an unmarked run that found issues
    row("unit", 0, secs=30, flake=True),  # a flake and nothing else: not a run with an issue
    row("unit", 1, secs=45, flake=True),  # a flake beside a real issue
    row("unit", 0, notes="no secs on older rows"),
    row("typecheck", 1, secs=3),
    json.dumps({"kind": "lens", "name": "correctness", "findings": 4, "confirmed": 2}),
    json.dumps({"kind": "rename", "name": "session-x", "to": "feature/x"}),
]) + "\n"


def summarize(log, summary):
    return subprocess.run(["python3", os.path.join(KIT, "review-mining", "test_types.py"), summary, "--log", log],
                          capture_output=True, text=True)


def counts_each_canonical_type(base):
    log, summary = os.path.join(base, "quality.jsonl"), os.path.join(base, "summary.json")
    open(log, "w").write(FIXTURE)
    before = open(log, "rb").read()
    out = summarize(log, summary)
    assert out.returncode == 0, out.stderr
    assert open(log, "rb").read() == before, "the log is read, never rewritten"
    got = json.load(open(summary))
    types = got["types"]
    assert types["regex-worst-case"] == {"runs": 3, "runs_with_issue": 2, "flaky_runs": 0, "issues": 3, "timed_runs": 3, "secs": 60}, types
    assert types["property-based"] == {"runs": 2, "runs_with_issue": 1, "flaky_runs": 0, "issues": 3, "timed_runs": 2, "secs": 12}, types
    assert types["unit"] == {"runs": 4, "runs_with_issue": 2, "flaky_runs": 2, "issues": 3, "timed_runs": 3, "secs": 135}, \
        f"the flake-only run is flaky, not a run with an issue; the run without secs is untimed, not 0s: {types['unit']}"
    assert types["typecheck"] == {"runs": 1, "runs_with_issue": 1, "flaky_runs": 0, "issues": 1, "timed_runs": 1, "secs": 3}, types
    assert got["unknown"] == ["typecheck"], got["unknown"]
    assert not {"regex", "regex-timing", "property", "correctness", "session-x"} & types.keys(), types.keys()
    assert types["e2e"] == ZERO, "every canonical type is listed, with no runs as zeros"


def every_alias_lands_on_a_canonical_name(base):
    """The canonical names come from bin/quality-log, which refuses everything else: an alias pointing elsewhere would split a type again."""
    caches = os.path.join(base, "pycache")  # macOS's python3 caches outside the tree: a prefix makes every cache visible
    out = subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); import test_types; "
                          "print(sorted(set(test_types.ALIASES.values()) - set(test_types.TEST_TYPES)), len(test_types.TEST_TYPES))",
                          os.path.join(KIT, "review-mining")], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPYCACHEPREFIX": caches})
    assert out.returncode == 0 and out.stdout.split() == ["[]", "12"], out
    cached = [name for _, _, names in os.walk(caches) for name in names if name.startswith("quality-log")]
    assert not cached, f"loading bin/quality-log left a cache, as bin/__pycache__ where bin/docs trips on it: {cached}"


def a_missing_log_is_an_empty_summary(base):
    summary = os.path.join(base, "summary.json")
    out = summarize(os.path.join(base, "none.jsonl"), summary)
    assert out.returncode == 0, out.stderr
    got = json.load(open(summary))
    assert got["unknown"] == [] and len(got["types"]) == 12 and all(v == ZERO for v in got["types"].values()), got


def never_writes_the_summary_over_its_log(base):
    log = os.path.join(base, "quality.jsonl")
    open(log, "w").write(FIXTURE)
    out = summarize(log, os.path.join(base, ".", "quality.jsonl"))
    assert out.returncode != 0 and "is the log" in out.stderr, out
    assert open(log).read() == FIXTURE, "the log keeps its rows"


def a_malformed_line_fails_and_writes_nothing(base):
    log, summary = os.path.join(base, "quality.jsonl"), os.path.join(base, "summary.json")
    for bad, why in [("{not json", "not JSON"), ("[1, 2]", "not an object"), (row("unit", "two"), "issues not a count"),
                     (row("unit", -1), "a negative count"), (row("unit", 1, flake="yes"), "a flake that isn't a boolean"),
                     (row("unit", 1, secs="ten"), "secs not a count"), (json.dumps({"kind": "test", "issues": 1}), "no name")]:
        open(log, "w").write(row("unit", 1) + "\n" + bad + "\n" + row("e2e", 0) + "\n")
        out = summarize(log, summary)
        assert out.returncode != 0 and "line 2" in out.stderr, (why, out)
        assert "Traceback" not in out.stderr, (why, out.stderr)
        assert not os.path.exists(summary), (why, "no summary from a log it couldn't read")
        assert os.listdir(base) == ["quality.jsonl"], (why, os.listdir(base))


def run_sh_summarizes_before_the_prompt(base):
    """The lines run.sh runs, taken from run.sh: a summary on success, none left after a rerun on a malformed log."""
    script = open(os.path.join(KIT, "review-mining", "run.sh")).read()
    prompt_at = script.index('cat > "$RUN/prompt.md"')
    lines = [line for line in script[:prompt_at].splitlines() if "test-types" in line or "test_types.py" in line]
    assert len(lines) == 2 and lines[0].startswith("rm -f ") and "test_types.py" in lines[1], lines
    summary = re.search(r'"(\$RUN/[^"]+\.json)"', lines[0]).group(1)
    assert summary in lines[1], (summary, lines[1])
    error_log = re.search(r'> "(\$RUN/[^"]+\.log)" 2>&1 \|\| true$', lines[1]).group(1)
    prompt = script[prompt_at:script.index("\nEOF\n", prompt_at)]
    assert summary in prompt and error_log in prompt, "the prompt points at the summary and its error log"
    step4 = re.search(r"^4\. .*?(?=^   Also from quality.jsonl)", prompt, re.M | re.S).group(0)
    for rule in (os.path.basename(summary), "flaky runs", "never an issue", "timed_runs", "unavailable", os.path.basename(error_log)):
        assert rule in step4, ("step 4 takes test counts from the summary and states its rules", rule)

    home, run = os.path.join(base, "home"), os.path.join(base, "run")
    os.makedirs(os.path.join(home, ".agents", "logs"))
    os.makedirs(run)
    for part in ("bin", "hooks", "review-mining"):
        os.symlink(os.path.join(KIT, part), os.path.join(home, ".agents", part))
    log = os.path.join(home, ".agents", "logs", "quality.jsonl")
    open(log, "w").write(FIXTURE)
    env = {**os.environ, "HOME": home, "BASE": os.path.join(home, ".agents", "review-mining"), "RUN": run}
    steps = "set -euo pipefail\n" + "\n".join(lines) + "\n"
    path = summary.replace("$RUN", run)

    out = subprocess.run(["bash", "-c", steps], env=env, capture_output=True, text=True)
    assert out.returncode == 0, out
    assert json.load(open(path))["types"]["regex-worst-case"]["runs"] == 3, open(path).read()
    open(log, "a").write("{cut by a crash\n")
    out = subprocess.run(["bash", "-c", steps], env=env, capture_output=True, text=True)
    assert out.returncode == 0, ("run.sh keeps going", out)
    assert not os.path.exists(path), "the previous summary is gone, so the prompt can't read last run's numbers"
    assert f"line {FIXTURE.count(chr(10)) + 1}" in open(error_log.replace("$RUN", run)).read()


for test in kit_home.selected((counts_each_canonical_type, every_alias_lands_on_a_canonical_name, a_missing_log_is_an_empty_summary,
                               never_writes_the_summary_over_its_log, a_malformed_line_fails_and_writes_nothing, run_sh_summarizes_before_the_prompt)):
    base = tempfile.mkdtemp(prefix="agents-test-test-types-")
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
