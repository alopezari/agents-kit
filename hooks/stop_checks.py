#!/usr/bin/env python3
"""Stop hook shared by Claude Code, Codex and Pi (via adapters/pi).

Only runs when the session edited files since the last stop. Looks at the lines the
branch adds since the merge-base with the default branch (committed or not) and asks
the agent to continue, once, for a skipped or focused test, a deleted test file, a debug
leftover, a conflict marker, a possible secret, a new option read near a cache, a
temporary compose override left behind, or a changed code file the spec's Change map
doesn't name. Then runs the repo's verify: the overlay in ~/.agents/repos/<repo-name>/verify
when it exists, else repos/_shared/verify_auto.py.

`stop_checks.py leftover-overrides` prints the marked overrides still present in every checkout
the hook has seen, for the weekly health check.
"""
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import log  # noqa: E402
import review_stamp  # noqa: E402

MARKER_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks")
CHECKOUTS = os.path.join(os.environ.get("AGENTS_STATE_DIR") or os.path.expanduser("~/.agents/monitors/state"), "checkouts.txt")
OVERRIDE_NAMES = ("docker-compose.override.yml", "docker-compose.override.yaml", "compose.override.yml", "compose.override.yaml")
OVERRIDE_MARKER = "agents: temporary override"
VERIFY_TIMEOUT = 600
AUTO_VERIFY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repos", "_shared", "verify_auto.py"))

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|[._-](test|spec)\.[a-z]+$|Test\.php$", re.I)
WEAKENED_TEST = re.compile(
    r"\b(it|test|describe)\.(skip|only)\(|\bx(it|describe|test)\(|markTest(Skipped|Incomplete)\("
    r"|@pytest\.mark\.skip|\bt\.Skip\(|@Disabled\b|@group\s+(skip|ignore)"
)
DEBUG_LEFTOVER = re.compile(r"\bvar_dump\(|\bdebugger;|^\s*dd\(|\bbinding\.pry\b|\bbreakpoint\(\)")
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)")


def git(args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def added_lines(root):
    """Yield (path, line) for lines added since the branch left the default branch (committed or not),
    including untracked files: a leftover committed mid-session still reaches the pull request."""
    diff = git(["diff", review_stamp.merge_base(root), "--unified=0", "--no-color"], root)
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
        elif path and line.startswith("+") and not line.startswith("+++"):
            yield path, line[1:]
    for path in git(["ls-files", "--others", "--exclude-standard"], root).splitlines():
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    yield path, line.rstrip("\n")
        except OSError:
            continue


OPTION_NAME = re.compile(r"\b(?:get_option|update_option|add_option|register_setting)\(\s*['\"]([\w-]+)['\"]")
CACHE_WRITE = r"set_transient|set_site_transient|wp_cache_set|wp_cache_add"


def new_options_near_caches(root, lines):
    """New WordPress option names whose plugin also writes caches: the classic 'cache key forgot the setting' bug."""
    problems = []
    seen = set()
    for path, line in lines:
        for name in OPTION_NAME.findall(line):
            if name in seen:
                continue
            seen.add(name)
            existed = subprocess.run(["git", "grep", "-q", "-F", name, "HEAD", "--"], cwd=root).returncode == 0
            if existed:
                continue
            module = os.path.dirname(path) or "."
            cache_files = git(["grep", "-l", "-E", CACHE_WRITE, "--", module], root).split()
            repo_wide = len(git(["grep", "-l", "-E", CACHE_WRITE], root).split())
            if repo_wide:
                problems.append(
                    f"New option `{name}` ({path}). The repo writes caches in {repo_wide} files"
                    + (f", including {', '.join(cache_files[:5])} next to it" if cache_files else "")
                    + ". Confirm every cache whose output depends on this option varies with it or is invalidated"
                    " when it changes (self-review Correctness lens, derived-data table)."
                )
    return problems


def leaked_secrets(lines):
    """Secrets in added lines, via gitleaks (redacted). Empty when gitleaks isn't installed."""
    text = "\n".join(f"{path}: {line}" for path, line in lines)
    if not text:
        return []
    try:
        proc = subprocess.run(
            ["gitleaks", "stdin", "--no-banner", "--redact", "--report-format", "json", "--report-path", "-",
             "--log-level", "error", "--exit-code", "0"],
            input=text, capture_output=True, text=True, timeout=60)
        findings = json.loads(proc.stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    out = []
    for f in findings:
        where = (f.get("Match") or "").split(": ", 1)[0] or "diff"
        out.append(f"Possible secret ({f.get('RuleID')}) added in {where}. Remove it and load it from the "
                   "environment or a secret store; if it's a false positive, add `gitleaks:allow` on that line.")
    return out


def record_verify_streak(session, failed):
    """Consecutive verify failures in this session, stored next to the edit marker."""
    path = os.path.join(MARKER_DIR, f"{session}.verify-fails")
    try:
        count = int(open(path).read()) if failed else 0
    except (OSError, ValueError):
        count = 0
    count = count + 1 if failed else 0
    os.makedirs(MARKER_DIR, exist_ok=True)
    with open(path, "w") as fh:
        fh.write(str(count))
    return count


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    if payload.get("stop_hook_active"):
        return 0
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "unknown"))
    marker = os.path.join(MARKER_DIR, f"{session}.edited")
    if not os.path.exists(marker):
        return 0
    edited = [p for p in open(marker).read().splitlines() if p]
    os.remove(marker)

    # Check every checkout the session touched (worktrees included), not only the session's cwd.
    cwd = payload.get("cwd") or os.getcwd()
    roots = []
    for path in edited or [cwd]:
        start = path if os.path.isdir(path) else os.path.dirname(path)
        root = git(["rev-parse", "--show-toplevel"], start).strip() if os.path.isdir(start) else ""
        if root and root not in roots:
            roots.append(root)
    problems, any_verify_failed = [], False
    for root in roots:
        found, failed_verify = check_checkout(root, session)
        problems += [f"[{review_stamp.repo_name(root)}] {p}" if len(roots) > 1 else p for p in found]
        any_verify_failed = any_verify_failed or failed_verify
    streak = record_verify_streak(session, any_verify_failed)

    if problems:
        reason = (
            "Before finishing, resolve these or explain to the user why they are intended. If you can't resolve one "
            "honestly, report it under **Blocked on me** with what you tried; never work around a check "
            "(skipping or loosening tests, deleting files, disabling a rule, editing the hook):\n- "
            + "\n- ".join(problems[:20])
        )
        output = {"decision": "block", "reason": reason}
        for problem in problems[:20]:
            log("stop_checks", "block", payload, problem)
        if streak >= 2:
            # Hooks can't change effort; on Opus 5.5 raising it mid-session keeps the cache, so nudge the user.
            output["systemMessage"] = (f"Verification has failed {streak} times in a row in this session. "
                                       "Consider /effort high (or xhigh) for this task.")
            log("stop_checks", "effort-nudge", payload, f"verify failed {streak} times in a row")
        print(json.dumps(output))
    return 0


def unmapped_files(root, session):
    """Changed code files the branch's spec has no Change map entry for, each returned once per session."""
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root).strip()
    name = f"spec-{review_stamp.repo_name(root)}-{review_stamp.branch_key(git(['branch', '--show-current'], root).strip())}.md"
    for spec in (os.path.join(common, "agents", name), os.path.join(review_stamp.TMP_SPECS, name)):
        if os.path.isfile(spec):
            break
    else:
        return []
    change_map = re.search(r"^## Change map\s*$(.*?)(?=^## |\Z)", open(spec, errors="ignore").read(), re.M | re.S | re.I)
    if not change_map:
        return []
    base = review_stamp.merge_base(root)
    changed = set(git(["diff", "--name-only", base], root).splitlines())
    changed |= set(git(["ls-files", "--others", "--exclude-standard"], root).splitlines())
    # A path, not a bare file name: `app.py` in the map doesn't cover `other/app.py`.
    unmapped = sorted(p for p in changed if p and not review_stamp.NOT_BEHAVIOR.search(p)
                      and not re.search(rf"(?<![\w./-]){re.escape(p)}(?![\w/-])", change_map.group(1)))
    asked_path = os.path.join(MARKER_DIR, f"{session}.map-asked")
    try:
        asked = set(open(asked_path).read().splitlines())
    except OSError:
        asked = set()
    new = [p for p in unmapped if f"{root}\t{p}" not in asked]
    try:
        os.makedirs(MARKER_DIR, exist_ok=True)
        with open(asked_path, "a") as fh:
            fh.writelines(f"{root}\t{p}\n" for p in new)
    except OSError:
        pass  # asking again next stop beats not asking
    return new


def checked_something(output):
    """Whether a verify ran any check: verify_changed.py and verify_auto.py print `ran: <check>` for each one."""
    return any(line.startswith("ran: ") and not line.startswith("ran: nothing to check")
               for line in output.splitlines())


def save_verify_report(root, verify, result, checked):
    """Keep the last verify run as evidence for the end-of-run summary (`~/.agents/bin/reports`)."""
    reports = os.path.expanduser("~/.agents/bin/reports")
    path = subprocess.run([reports, "path", "verify"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not path:
        return
    output = (result.stdout + result.stderr).strip()[-6000:] or "(no output)"
    if result.returncode != 0:
        verdict = f"FAIL (exit {result.returncode})"
    elif not checked:
        verdict = "PASS, but nothing was checked"
    else:
        verdict = "PASS"
    try:
        with open(path, "w") as report:
            report.write(f"# Verify: {verdict}\n\n{time.strftime('%Y-%m-%d %H:%M:%S')} · `{verify}` in `{root}`\n\n"
                         f"```\n{output}\n```\n")
    except OSError:
        pass


def primary_checkout(root):
    """The main working tree of root's repository, where the validate skill runs the stack."""
    common = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root).strip()
    return os.path.dirname(common) if os.path.basename(common) == ".git" else root


def marked_overrides(dirs):
    found = []
    for directory in sorted(dirs):
        for name in OVERRIDE_NAMES:
            path = os.path.join(directory, name)
            try:
                if OVERRIDE_MARKER in open(path).read():
                    found.append(path)
            except OSError:
                pass
    return found


def known_checkouts():
    try:
        return [line for line in open(CHECKOUTS).read().splitlines() if line]
    except FileNotFoundError:
        return []


def remember_checkout(path):
    """Record every checkout the agents work in, so the health check looks exactly there, wherever it lives."""
    if path not in known_checkouts():
        os.makedirs(os.path.dirname(CHECKOUTS), exist_ok=True)
        with open(CHECKOUTS, "a") as f:
            f.write(path + "\n")


def leftover_overrides():
    existing = [path for path in known_checkouts() if os.path.isdir(path)]
    os.makedirs(os.path.dirname(CHECKOUTS), exist_ok=True)
    with open(CHECKOUTS + ".tmp", "w") as f:
        f.write("".join(path + "\n" for path in existing))
    os.replace(CHECKOUTS + ".tmp", CHECKOUTS)
    for override in marked_overrides(existing):
        print(override)
    return 0


def check_checkout(root, session):
    """Return (problems, verify_failed) for one checkout."""
    problems = []
    lines = list(added_lines(root))
    problems += new_options_near_caches(root, lines)
    problems += leaked_secrets(lines)
    for path, line in lines:
        if TEST_FILE.search(path) and WEAKENED_TEST.search(line):
            problems.append(f"Skipped or focused test added in {path}: `{line.strip()}`")
        elif not TEST_FILE.search(path) and DEBUG_LEFTOVER.search(line):
            problems.append(f"Debug leftover in {path}: `{line.strip()}`")
        if CONFLICT_MARKER.search(line):
            problems.append(f"Merge conflict marker in {path}")
    primary = primary_checkout(root)
    remember_checkout(primary)
    for override in marked_overrides({root, primary}):
        problems.append(f"Temporary {override} from the validate skill is still there: delete it and run "
                        "`docker compose up -d` so the stack serves the primary checkout again.")
    deleted = git(["diff", review_stamp.merge_base(root), "--name-only", "--diff-filter=D"], root).splitlines()
    problems += [f"Test file deleted: {p}" for p in deleted if TEST_FILE.search(p)]
    problems += [f"`{p}` is changed but the spec's Change map doesn't name it: add it to the map with what it changes, "
                 "or split it into another branch." for p in unmapped_files(root, session)]

    verify = os.path.expanduser(f"~/.agents/repos/{review_stamp.repo_name(root)}/verify")
    if not os.access(verify, os.X_OK):
        verify = AUTO_VERIFY
    verify_failed = False
    if os.access(verify, os.X_OK):
        try:
            result = subprocess.run([verify], cwd=root, capture_output=True, text=True, timeout=VERIFY_TIMEOUT)
            checked = checked_something(result.stdout + "\n" + result.stderr)
            save_verify_report(root, verify, result, checked)
            if result.returncode != 0:
                verify_failed = True
                tail = (result.stdout + result.stderr).strip()[-3000:]
                problems.append(f"{verify} failed (exit {result.returncode}):\n{tail}")
            else:
                # Lets the skills skip re-running verify on a change it already passed. A run that checked nothing
                # gets its own stamp, so the flow moves on without reporting it as a pass.
                kind = "verify" if checked else "verify-empty"
                stamp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_stamp.py")
                subprocess.run([sys.executable, stamp, "write", "--kind", kind], cwd=root, capture_output=True)
        except subprocess.TimeoutExpired:
            verify_failed = True
            problems.append(f"{verify} timed out after {VERIFY_TIMEOUT}s.")
    return problems, verify_failed


if __name__ == "__main__":
    sys.exit(leftover_overrides() if sys.argv[1:] == ["leftover-overrides"] else main())
