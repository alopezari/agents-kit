#!/usr/bin/env python3
"""Deterministic checks on the lines a change adds, for the per-repo `verify` scripts.

Only findings on added or modified lines count, so touching a file with old
problems doesn't block the agent. Exit 1 when an error-level finding lands on
a changed line; warnings are printed but don't fail.

Usage (from a repo's verify script):
  verify_changed.py [--semgrep RULES.yml ...] [--phpcs "CMD" ] [--phpstan "CMD"] [--cwd SUBDIR]
                    [--phpunit "CMD" --baseline FILE] [--refresh-baseline]
PHPCS/PHPStan commands get the changed PHP files appended and must emit JSON.
PHPUnit runs the unit suite, or with --related-tests only the tests related to the change,
and fails only on tests that aren't already failing in the baseline, so pre-existing
failures never block. --red-check proves changed tests fail without the change.
"""
import argparse
import shutil
import tempfile
import xml.etree.ElementTree as ET
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "hooks"))
import review_stamp  # noqa: E402


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def changed_lines(root):
    """{path: set(line numbers)} for lines added or modified since the branch left the default branch
    (committed or not), plus whole untracked files: what the pull request will contain."""
    result = {}
    path = None
    base = review_stamp.merge_base(root)
    for line in git(["diff", base, "--unified=0", "--no-color", "--diff-filter=AMR"], root).splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and line.startswith("@@"):
            m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
            start, count = int(m.group(1)), int(m.group(2) or 1)
            result.setdefault(path, set()).update(range(start, start + count))
    for path in git(["ls-files", "--others", "--exclude-standard"], root).splitlines():
        result[path] = None  # None means every line is new
    return result


def on_changed_line(changes, path, line):
    lines = changes.get(path, set())
    return lines is None or line in lines


def execute(args, cwd=None, timeout=600):
    """Run a tool; a missing binary or a timeout comes back as a failed run, so callers report it as NOT RUN."""
    try:
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(args, 127, "", f"{e.filename or args[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 124, "", f"{args[0]}: timed out after {timeout}s")


def run_json(cmd, cwd, not_run):
    proc = execute(cmd, cwd)
    stdout = proc.stdout or ""
    try:
        # PHP deprecation notices can surround the JSON on stdout; decode the first object only.
        data = json.JSONDecoder().raw_decode(stdout[stdout.index("{"):])[0]
        if "files" in data or "results" in data:
            return data
    except ValueError:
        pass
    # A tool that didn't run must not look like a clean pass.
    not_run.append(f"{os.path.basename(cmd[0])} did not run, so its checks are NOT RUN; fix the setup or say so to the user:\n"
                   f"{(proc.stdout + proc.stderr).strip()[-600:]}")
    return {}


def failing_tests(cmd, cwd, not_run, junit_host=None, test_filter=""):
    """Run PHPUnit with a JUnit log; return the set of failing test ids, or None if it didn't run.

    Local commands get `--log-junit <tmp>` appended. Containerized commands write the log
    themselves to a bind-mounted path (`junit_host` is where it appears on this machine),
    and may carry a `{filter}` placeholder for running only related tests.
    """
    with tempfile.NamedTemporaryFile(suffix=".xml") as tmp:
        args = shlex.split(cmd.replace("{filter}", test_filter))
        junit = junit_host or tmp.name
        if not junit_host:
            args += ["--log-junit", junit]
        elif os.path.exists(junit):
            os.remove(junit)
        proc = execute(args, cwd, timeout=480)
        try:
            root = ET.parse(junit).getroot()
        except (ET.ParseError, OSError):
            not_run.append(f"phpunit did not run, so unit tests are NOT RUN:\n{(proc.stdout + proc.stderr).strip()[-600:]}")
            return None
    return {f"{tc.get('class') or tc.get('classname')}::{tc.get('name')}"
            for tc in root.iter("testcase") if tc.find("failure") is not None or tc.find("error") is not None}


TEST_FILE = re.compile(r"Test\.php$")


def link_ignored_dirs(root, tmp):
    """Mirror every git-ignored directory (vendor/, node_modules/, build output) into the pristine copy.

    Composer's autoload files and bin proxies resolve paths from their own location, so inside each
    vendor/ they are copied and only the (unchanged) third-party packages are symlinked.
    """
    ignored = git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], root).splitlines()
    for rel in (d.rstrip("/") for d in ignored if d.endswith("/")):
        src, dst = os.path.join(root, rel), os.path.join(tmp, rel)
        if os.path.lexists(dst) or not os.path.isdir(src):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.basename(rel) != "vendor":
            os.symlink(src, dst)
            continue
        os.makedirs(dst)
        for entry in os.listdir(src):
            if entry in ("composer", "autoload.php", "bin"):
                copy = shutil.copytree if os.path.isdir(os.path.join(src, entry)) else shutil.copy2
                copy(os.path.join(src, entry), os.path.join(dst, entry), symlinks=True) if copy is shutil.copytree \
                    else copy(os.path.join(src, entry), os.path.join(dst, entry))
            else:
                os.symlink(os.path.join(src, entry), os.path.join(dst, entry))


def red_check(root, cwd_rel, cmd, changed_tests, not_run):
    """Run the changed tests against HEAD without the change: at least one must fail.

    Builds a pristine copy of the merge-base with `git archive`, symlinks untracked dependency dirs
    (vendor/...) from the working tree, copies in the changed test files, and runs only those
    test classes. Returns an error message when every test passes without the change.
    """
    tmp = tempfile.mkdtemp(prefix="agents-red-")
    try:
        archive = subprocess.Popen(["git", "archive", review_stamp.merge_base(root)], cwd=root, stdout=subprocess.PIPE)
        subprocess.run(["tar", "-x", "-C", tmp], stdin=archive.stdout, check=True)
        archive.wait()
        link_ignored_dirs(root, tmp)
        for test in changed_tests:
            os.makedirs(os.path.dirname(os.path.join(tmp, test)), exist_ok=True)
            shutil.copy2(os.path.join(root, test), os.path.join(tmp, test))
        classes = sorted({os.path.splitext(os.path.basename(t))[0] for t in changed_tests})
        junit = os.path.join(tmp, "agents-red-junit.xml")
        args = shlex.split(cmd) + ["--filter", "/(" + "|".join(classes) + ")/", "--log-junit", junit]
        proc = execute(args, os.path.join(tmp, cwd_rel))
        try:
            cases = list(ET.parse(junit).getroot().iter("testcase"))
        except (ET.ParseError, OSError):
            # No report means PHPUnit died before running tests: a setup problem, not evidence either way.
            not_run.append(f"red check did not run (PHPUnit failed to start in the pristine copy):\n"
                           f"{(proc.stdout + proc.stderr).strip()[:600]}")
            return None
        failed = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
        if cases and not failed:
            return (f"[red check] The changed tests ({', '.join(classes)}) all pass without your change (on the merge-base), "
                    "so they don't test it. Make at least one of them fail without the change, or say why "
                    "they intentionally pin existing behavior.")
        return None
    except (OSError, subprocess.SubprocessError) as err:
        not_run.append(f"red check did not run: {err}")
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def container_running(name):
    out = subprocess.run(["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
                         capture_output=True, text=True).stdout
    return name in out.split()


def related_test_classes(root, test_dir, changed_php):
    """Test classes to run for a diff: changed test files plus tests that mention a changed class."""
    tests = [os.path.join(d, f) for d, _, fs in os.walk(test_dir) for f in fs if f.endswith("Test.php")]
    changed = {os.path.join(root, p) for p in changed_php}
    names = {os.path.splitext(os.path.basename(p))[0] for p in changed_php if not p.endswith("Test.php")}
    related = set()
    for t in tests:
        if t in changed:
            related.add(os.path.splitext(os.path.basename(t))[0])
            continue
        try:
            text = open(t, errors="ignore").read()
        except OSError:
            continue
        if any(re.search(rf"\b{re.escape(n)}\b", text) for n in names):
            related.add(os.path.splitext(os.path.basename(t))[0])
    return related


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--semgrep", action="append", default=[], help="semgrep rules file; repeat for several")
    ap.add_argument("--phpcs", help="PHPCS command emitting JSON; changed PHP files are appended")
    ap.add_argument("--phpstan", help="PHPStan command emitting JSON; changed PHP files are appended")
    ap.add_argument("--cwd", default=".", help="subdirectory the PHPCS/PHPStan/PHPUnit commands run from")
    ap.add_argument("--start-hint", help="copy-paste command that starts the --requires-container stack")
    ap.add_argument("--phpunit", help="unit-suite command that runs locally without Docker or network")
    ap.add_argument("--baseline", help="file listing test ids already failing on a clean checkout")
    ap.add_argument("--refresh-baseline", action="store_true", help="record current failures (clean tree only)")
    ap.add_argument("--phpunit-cwd", help="where to run the PHPUnit command (default: --cwd)")
    ap.add_argument("--junit-host", help="repo-relative path where a containerized run writes its JUnit log")
    ap.add_argument("--related-tests", help="test dir (relative to --cwd): run only tests related to the diff via {filter}")
    ap.add_argument("--requires-container", help="container that must be running for PHPUnit")
    ap.add_argument("--php-min", help="lint changed PHP files on this minimum version (e.g. 7.4) in a php:<v>-cli container")
    ap.add_argument("--red-check", action="store_true",
                    help="changed tests must fail without the change, on the merge-base (local PHPUnit only)")
    args = ap.parse_args()

    root = git(["rev-parse", "--show-toplevel"], os.getcwd()).strip()
    if args.refresh_baseline:
        if git(["status", "--porcelain"], root).strip():
            print("refusing to refresh the baseline: the working tree has changes")
            return 1
        phpunit_cwd = os.path.join(root, args.phpunit_cwd or args.cwd)
        junit_host = os.path.join(root, args.junit_host) if args.junit_host else None
        failing = failing_tests(args.phpunit, phpunit_cwd, [], junit_host) or set()
        with open(os.path.expanduser(args.baseline), "w") as fh:
            fh.write("\n".join(sorted(failing)) + "\n")
        print(f"baseline: {len(failing)} failing tests recorded")
        return 0
    changes = changed_lines(root)
    php = [p for p in changes if p.endswith(".php") and os.path.isfile(os.path.join(root, p))]
    if not php:
        print("ran: nothing to check: no changed PHP files")
        return 0
    errors, warnings, not_run = [], [], []
    ran = []  # Evidence for the report: what actually ran, not only what failed.

    for rules in args.semgrep:
        out = run_json(["semgrep", "--config", os.path.expanduser(rules), "--metrics=off",
                        "--disable-version-check", "--quiet", "--json", *php], root, not_run)
        for r in out.get("results", []):
            if on_changed_line(changes, r["path"], r["start"]["line"]):
                msg = f"{r['path']}:{r['start']['line']} [{r['check_id'].split('.')[-1]}] {r['extra']['message'].strip()}"
                (errors if r["extra"]["severity"] == "ERROR" else warnings).append(msg)
        if "errors" in out or "results" in out:
            ran.append(f"semgrep {os.path.basename(rules)} on {len(php)} changed PHP files")

    tool_cwd = os.path.join(root, args.cwd)
    rel = [os.path.relpath(os.path.join(root, p), tool_cwd) for p in php]

    if args.phpcs:
        out = run_json(shlex.split(args.phpcs) + ["--report=json", "-q", *rel], tool_cwd, not_run)
        for fpath, data in (out.get("files") or {}).items():
            repo_path = os.path.relpath(os.path.realpath(fpath if os.path.isabs(fpath) else os.path.join(tool_cwd, fpath)), os.path.realpath(root))
            for m in data.get("messages", []):
                if on_changed_line(changes, repo_path, m["line"]):
                    msg = f"{repo_path}:{m['line']} [phpcs {m['source']}] {m['message']}"
                    (errors if m["type"] == "ERROR" else warnings).append(msg)
        if out:
            ran.append(f"phpcs on {len(rel)} files (changed lines only)")

    if args.phpstan:
        out = run_json(shlex.split(args.phpstan) + ["--error-format=json", "--no-progress", *rel], tool_cwd, not_run)
        for fpath, data in (out.get("files") or {}).items():
            repo_path = os.path.relpath(os.path.realpath(fpath), os.path.realpath(root))
            for m in data.get("messages", []):
                if on_changed_line(changes, repo_path, m.get("line") or 0):
                    errors.append(f"{repo_path}:{m.get('line')} [phpstan] {m['message']}")
        if out:
            ran.append(f"phpstan on {len(rel)} files (changed lines only)")

    if args.phpunit:
        phpunit_cwd = os.path.join(root, args.phpunit_cwd) if args.phpunit_cwd else tool_cwd
        junit_host = os.path.join(root, args.junit_host) if args.junit_host else None
        test_filter, run_tests = "", True
        if args.related_tests:
            classes = related_test_classes(root, os.path.join(tool_cwd, args.related_tests), php)
            run_tests = bool(classes)
            test_filter = "--filter " + shlex.quote("/(" + "|".join(sorted(classes)) + ")/")
        if run_tests and args.requires_container and not container_running(args.requires_container):
            start = (f"Start it with:\n    cd {shlex.quote(root)} && {args.start_hint}\nthen stop again."
                     if args.start_hint else "Start the repo's local stack (see the repo notes) and stop again.")
            not_run.append(f"container {args.requires_container} isn't running, so unit tests are NOT RUN. {start}")
            run_tests = False
        failing = failing_tests(args.phpunit, phpunit_cwd, not_run, junit_host, test_filter) if run_tests else None
        if failing is not None:
            try:
                known = set(open(os.path.expanduser(args.baseline)).read().split("\n")) if args.baseline else set()
            except OSError:
                known = set()
            ran.append(f"phpunit {test_filter or '(full suite)'}: {len(failing)} failing, "
                       f"{len(failing - known)} not in the baseline")
            for test in sorted(failing - known)[:30]:
                errors.append(f"[phpunit] {test} fails and isn't in the baseline. If it also fails on a clean "
                              f"trunk checkout, say so; the baseline is refreshed with --refresh-baseline.")

    if args.php_min:
        image = f"php:{args.php_min}-cli"
        have = execute(["docker", "image", "inspect", image]).returncode == 0
        if not have:
            have = execute(["docker", "pull", "-q", image], timeout=300).returncode == 0
        if not have:
            not_run.append(f"PHP {args.php_min} syntax check did not run: couldn't get the {image} image (is Docker running?)")
        else:
            script = 'for f in "$@"; do out=$(php -l "$f" 2>&1) || echo "$f: $out"; done'
            proc = subprocess.run(["docker", "run", "--rm", "-v", f"{root}:/app", "-w", "/app", image,
                                   "sh", "-c", script, "lint", *php], capture_output=True, text=True, timeout=300)
            for line in proc.stdout.splitlines():
                if "error" in line.lower():
                    errors.append(f"[php {args.php_min}] {line.strip()}")
            ran.append(f"php -l under PHP {args.php_min} on {len(php)} files")

    if args.red_check and args.phpunit and not not_run:
        tests = [p for p in php if TEST_FILE.search(p)]
        if tests and any(not TEST_FILE.search(p) and "/tests/" not in p for p in php):
            problem = red_check(root, args.cwd, args.phpunit, tests, not_run)
            if problem:
                errors.append(problem)
            elif not not_run:
                ran.append(f"red check: {len(tests)} changed test files fail before the change (merge-base), pass with it")

    for r in ran:
        print(f"ran: {r}")
    for w in warnings[:30]:
        print(f"warning: {w}")
    for e in errors[:30]:
        print(f"error: {e}")
    for n in not_run:
        print(f"error: {n}")
    return 1 if errors or not_run else 0


if __name__ == "__main__":
    sys.exit(main())
