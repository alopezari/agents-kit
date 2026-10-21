#!/usr/bin/env python3
"""Replay bugs that got past the review against its reviewers, and clean changes for their false alarms.

  eval.py add --checkout DIR --sha SHA --base REF --lens LENS --goal "..." --file PATH --line N --summary "..."
  eval.py add --checkout DIR --sha SHA --base REF --lens LENS --goal "..." --clean
  eval.py run --model claude|codex [--limit N]

A bug case is a commit that held the bug, the base it was reviewed against and where the bug was. `run` reviews the
newest N bug and N clean cases as a cross-model first pass does (bundle.sh piped to the model, in a detached worktree
of the commit): a bug case is a hit when the review's `## Found` names its file within 15 lines of the bug, a match on
place, not on reason; a clean case counts the Found lines that say Important. Cases and results hold repo paths and
bug summaries, so they stay in review-mining/evals/, which git ignores.
"""
import argparse
import collections
import datetime
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time

EVALS = os.path.join(os.path.expanduser("~/.agents"), "review-mining", "evals")
CASES = os.path.join(EVALS, "cases.jsonl")
RUNS = os.path.join(EVALS, "runs")
BUNDLE = os.path.expanduser("~/.agents/skills/kit-review/bundle.sh")
LENSES = os.path.expanduser("~/.agents/skills/kit-review/lenses.md")
NEAR = 15  # a reviewer names the line its finding starts on, often the function above the faulty line
REVIEW_SECS = 15 * 60
BUNDLE_SECS = GIT_SECS = 120
# claude gets the review's cross-model tools; dontAsk refuses the rest instead of falling back to the user's
# permission mode, and nothing from a replay lands among the sessions the monthly job counts.
MODELS = {
    "codex": ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-s", "read-only", "-"],
    "claude": ["claude", "-p", "--strict-mcp-config", "--no-session-persistence", "--tools", "Read,Grep,Glob,Bash",
               "--permission-mode", "dontAsk",
               "--allowedTools", "Read,Grep,Glob,Bash(git diff:*),Bash(git log:*),Bash(git show:*)"],
}


def git(checkout, *args):
    return subprocess.run(["git", "-C", checkout, *args], capture_output=True, text=True, errors="replace")


def bounded(cmd, secs, cwd, stdin=""):
    """subprocess.run whose timeout kills the whole process group: codex is a node shim around the real binary, which
    would otherwise go on reviewing a worktree that's been removed."""
    proc = subprocess.Popen(cmd, cwd=cwd, start_new_session=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, errors="replace")
    try:
        out, err = proc.communicate(stdin, timeout=secs)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


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
    for name in ("sha", "base"):  # in the checkout given: HEAD there isn't the main checkout's HEAD
        out = git(args.checkout, "rev-parse", "--verify", "--quiet", getattr(args, name) + "^{commit}")
        if out.returncode:
            parser.error(f"--{name} {getattr(args, name)!r} isn't a commit in {checkout}")
        commits[name] = out.stdout.strip()
    bug = [value is not None for value in (args.file, args.line, args.summary)]
    if (args.clean and any(bug)) or (not args.clean and not all(bug)):
        parser.error("give either --clean, or all of --file, --line and --summary")
    if git(checkout, "merge-base", "--is-ancestor", commits["base"], commits["sha"]).returncode \
            or commits["base"] == commits["sha"]:
        parser.error(f"--base {args.base!r} isn't an earlier commit of {args.sha}, so there's no change to review")
    if args.file is not None:
        args.file = os.path.normpath(args.file) if args.file else ""
        if git(checkout, "cat-file", "-t", f"{commits['sha']}:{args.file}").stdout.strip() != "blob" \
                or args.file.startswith(("..", "/")) or args.file == ".":
            parser.error(f"--file {args.file!r} isn't a file in {args.sha}")
        content = git(checkout, "show", f"{commits['sha']}:{args.file}")
        if not 1 <= args.line <= len(content.stdout.splitlines()):
            parser.error(f"--line {args.line} isn't a line of {args.file} at {args.sha}")
    case = {"added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "clean" if args.clean else "bug",
            "checkout": checkout, "sha": commits["sha"], "base": commits["base"], "lens": lens, "goal": args.goal}
    if not args.clean:
        case.update(file=args.file, line=args.line, summary=args.summary)
    # Pinned, or gc prunes the commit once a squash merge deletes its branch; the base is an ancestor, so kept too.
    pinned = git(checkout, "update-ref", f"refs/review-evals/{commits['sha']}", commits["sha"])
    if pinned.returncode:
        print(f"eval.py: couldn't pin {commits['sha']} in {checkout}: {pinned.stderr.strip()}", file=sys.stderr)
        return 1
    os.makedirs(EVALS, exist_ok=True)
    with open(CASES, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)  # two follow-pr sessions adding the same case write it once
        fh.seek(0)
        if any(line.strip() and case_key(json.loads(line)) == case_key(case) for line in fh):
            print(f"eval.py: that case is already in {CASES}")
            return 0
        fh.write(json.dumps(case) + "\n")
    print(f"eval.py: added a {case['kind']} case to {CASES}")
    return 0


def found_section(text):
    """The last `## Found` heading to the end, as the review reads a cross-model pass."""
    start = [m.start() for m in re.finditer(r"^## Found[ \t]*$", text, re.M)]
    return text[start[-1]:] if start else None


def is_hit(found, case):
    """Whether Found names the case's file, or a longer path ending in it, within NEAR lines of the bug."""
    place = re.compile(r"(?<![\w.-])" + re.escape(case["file"]) + r":(\d+)(?:-(\d+))?")
    return any(int(first) - NEAR <= case["line"] <= int(last or first) + NEAR
               for first, last in place.findall(found))


def replay(case, model, worktree):
    """(the `## Found` section, None) of one review of the case's change, or (None, the error)."""
    added = bounded(["git", "-C", case["checkout"], "worktree", "add", "--detach", "--quiet", worktree, case["sha"]],
                    GIT_SECS, case["checkout"])
    if added.returncode:
        return None, f"git worktree add: {added.stderr.strip()[-300:]}"
    bundle = bounded([BUNDLE, case["base"], case["lens"], case["goal"]], BUNDLE_SECS, worktree)
    if bundle.returncode:
        return None, f"bundle.sh exit {bundle.returncode}: {bundle.stderr.strip()[-300:]}"
    out = bounded(MODELS[model], REVIEW_SECS, worktree, bundle.stdout)
    found = found_section(out.stdout) if out.returncode == 0 else None
    if found is None:
        said = " / ".join(text.strip()[-300:] for text in (out.stderr, out.stdout) if text.strip())
        return None, f"{model} exit {out.returncode}, no ## Found section: {said}"
    return found, None


def review(case, model):
    """replay() in a worktree that goes even when the review fails; one that can't be removed is an error too, since
    a run that leaves them behind fills the disk."""
    worktree = tempfile.mkdtemp(prefix="review-eval-")
    os.rmdir(worktree)  # git worktree add wants to create it
    found, error = None, None
    try:
        found, error = replay(case, model, worktree)
    except subprocess.TimeoutExpired as expired:
        error = f"{expired.cmd[0]} took over {expired.timeout}s"
    except OSError as failure:
        error = str(failure)
    finally:
        if os.path.isdir(worktree):
            try:
                removed = bounded(["git", "-C", case["checkout"], "worktree", "remove", "--force", worktree], GIT_SECS,
                                  case["checkout"])
                failed = (removed.stderr.strip() or f"exit {removed.returncode}") if removed.returncode else None
            except subprocess.TimeoutExpired:
                failed = f"took over {GIT_SECS}s"
            if failed:
                found, error = None, error or f"couldn't remove the worktree {worktree}: {failed}"
    return found, error


def score(case, found):
    important = len(re.findall(r"^.*\bImportant\b.*$", found, re.M))
    if case["kind"] == "clean":
        return "clean", important
    return ("hit" if is_hit(found, case) else "miss"), important


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
    bugs = [case for case in cases if case["kind"] == "bug"][-limit:]
    cleans = [case for case in cases if case["kind"] == "clean"][-limit:]
    return [case for case in cases if any(case is kept for kept in bugs + cleans)]


def run(args):
    cases = newest(read_cases(), args.limit)
    if not cases:
        print(f"eval.py: no cases in {CASES}; add them with `eval.py add`")
        return 0
    os.makedirs(RUNS, exist_ok=True)
    previous = sorted(f for f in os.listdir(RUNS) if f.endswith(f"-{args.model}.jsonl"))
    kit_dir = os.path.dirname(os.path.realpath(__file__))
    kit = bounded(["git", "-C", kit_dir, "describe", "--always", "--dirty"], GIT_SECS, kit_dir).stdout.strip() or None
    rows, reviews = [], {}
    for case in cases:
        started = time.time()
        if not os.path.isdir(case["checkout"]):
            result, important, error = "skipped", 0, f"no checkout at {case['checkout']}"
        elif lens_heading(case["lens"]) != case["lens"]:
            result, important, error = "error", 0, f"{case['lens']!r} is no longer a lens heading in {LENSES}"
        else:
            # follow-pr records each confirmed comment on a PR as a case of the same change: one review scores them all.
            change = (case["checkout"], case["sha"], case["base"], case["lens"], case["goal"])
            if change not in reviews:
                reviews[change] = review(case, args.model)
            found, error = reviews[change]
            result, important = score(case, found) if found is not None else ("error", 0)
        rows.append({"kit": kit, "sha": case["sha"], "lens": case["lens"], "file": case.get("file"), "line": case.get("line"),
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
        same = collections.Counter(map(case_key, earlier)) == collections.Counter(map(case_key, rows))
        compared = "same cases as the previous run" if same else \
            "the cases differ from the previous run's, so the numbers aren't like for like"
    print(f"Review evals, {args.model}, {len(rows)} cases, {compared} (results in {os.path.join(RUNS, name)}):")
    now = summary(rows)
    for lens in sorted(now.keys() | before.keys()):
        missing = "no previous run" if not previous else "not in the previous run"
        print(f"- {lens}: {now.get(lens, 'no cases this run')}" +
              (f" (previous run: {before[lens]})" if lens in before else f" ({missing})"))
    return 0


def at_least_one(value):
    if not value.lstrip("-").isdigit() or int(value) < 1:
        raise argparse.ArgumentTypeError(f"{value!r} isn't a whole number at least 1")
    return int(value)


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
    run_cmd.add_argument("--limit", type=at_least_one, help="the newest N bug cases and the newest N clean ones")
    args = parser.parse_args()
    return add(args, add_cmd) if args.command == "add" else run(args)


if __name__ == "__main__":
    sys.exit(main())
