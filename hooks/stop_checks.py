#!/usr/bin/env python3
"""Stop hook shared by Claude Code, Codex and Pi (via adapters/pi).

After a turn that edited files since the last stop, looks at the lines the
branch adds since the merge-base with the default branch (committed or not) and asks
the agent to continue, once, for a skipped or focused test, a deleted test file, a debug
leftover, a conflict marker, a possible secret, a new option read near a cache, a
temporary compose override left behind, or a changed code file the spec's Change map
doesn't name. Then runs the repo's verify: the overlay in ~/.agents/repos/<repo-name>/verify
when it exists, else repos/_shared/verify_auto.py. After a turn that pushed (the shell guard records it), even
one that edited nothing, asks about the pushed commit's CI when it failed, is still running or can't be read. Once a
branch's PR is open and the session's context is over CONTEXT_NUDGE_TOKENS, or the session moves on to another change
with its context over NEW_CHANGE_NUDGE_TOKENS, tells the user, once, that a new session picks it up for less (Claude
Code only: it reads the transcript). Every stop also starts each profile's `after-turn` in the background with the
stop payload on stdin, and doesn't wait for it.

`stop_checks.py leftover-overrides` prints the marked overrides still present in every checkout
the hook has seen, for the weekly health check. `stop_checks.py verify` runs verify on the current checkout now,
saving its report and stamping a pass, which is the only way a verify stamp gets written.
"""
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import log  # noqa: E402
import review_stamp  # noqa: E402

MARKER_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks")
CHECKOUTS = os.path.join(os.environ.get("AGENTS_STATE_DIR") or os.path.expanduser("~/.agents/monitors/state"), "checkouts.txt")
OVERRIDE_NAMES = ("docker-compose.override.yml", "docker-compose.override.yaml", "compose.override.yml", "compose.override.yaml")
OVERRIDE_MARKER = "agents: temporary override"
VERIFY_TIMEOUT = 600
PROFILES = os.environ.get("AGENTS_PROFILES_DIR") or os.path.expanduser("~/.agents/profiles")
CI_WAIT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "ci-wait"))
# Checks show up a little after a push; "no checks" long after it means the repo runs no CI. ci-wait's
# GRACE_SECS is the same wait.
NO_CHECKS_GRACE_SECS = 300
MAX_PROBLEMS = 20
# Every turn re-reads the whole context; past this, a new session that starts from `reports brief` costs less.
CONTEXT_NUDGE_TOKENS = 250_000
# A session that moves on to another change carries the earlier one's history into every turn of the new one. Lower
# than the threshold above: the new change has all its turns ahead of it.
NEW_CHANGE_NUDGE_TOKENS = 150_000
DEFAULT_BRANCHES = ("main", "master", "trunk", "develop")
TRANSCRIPT_TAIL_BYTES = 2_000_000
SPEC_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "spec", "path.sh"))
AUTO_VERIFY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repos", "_shared", "verify_auto.py"))

TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|[._-](test|spec)\.[a-z]+$|Test\.php$", re.I)
WEAKENED_TEST = re.compile(
    r"\b(it|test|describe)\.(skip|only)\(|\bx(it|describe|test)\(|markTest(Skipped|Incomplete)\("
    r"|@pytest\.mark\.skip|\bt\.Skip\(|@Disabled\b|@group\s+(skip|ignore)"
)
DEBUG_LEFTOVER = re.compile(r"\bvar_dump\(|\bdebugger;|^\s*dd\(|\bbinding\.pry\b|\bbreakpoint\(\)")
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)")
# Generated, never written by hand: lock files, and bytecode that imports and test runs leave next to the source.
GENERATED_FILE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|[\w.-]+\.lock)$"
                            r"|(^|/)__pycache__/|\.py[co]$")


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
    start_profile_after_turns(payload)
    if payload.get("stop_hook_active"):
        return 0
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "unknown"))
    notice = fresh_session_notice(session, payload)
    marker = os.path.join(MARKER_DIR, f"{session}.edited")
    edited = None
    if os.path.exists(marker):
        edited = [p for p in open(marker).read().splitlines() if p]
        os.remove(marker)
    # Runs even when the notice above speaks: it records the branches the session works on.
    next_change = new_change_notice(session, payload, edited or [])
    if next_change and not notice:
        notice, detail = next_change
        log("stop_checks", "new-change", payload, detail)
    problems = ci_after_push(session, payload)
    if edited is None:
        return block(payload, problems, streak=0, notice=notice)

    # Check every checkout the session touched (worktrees included), not only the session's cwd.
    cwd = payload.get("cwd") or os.getcwd()
    roots = []
    for path in edited or [cwd]:
        start = path if os.path.isdir(path) else os.path.dirname(path)
        root = git(["rev-parse", "--show-toplevel"], start).strip() if os.path.isdir(start) else ""
        if root and root not in roots:
            roots.append(root)
    any_verify_failed = False
    for root in roots:
        try:
            found, failed_verify = check_checkout(root, session, payload)
        except FileNotFoundError:
            if os.path.isdir(root):
                raise
        if not os.path.isdir(root):  # removed while checked, e.g. a worktree freed as the session ends
            log("stop_checks", "checkout-removed", payload, root)
            continue
        problems += [f"[{review_stamp.repo_name(root)}] {p}" if len(roots) > 1 else p for p in found]
        any_verify_failed = any_verify_failed or failed_verify
    streak = record_verify_streak(session, any_verify_failed)
    return block(payload, problems, streak, notice)


def context_tokens(transcript):
    """The context the last main-thread turn read, from the end of a Claude Code transcript."""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(max(0, os.path.getsize(transcript) - TRANSCRIPT_TAIL_BYTES))
            lines = fh.read(TRANSCRIPT_TAIL_BYTES).decode(errors="ignore").splitlines()
    except OSError:
        return 0
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the first line of the tail is usually cut
        usage = (entry.get("message") or {}).get("usage")
        if entry.get("type") == "assistant" and usage and not entry.get("isSidechain"):
            return sum(usage.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return 0


def start_profile_after_turns(payload):
    """Start each profile's after-turn on its own copy of the payload and return at once: a pipe would block on a
    child that doesn't read it. A session an after-turn starts (codex exec, claude -p) runs these hooks too, so
    AGENTS_AFTER_TURN marks it and its stops start nothing."""
    if "AGENTS_AFTER_TURN" in os.environ:
        return
    for script in sorted(glob.glob(os.path.join(PROFILES, "*", "after-turn"))):
        try:
            with tempfile.TemporaryFile() as stdin:
                stdin.write(json.dumps(payload).encode())
                stdin.seek(0)
                subprocess.Popen([script], stdin=stdin, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True, env={**os.environ, "AGENTS_AFTER_TURN": "1"})
        except OSError as error:
            profile = os.path.basename(os.path.dirname(script))
            log("stop_checks", "profile-after-turn-failed", payload, f"{profile}: {error}")


def fresh_session_notice(session, payload):
    """Once per session and branch: the PR is open (or merged) and the context is large, so a new session is cheaper.
    Advisory: any failure here returns None rather than stopping the checks that follow."""
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    try:  # the session's directory may be gone: another session removed its worktree
        out = git(["rev-parse", "--path-format=absolute", "--git-common-dir", "--abbrev-ref", "HEAD"],
                  payload.get("cwd") or os.getcwd()).splitlines()
    except (OSError, ValueError):
        return None
    if len(out) != 2 or out[1] == "HEAD":
        return None
    common_dir, branch = out
    try:  # the phase the status line last computed (bin/phase); asking GitHub here would slow every stop
        label = json.load(open(os.path.join(common_dir, "agents", "phase", review_stamp.branch_key(branch) + ".json")))["label"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not label.startswith(("PR open", "ship")):
        return None
    tokens = context_tokens(transcript)
    if tokens <= CONTEXT_NUDGE_TOKENS:
        return None
    identity = hashlib.sha256(f"{common_dir}\0{branch}".encode()).hexdigest()[:16]
    try:  # creating the marker is the claim, so two overlapping stops can't both tell
        os.makedirs(MARKER_DIR, exist_ok=True)
        os.close(os.open(os.path.join(MARKER_DIR, f"{session}.fresh-session-{identity}"), os.O_CREAT | os.O_EXCL))
    except OSError:
        return None
    log("stop_checks", "fresh-session", payload, f"{branch} {tokens // 1000}K")
    state = "is merged" if label.startswith("ship") else "is open"
    return (f"This session's context is {tokens // 1000}K tokens, re-read on every turn, and {branch}'s PR {state}. "
            "Follow it up or start the next change in a new session: `~/.agents/bin/reports brief` there picks it up.")


def git_or_fail(args, cwd):
    """git's output; raises CalledProcessError when it fails, where `git` would hand back an empty answer."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=10).stdout


def change_branch(directory):
    """(common git dir, branch) of the checkout at `directory` when it is on a branch other than the default one
    (origin's HEAD, else any of DEFAULT_BRANCHES); None outside a repo."""
    out = git(["rev-parse", "--path-format=absolute", "--git-common-dir", "--abbrev-ref", "HEAD"], directory).splitlines()
    if len(out) != 2 or out[1] == "HEAD":
        return None
    default = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], directory).strip()
    return None if out[1] in ((default.split("/", 1)[-1],) if default else DEFAULT_BRANCHES) else (out[0], out[1])


def earlier_names(branch, directory):
    """The names this branch had before `git branch -m`, from its reflog, which git carries across renames. A copy
    (`git branch -c`) carries it too, so the names before the copy are another branch's."""
    names = []
    for entry in git_or_fail(["reflog", "show", "--format=%gs", f"refs/heads/{branch}"], directory).splitlines():
        if entry.startswith("Branch: copied "):
            break
        renamed = re.match(r"Branch: renamed refs/heads/(.+) to refs/heads/", entry)
        if renamed:
            names.append(renamed.group(1))
    return names


def new_change_notice(session, payload, edited):
    """(message, log detail) once per session and branch: the session works on a branch, in its directory or a
    worktree it edited, after another one, with a large context. Advisory: any failure here returns None rather than
    stopping the checks."""
    transcript = payload.get("transcript_path")
    if not transcript:
        return None
    try:
        return record_branches_and_notice(session, transcript, payload, edited)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def record_branches_and_notice(session, transcript, payload, edited):
    directories = dict.fromkeys([payload.get("cwd") or os.getcwd()] + [p if os.path.isdir(p) else os.path.dirname(p) for p in edited])
    current = {}
    for directory in directories:
        found = change_branch(directory) if os.path.isdir(directory) else None
        if found:
            current[found] = directory
    seen_file = os.path.join(MARKER_DIR, f"{session}.branches")
    try:
        lines = open(seen_file).read().splitlines()
    except OSError:
        lines = []
    seen = set()
    for line in lines:
        try:
            pair = json.loads(line)
        except ValueError:
            continue  # a torn line: that branch's history is lost, nothing more
        if isinstance(pair, list) and len(pair) == 2 and all(isinstance(part, str) for part in pair):
            seen.add(tuple(pair))
    had_history, new = bool(seen), []
    for (common_dir, branch), directory in current.items():
        if (common_dir, branch) in seen:
            continue
        if not any((common_dir, name) in seen for name in earlier_names(branch, directory)):
            new.append(branch)
        seen.add((common_dir, branch))
    os.makedirs(MARKER_DIR, exist_ok=True)
    with open(seen_file + ".tmp", "w") as fh:  # replaced whole, so the next stop never reads it half written
        fh.write("".join(json.dumps(list(pair)) + "\n" for pair in sorted(seen)))
    os.replace(seen_file + ".tmp", seen_file)
    if not new or not had_history:  # the session's first change carries no earlier one
        return None
    tokens = context_tokens(transcript)
    if tokens <= NEW_CHANGE_NUDGE_TOKENS:
        return None
    return (f"This session's context is {tokens // 1000}K tokens, re-read on every turn, and it has moved on to "
            f"{new[0]}. A new session for it starts lighter: `~/.agents/bin/reports brief` there picks up its spec and "
            "reports.", f"{new[0]} {tokens // 1000}K")


def block(payload, problems, streak, notice=None):
    if notice and not problems:
        print(json.dumps({"systemMessage": notice}))
    if problems:
        reason = (
            "Before finishing, resolve these or explain to the user why they are intended. If you can't resolve one "
            "honestly, report it under **Blocked on me** with what you tried; never work around a check "
            "(skipping or loosening tests, deleting files, disabling a rule, editing the hook):\n- "
            + "\n- ".join(problems[:MAX_PROBLEMS])
        )
        output = {"decision": "block", "reason": reason}
        for problem in problems[:MAX_PROBLEMS]:
            log("stop_checks", "block", payload, problem)
        if streak >= 2:
            # Hooks can't change effort; on Opus 5.5 raising it mid-session keeps the cache, so nudge the user.
            output["systemMessage"] = (f"Verification has failed {streak} times in a row in this session. "
                                       "Consider /effort high (or xhigh) for this task.")
            log("stop_checks", "effort-nudge", payload, f"verify failed {streak} times in a row")
        if notice:
            output["systemMessage"] = (output.get("systemMessage", "") + "\n" + notice).strip()
        print(json.dumps(output))
    return 0


def unmapped_files(root, session):
    """Changed code files the branch's spec has no Change map entry for, each returned once per session."""
    # path.sh finds the spec wherever it is kept: after a branch rename, in a worktree's old place or in $TMPDIR.
    spec = subprocess.run([SPEC_PATH], cwd=root, capture_output=True, text=True).stdout.strip()
    if not os.path.isfile(spec):
        return []
    change_map = re.search(r"^## Change map\s*$(.*?)(?=^## |\Z)", open(spec, errors="ignore").read(), re.M | re.S | re.I)
    if not change_map:
        return []
    base = review_stamp.merge_base(root)
    changed = set(git(["-c", "core.quotePath=off", "diff", "--name-only", base], root).splitlines())
    changed |= set(git(["-c", "core.quotePath=off", "ls-files", "--others", "--exclude-standard"], root).splitlines())
    # A path, not a bare file name: `app.py` in the map doesn't cover `other/app.py`, nor does `app.py.bak`.
    unmapped = sorted(p for p in changed if p and not review_stamp.NOT_BEHAVIOR.search(p) and not GENERATED_FILE.search(p)
                      and not re.search(rf"(?<![\w./-])(\./)?{re.escape(p)}(?![\w/-]|\.\w)", change_map.group(1)))
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


def ci_after_push(session, payload):
    """For each checkout this session pushed from, a question when its pushed commit's CI failed, is still running or
    can't be read; each once per checkout, commit and state. A checkout is followed until its CI passed, failed or
    turned out to have none, so later turns don't query GitHub for it again."""
    pushed_path = os.path.join(MARKER_DIR, f"{session}.pushed")
    try:
        lines = open(pushed_path).read().splitlines()
        written_at = os.path.getmtime(pushed_path)
    except OSError:
        return []
    pushed_at = {}
    for line in lines:
        root, _, at = line.partition("\t")
        pushed_at[root] = max(pushed_at.get(root, 0.0), float(at) if at else written_at)  # a bare path: an older guard
    asked_path = os.path.join(MARKER_DIR, f"{session}.ci-asked")
    try:
        asked = set(open(asked_path).read().splitlines())
    except OSError:
        asked = set()
    problems, followed = [], {}
    for root, at in sorted(pushed_at.items()):
        if len(problems) == MAX_PROBLEMS:  # an unshown question isn't asked: keep following its checkout
            followed[root] = at
            continue
        if not os.path.isdir(root):
            continue  # the worktree was removed after the push
        sha = git(["rev-parse", "--verify", "--quiet", "@{upstream}"], root).strip()
        if not sha:
            continue  # the push failed, or pushed nothing this checkout tracks
        wait = f"`~/.agents/bin/ci-wait --sha {sha}`"
        try:
            result = subprocess.run([CI_WAIT, "--sha", sha, "--once", "--no-log"], cwd=root, capture_output=True, text=True, timeout=90)
            code, out = result.returncode, result.stdout.strip()
        except subprocess.TimeoutExpired:
            code, out = 4, "ci-wait timed out"
        if code == 3 and time.time() - at > NO_CHECKS_GRACE_SECS:
            continue  # this repo runs no CI
        failed_on = [line for line in out.splitlines() if line.startswith("ci-wait: failed on ")]
        if code == 1 and not failed_on:
            code = 4  # ci-wait itself broke; a traceback isn't a CI result
        state = {1: "failed", 2: "running", 3: "running", 4: "unreadable"}.get(code)
        if state in ("running", "unreadable"):
            followed[root] = at
        if not state or f"{root}\t{sha}\t{state}" in asked:
            continue
        asked.add(f"{root}\t{sha}\t{state}")
        names = failed_on[-1].split(": ", 2)[-1] if failed_on else ""
        problems.append({
            "failed": f"CI failed on {sha[:12]} ({names}). Read the failures with {wait} and fix them, or tell the user "
                      "why not; don't call the work done.",
            "running": f"CI is still running on {sha[:12]}: wait for it in the foreground with {wait} (see the follow-pr "
                       "skill when your harness caps a command's run time) and act on the result before saying the work "
                       "is done.",
            "unreadable": f"Couldn't read CI for {sha[:12]} from GitHub ({out.splitlines()[-1] if out else 'no output'}): "
                          f"check it with {wait} before calling it passed.",
        }[state])
        log("stop_checks", f"ci-{state}", payload, f"{root} {sha[:12]}")
    try:
        with open(asked_path, "w") as fh:
            fh.writelines(line + "\n" for line in sorted(asked))
        pushed_since = open(pushed_path).read().splitlines()[len(lines):]  # by a subagent while CI was read
        with open(pushed_path, "w") as fh:
            fh.writelines(f"{root}\t{at}\n" for root, at in sorted(followed.items()))
            fh.writelines(line + "\n" for line in pushed_since)
    except OSError:
        pass  # asking again next stop beats not asking
    return problems


def checked_something(output):
    """Whether a verify ran any check: verify_changed.py and verify_auto.py print `ran: <check>` for each one."""
    return any(line.startswith("ran: ") and not line.startswith("ran: nothing to check")
               for line in output.splitlines())


def save_verify_report(root, verify, output, verdict):
    """Keep the last verify run as evidence for the end-of-run summary (`~/.agents/bin/reports`)."""
    reports = os.path.expanduser("~/.agents/bin/reports")
    path = subprocess.run([reports, "path", "verify"], cwd=root, capture_output=True, text=True).stdout.strip()
    if not path:
        return
    output = output.strip()[-6000:] or "(no output)"
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


def check_checkout(root, session, payload):
    """Return (problems, verify_failed) for one checkout."""
    problems = []
    unmapped = unmapped_files(root, session)
    if unmapped:  # first, so the cap on listed problems never hides a file it has marked as asked
        problems.append("Changed code files the spec's Change map doesn't name: " + ", ".join(f"`{p}`" for p in unmapped)
                        + ". Add each to the map with what it changes, or split it into another branch.")
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

    verify_problems, verify_failed, _, stamp_error = run_verify(root)
    problems += verify_problems
    if stamp_error:  # logged, not blocking: a missing stamp only makes a skill run verify again
        log("stop_checks", "verify-stamp", payload, f"{root}: {stamp_error}")
    return problems, verify_failed


def run_verify(root):
    """Run the repo's verify in root, save its report and leave only this run's stamp: the only path to a verify
    stamp. Returns (problems, failed, output, stamp error or None)."""
    verify = os.path.expanduser(f"~/.agents/repos/{review_stamp.repo_name(root)}/verify")
    if not os.access(verify, os.X_OK):
        verify = AUTO_VERIFY
    if not os.access(verify, os.X_OK):
        problems = [f"No verify to run: neither {verify} nor ~/.agents/repos/<repo>/verify is executable."]
        save_verify_report(root, verify, "", "FAIL (no verify to run)")
        return problems, True, "", record_verify(root, None, "")
    # Stamped with the content verify started from: an edit made while it ran leaves the change unstamped.
    try:
        checked_fingerprint = in_checkout(root, review_stamp.fingerprint)
    except RuntimeError as error:
        # An earlier run's stamp must not outlive a run that couldn't check anything.
        return [f"Couldn't fingerprint the change, so verify can't stamp it: {error}"], True, "", record_verify(root, None, "")
    try:
        result = subprocess.run([verify], cwd=root, capture_output=True, text=True, errors="replace", timeout=VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired as timeout:
        # On POSIX the partial output comes back as bytes even with text=True.
        output = "".join(part.decode(errors="replace") if isinstance(part, bytes) else part or ""
                         for part in (timeout.stdout, timeout.stderr))
        save_verify_report(root, verify, output, f"FAIL (timed out after {VERIFY_TIMEOUT}s)")
        return [f"{verify} timed out after {VERIFY_TIMEOUT}s."], True, output, record_verify(root, None, "")
    except OSError as error:
        save_verify_report(root, verify, "", f"FAIL (couldn't start: {error})")
        return [f"{verify} couldn't start: {error}"], True, "", record_verify(root, None, "")
    output = result.stdout + result.stderr
    checked = checked_something(result.stdout + "\n" + result.stderr)
    if result.returncode != 0:
        save_verify_report(root, verify, output, f"FAIL (exit {result.returncode})")
        problems = [f"{verify} failed (exit {result.returncode}):\n{output.strip()[-3000:]}"]
        return problems, True, output, record_verify(root, None, "")
    save_verify_report(root, verify, output, "PASS" if checked else "PASS, but nothing was checked")
    # Lets the skills skip re-running verify on a change it already passed. A run that checked nothing
    # gets its own stamp, so the flow moves on without reporting it as a pass.
    return [], False, output, record_verify(root, "verify" if checked else "verify-empty", checked_fingerprint)


def in_checkout(root, action, *args):
    """review_stamp works on the checkout in the working directory; the stop hook visits several."""
    try:
        previous = os.getcwd()
    except FileNotFoundError:  # the session's directory was removed (another session freed its worktree)
        previous = None
    os.chdir(root)
    try:
        return action(*args)
    finally:
        try:
            if previous:
                os.chdir(previous)
        except FileNotFoundError:  # removed while we were away
            pass


def record_verify(root, kind, checked_fingerprint):
    try:
        in_checkout(root, review_stamp.record_verify, kind, checked_fingerprint)
    except OSError as error:
        return f"Couldn't update the verify stamp: {error}"
    return None


def verify_here():
    """`stop_checks.py verify`: run verify on the checkout in the working directory now, as the stop hook would."""
    toplevel = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if toplevel.returncode != 0:
        print(f"stop_checks.py verify: verify runs on a repository's changes; {toplevel.stderr.strip()}", file=sys.stderr)
        return 1
    root = toplevel.stdout.strip()
    problems, failed, output, stamp_error = run_verify(root)
    print(output, end="")
    for problem in problems + [stamp_error] * bool(stamp_error):
        print(problem.splitlines()[0], file=sys.stderr)
    return 1 if failed or stamp_error else 0


if __name__ == "__main__":
    command = sys.argv[1:]
    sys.exit(leftover_overrides() if command == ["leftover-overrides"] else verify_here() if command == ["verify"] else main())
