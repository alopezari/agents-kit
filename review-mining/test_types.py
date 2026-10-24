#!/usr/bin/env python3
"""Summarize the test rows of logs/quality.jsonl per test type for the monthly job, folding the spellings logged before
bin/quality-log took only canonical names into those names. Unknown names pass through unchanged and are listed.

  test_types.py <summary.json> [--log <quality.jsonl>]

Per type: runs, runs_with_issue (issues > 0; a flake is never an issue), flaky_runs, issues and secs. Reads the log and
never writes it. A malformed line fails naming its line number, and no summary is written; a missing log is no runs.
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile

KIT = os.path.join(os.path.dirname(os.path.realpath(__file__)), "..")
sys.path.insert(0, os.path.join(KIT, "hooks"))
from review_stamp import quality_log  # noqa: E402

ALIASES = {"regex": "regex-worst-case", "regex-timing": "regex-worst-case", "property": "property-based"}


def canonical_test_types():
    sys.dont_write_bytecode = True  # loading quality-log would leave bin/__pycache__ behind, which bin/docs reads as a tool
    loader = importlib.machinery.SourceFileLoader("quality_log", os.path.join(KIT, "bin", "quality-log"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("quality_log", loader))
    loader.exec_module(module)
    return module.TEST_TYPES


TEST_TYPES = canonical_test_types()


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def summarize(lines):
    types = {name: {"runs": 0, "runs_with_issue": 0, "flaky_runs": 0, "issues": 0, "secs": 0} for name in TEST_TYPES}
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except ValueError as error:
            raise ValueError(f"line {number} isn't JSON ({error})") from None
        if not isinstance(row, dict):
            raise ValueError(f"line {number} isn't a JSON object")
        if row.get("kind") != "test":
            continue
        name, issues, secs, flake = row.get("name"), row.get("issues"), row.get("secs", 0), row.get("flake", False)
        if not isinstance(name, str) or not name:
            raise ValueError(f"line {number}: a test row without a name")
        if not is_count(issues) or not is_count(secs):
            raise ValueError(f"line {number}: issues and secs must be whole numbers, not {issues!r} and {secs!r}")
        if not isinstance(flake, bool):
            raise ValueError(f"line {number}: flake is {flake!r}, not true")
        counts = types.setdefault(ALIASES.get(name, name), dict.fromkeys(types["unit"], 0))
        counts["runs"] += 1
        counts["runs_with_issue"] += issues > 0
        counts["flaky_runs"] += flake
        counts["issues"] += issues
        counts["secs"] += secs
    return {"types": types, "unknown": sorted(set(types) - set(TEST_TYPES))}


def main():
    parser = argparse.ArgumentParser(prog="test_types.py", description=__doc__.split("\n\n")[0])
    parser.add_argument("summary")
    parser.add_argument("--log", default=quality_log())
    args = parser.parse_args()
    try:
        with open(args.log) as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        lines = []
    try:
        summary = summarize(lines)
    except ValueError as error:
        sys.exit(f"test_types.py: {args.log}: {error}; no summary written")
    # A temp file then a rename: a run cut short leaves the previous state, never half a summary.
    fd, temp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(args.summary)), prefix=".test-types-")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(summary, fh, indent=1)
        os.replace(temp, args.summary)
    except BaseException:
        os.unlink(temp)
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
