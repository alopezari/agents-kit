#!/usr/bin/env python3
"""Replay bugs that got past the self-review against its reviewers, and clean changes for their false alarms.

  eval.py add --checkout DIR --sha SHA --base REF --lens LENS --goal "..." --file PATH --line N --summary "..."
  eval.py add --checkout DIR --sha SHA --base REF --lens LENS --goal "..." --clean
  eval.py run --model claude|codex [--limit N]

A bug case is a commit that held the bug, the base it was reviewed against and where the bug was. `run` reviews the
newest N bug and N clean cases as a cross-model first pass does (bundle.sh piped to the model, in a detached worktree
of the commit): a bug case is a hit when the review's `## Found` names its file within 15 lines of the bug, a match on
place, not on reason; a clean case counts the Found lines that say Important. Cases and results hold repo paths and bug summaries, so they stay in
review-mining/evals/, which git ignores.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import time

EVALS = os.path.join(os.path.expanduser("~/.agents"), "review-mining", "evals")
CASES = os.path.join(EVALS, "cases.jsonl")
RUNS = os.path.join(EVALS, "runs")
BUNDLE = os.path.expanduser("~/.agents/skills/self-review/bundle.sh")
LENSES = os.path.expanduser("~/.agents/skills/self-review/lenses.md")
NEAR = 15  # a reviewer names the line its finding starts on, often the function above the faulty line
REVIEW_SECS = 15 * 60
MODELS = {
    "codex": ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-"],
    "claude": ["claude", "-p", "--allowedTools", "Read,Grep,Glob", "--disallowedTools", "Edit,Write"],
}


def git(checkout, *args):
    return subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True)


def read_cases():
    if not os.path.exists(CASES):
        return []
    with open(CASES) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def case_key(case):
    return (case["sha"], case["lens"], case.get("file"), case.get("line"))


def lens_heading(name):
    """The lens as bundle.sh selects it, its heading in lenses.md, or None: an unknown lens would review without one."""
    with open(LENSES) as fh:
        headings = [line[3:].strip() for line in fh if line.startswith("## ")]
    return next((heading for heading in headings if heading.lower() == name.lower()), None)


def add(args, parser):
    lens = lens_heading(args.lens)
    if lens is None:
        parser.error(f"--lens {args.lens!r} isn't a lens heading in {LENSES}")
    common = git(args.checkout, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode:
        parser.error(f"--checkout {args.checkout!r} isn't a git checkout")
    # The main checkout, not a worktree of it: follow-pr runs in worktrees that are removed afterwards.
    checkout = os.path.dirname(common.stdout.strip())
    commits = {}
    for name in ("sha", "base"):
        out = git(checkout, "rev-parse", "--verify", "--quiet", getattr(args, name) + "^{commit}")
        if out.returncode:
            parser.error(f"--{name} {getattr(args, name)!r} isn't a commit in {checkout}")
        commits[name] = out.stdout.strip()
    bug = [value is not None for value in (args.file, args.line, args.summary)]
    if (args.clean and any(bug)) or (not args.clean and not all(bug)):
        parser.error("give either --clean, or all of --file, --line and --summary")
    if git(checkout, "merge-base", "--is-ancestor", commits["base"], commits["sha"]).returncode \
            or commits["base"] == commits["sha"]:
        parser.error(f"--base {args.base!r} isn't an earlier commit of {args.sha}, so there's no change to review")
    if args.file:
        content = git(checkout, "show", f"{commits['sha']}:{args.file}")
        if content.returncode:
            parser.error(f"--file {args.file!r} isn't in {args.sha}")
        if not 1 <= args.line <= len(content.stdout.splitlines()):
            parser.error(f"--line {args.line} isn't a line of {args.file} at {args.sha}")
    case = {"added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "clean" if args.clean else "bug",
            "checkout": checkout, "sha": commits["sha"], "base": commits["base"], "lens": lens, "goal": args.goal}
    if not args.clean:
        case.update(file=args.file, line=args.line, summary=args.summary)
    if any(case_key(old) == case_key(case) for old in read_cases()):
        print(f"eval.py: that case is already in {CASES}")
        return 0
    os.makedirs(EVALS, exist_ok=True)
    with open(CASES, "a") as fh:
        fh.write(json.dumps(case) + "\n")
    print(f"eval.py: added a {case['kind']} case to {CASES}")
    return 0


def found_section(text):
    """The last `## Found` heading to the end, as the self-review reads a cross-model pass."""
    start = [m.start() for m in re.finditer(r"^## Found[ \t]*$", text, re.M)]
    return text[start[-1]:] if start else None


def is_hit(found, case):
    for path, first, last in re.findall(r"([\w./-]+):(\d+)(?:-(\d+))?", found):
        if path == case["file"] or path.endswith("/" + case["file"]):
            if int(first) - NEAR <= case["line"] <= int(last or first) + NEAR:
                return True
    return False


def review(case, model):
    """(result, important, error) for one case; the worktree goes even when the review fails."""
    if not os.path.isdir(case["checkout"]):
        return "skipped", 0, f"no checkout at {case['checkout']}"
    worktree = tempfile.mkdtemp(prefix="review-eval-")
    os.rmdir(worktree)  # git worktree add wants to create it
    added = git(case["checkout"], "worktree", "add", "--detach", "--quiet", worktree, case["sha"])
    if added.returncode:
        return "error", 0, f"git worktree add: {added.stderr.strip()[-300:]}"
    try:
        bundle = subprocess.run([BUNDLE, case["base"], case["lens"], case["goal"]], cwd=worktree, capture_output=True,
                                text=True, timeout=120)
        if bundle.returncode:
            return "error", 0, f"bundle.sh exit {bundle.returncode}: {bundle.stderr.strip()[-300:]}"
        out = subprocess.run(MODELS[model], input=bundle.stdout, cwd=worktree, capture_output=True, text=True,
                             timeout=REVIEW_SECS)
    except subprocess.TimeoutExpired as expired:
        return "error", 0, f"{expired.cmd[0]} took over {expired.timeout}s"
    except OSError as error:
        return "error", 0, f"{error}"
    finally:
        removed = git(case["checkout"], "worktree", "remove", "--force", worktree)
        if removed.returncode:
            print(f"eval.py: couldn't remove the worktree {worktree}: {removed.stderr.strip()}", file=sys.stderr)
    found = found_section(out.stdout) if out.returncode == 0 else None
    if found is None:
        return "error", 0, f"{model} exit {out.returncode}, no ## Found section: {out.stderr.strip()[-300:]}"
    important = len(re.findall(r"^.*\bImportant\b.*$", found, re.M))
    if case["kind"] == "clean":
        return "clean", important, None
    return ("hit" if is_hit(found, case) else "miss"), important, None


def summary(rows):
    """Per lens: recall over bug cases, Important findings per clean case, errors and skips."""
    lines = {}
    for lens in sorted({row["lens"] for row in rows}):
        mine = [row for row in rows if row["lens"] == lens]
        bugs = [row for row in mine if row["result"] in ("hit", "miss")]
        cleans = [row for row in mine if row["result"] == "clean"]
        hits = sum(row["result"] == "hit" for row in bugs)
        recall = f"recall {hits}/{len(bugs)}" if bugs else "recall n/a"
        alarms = f"{sum(r['important'] for r in cleans) / len(cleans):.1f} Important per clean case" if cleans \
            else "no clean case"
        errors = sum(row["result"] == "error" for row in mine)
        skipped = sum(row["result"] == "skipped" for row in mine)
        lines[lens] = f"{recall}, {alarms}, {errors} errors, {skipped} skipped"
    return lines


def newest(cases, limit):
    """The newest `limit` bug cases and the newest `limit` clean ones, so a run always measures both."""
    if not limit:
        return cases
    keep = {id(case) for kind in ("bug", "clean") for case in [c for c in cases if c["kind"] == kind][-limit:]}
    return [case for case in cases if id(case) in keep]


def run(args):
    cases = newest(read_cases(), args.limit)
    if not cases:
        print(f"eval.py: no cases in {CASES}; add them with `eval.py add`")
        return 0
    os.makedirs(RUNS, exist_ok=True)
    previous = sorted(f for f in os.listdir(RUNS) if f.endswith(f"-{args.model}.jsonl"))
    rows = []
    for case in cases:
        started = time.time()
        result, important, error = review(case, args.model)
        rows.append({"sha": case["sha"], "lens": case["lens"], "file": case.get("file"), "line": case.get("line"),
                     "kind": case["kind"], "result": result, "important": important, "error": error,
                     "secs": round(time.time() - started)})
        if error:
            print(f"eval.py: {case['sha'][:12]} {case['lens']}: {result}: {error}", file=sys.stderr)
    name = f"{datetime.datetime.now().strftime('%Y-%m-%dT%H%M%S.%f')}-{args.model}.jsonl"
    with open(os.path.join(RUNS, name), "w") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in rows)
    before, compared = {}, "no previous run"
    if previous:
        with open(os.path.join(RUNS, previous[-1])) as fh:
            earlier = [json.loads(line) for line in fh if line.strip()]
        before = summary(earlier)
        same = sorted(map(case_key, earlier)) == sorted(map(case_key, rows))
        compared = "same cases as the previous run" if same else \
            "the cases differ from the previous run's, so the numbers aren't like for like"
    print(f"Review evals, {args.model}, {len(rows)} cases, {compared} (results in {os.path.join(RUNS, name)}):")
    for lens, now in summary(rows).items():
        print(f"- {lens}: {now}" + (f" (previous run: {before[lens]})" if lens in before else " (no previous run)"))
    return 0


def main():
    parser = argparse.ArgumentParser(prog="eval.py", allow_abbrev=False, description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    add_cmd = commands.add_parser("add", allow_abbrev=False)
    add_cmd.add_argument("--checkout", required=True)
    add_cmd.add_argument("--sha", required=True)
    add_cmd.add_argument("--base", required=True)
    add_cmd.add_argument("--lens", required=True)
    add_cmd.add_argument("--goal", required=True)
    add_cmd.add_argument("--file")
    add_cmd.add_argument("--line", type=int)
    add_cmd.add_argument("--summary")
    add_cmd.add_argument("--clean", action="store_true")
    run_cmd = commands.add_parser("run", allow_abbrev=False)
    run_cmd.add_argument("--model", choices=tuple(MODELS), required=True)
    run_cmd.add_argument("--limit", type=int, help="the newest N bug cases and the newest N clean ones")
    args = parser.parse_args()
    return add(args, add_cmd) if args.command == "add" else run(args)


if __name__ == "__main__":
    sys.exit(main())
