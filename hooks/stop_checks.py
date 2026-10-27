#!/usr/bin/env python3
"""Stop hook shared by Claude Code and Codex.

After a turn that edited files since the last stop, looks at the lines the
branch adds since the merge-base with its base (`review_stamp.branch_base`: the
default branch, or the branch it is stacked on; committed or not) and asks
the agent to continue, once, for a skipped or focused test, a deleted test file, a debug
leftover, a conflict marker, a possible secret, a new option read near a cache, a
temporary compose override left behind, or a changed code file the spec's Change map
doesn't name. It also asks the agent to justify, once per session each, every assertion the branch removes from a test
file without adding it back, swaps from a strict matcher to a loose one, or changes the expected literal of while it
also changes code. Then runs the repo's verify: the overlay in ~/.agents/repos/<repo-name>/verify
when it exists, else repos/_shared/verify_auto.py. After a turn that pushed (the shell guard records it), even
one that edited nothing, asks about the pushed commit's CI when it failed, is still running or can't be read. Once a
branch's PR is open and the session's context is over CONTEXT_NUDGE_TOKENS, or the session moves on to another change
with its context over NEW_CHANGE_NUDGE_TOKENS, tells the user, once, that a new session picks it up for less (Claude
Code only: it reads the transcript). Every stop also starts each profile's `after-turn` in the background with the
stop payload on stdin, and doesn't wait for it. The Change map question, verify and the CI question each stay quiet
for a checkout whose user switched off the spec, verify or ci phase (phase_switches.py); the other checks always run.

`stop_checks.py leftover-overrides` prints the marked overrides still present in every checkout
the hook has seen, for the weekly health check. `stop_checks.py verify` runs verify on the current checkout now,
saving its report and stamping a pass, which is the only way a verify stamp gets written; it prints the weakened
assertions as `warning:` lines, which change neither.
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
import phase_switches  # noqa: E402
import post_edit  # noqa: E402
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
CONTEXT_NUDGE_TOKENS = 150_000
# A session that moves on to another change carries the earlier one's history into every turn of the new one. Not
# above the threshold above: the new change has all its turns ahead of it.
NEW_CHANGE_NUDGE_TOKENS = 150_000
DEFAULT_BRANCHES = ("main", "master", "trunk", "develop")
TRANSCRIPT_TAIL_BYTES = 2_000_000
SPEC_PATH = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "kit-spec", "path.sh"))
AUTO_VERIFY = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "repos", "_shared", "verify_auto.py"))

TEST_FILE = review_stamp.TEST_FILE
WEAKENED_TEST = re.compile(
    r"\b(it|test|describe)\.(skip|only)\(|\bx(it|describe|test)\(|markTest(Skipped|Incomplete)\("
    r"|@pytest\.mark\.skip|\bt\.Skip\(|@Disabled\b|@group\s+(skip|ignore)"
)
# Matched on an assertion's first line: PHPUnit, Jest/Vitest, pytest and unittest.
ASSERTION = re.compile(r"\bassert[A-Z_]\w*\s*\(|^\s*assert\b|\bexpect(Exception\w*)?\s*\(|\bpytest\.raises\(")
STRICT_ASSERTION = re.compile(
    r"\bassert(Same|Equals?|Count|Contains|StringContainsString)\s*\(|\bexpectException\s*\("
    r"|\.(toBe|toEqual|toStrictEqual|toHaveLength|toContain|toThrow|toHaveBeenCalledWith|toMatchObject)\("
    r"|^\s*assert\s.*[^=!<>]==[^=]|\bpytest\.raises\("
)
LOOSE_ASSERTION = re.compile(
    r"\bassert(NotNull|NotEmpty|IsArray|IsString|IsNotNone)\s*\(|\bassertTrue\(\s*true\s*\)|\bself\.assertTrue\("
    r"|\.(toBeDefined|toBeTruthy)\(|\.not\.(toBeNull|toBeUndefined)\(|\.toHaveBeenCalled\(\s*\)"
    r"|^\s*assert\s+[\w.()\[\]'\"]+(\s+is\s+not\s+None)?\s*(,.*)?$"
)
LITERAL = re.compile(r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`|\b\d+(?:\.\d+)?\b")
HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")
DEBUG_LEFTOVER = re.compile(r"\bvar_dump\(|\bdebugger;|^\s*dd\(|\bbinding\.pry\b|\bbreakpoint\(\)")
CONFLICT_MARKER = re.compile(r"^(<{7}|>{7})( |$)")
# Generated, never written by hand: lock files, and bytecode that imports and test runs leave next to the source.
GENERATED_FILE = re.compile(r"(^|/)(package-lock\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|go\.sum|[\w.-]+\.lock)$"
                            r"|(^|/)__pycache__/|\.py[co]$")


def git(args, cwd):
    return review_stamp.run_git(args, cwd=cwd).stdout


def branch_hunks(root):
    """(path, removed lines, added lines) for each hunk the branch changes since it left its base (committed or not),
    and each untracked file as one hunk of added lines: a leftover committed mid-session still reaches the pull
    request. A deleted file's hunks are under its old path."""
    diff = git(["diff", review_stamp.merge_base(root), "--unified=0", "--no-color"], root)
    hunks, path, left = [], None, 0
    for line in diff.splitlines():
        if left:  # counted from the hunk header: a removed `-- x` reads as `--- x`, an added `++ x` as `+++ x`
            if line.startswith(("-", "+")):
                hunks[-1][1 if line[0] == "-" else 2].append(line[1:])
                left -= 1
        elif line.startswith("--- "):
            path = line[6:] if line.startswith("--- a/") else None
        elif line.startswith("+++ b/"):
            path = line[6:]
        elif header := HUNK_HEADER.match(line):
            hunks.append((path, [], []))
            left = sum(1 if count is None else int(count) for count in header.groups())
    for path in git(["ls-files", "--others", "--exclude-standard"], root).splitlines():
        try:
            with open(os.path.join(root, path), encoding="utf-8", errors="ignore") as fh:
                hunks.append((path, [], [line.rstrip("\n") for line in fh]))
        except OSError:
            continue
    return [hunk for hunk in hunks if hunk[0]]


def normalized(line):
    return " ".join(line.split())


def weakened_assertions(root, hunks):
    """(asked-store key, question) for each assertion the branch weakens in a test file it keeps: one removed and not
    added back anywhere, a strict one swapped for a loose one in the same hunk, or one whose literals alone change
    while the branch also changes code. A rewrite from one strict assertion to another is none."""
    added_back = {normalized(line) for _, _, added in hunks for line in added}
    removed = {normalized(line) for _, gone, _ in hunks for line in gone}
    changes_code = any(not review_stamp.NOT_BEHAVIOR.search(path) and not GENERATED_FILE.search(path)
                       for path, _, _ in hunks)
    found = []
    for path, gone, added in hunks:
        if not TEST_FILE.search(path) or not os.path.exists(os.path.join(root, path)):
            continue  # a deleted test file is asked about as a deletion
        lost = [line.strip() for line in gone if ASSERTION.search(line) and normalized(line) not in added_back]
        new = [line.strip() for line in added if ASSERTION.search(line) and normalized(line) not in removed]

        def replacement(old, fits):
            line = next((line for line in new if fits(line)), None)
            if line is not None:
                new.remove(line)
            return line

        rewritten = []
        for old in lost:  # paired first, so a looser pairing below can't take the line that only changes a value
            shape = LITERAL.sub("0", normalized(old))
            changed_value = replacement(old, lambda line: LITERAL.sub("0", normalized(line)) == shape)
            if changed_value is None:
                rewritten.append(old)
            elif changes_code:
                found.append((path, old, f"Expected value changed in {path} while the branch also changes code: `{old}` "
                                         f"became `{changed_value}`. Confirm the spec asks for the new value, or put the "
                                         "old one back."))
        for old in rewritten:
            strict = STRICT_ASSERTION.search(old)
            loose = strict and replacement(old, lambda line: LOOSE_ASSERTION.search(line)
                                           and not STRICT_ASSERTION.search(line))
            if loose:
                found.append((path, old, f"Assertion loosened in {path}: `{old}` became `{loose}`. Say why the looser check "
                                         "is enough, or put the strict one back."))
            elif not (strict and replacement(old, STRICT_ASSERTION.search)):
                found.append((path, old, f"Assertion removed from {path}, not added back anywhere: `{old}`. Say why the "
                                         "check is no longer needed, or put it back."))
    return list({f"{root}\t{path}\t{normalized(old)}": question for path, old, question in found}.items())


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
            existed = review_stamp.run_git(["grep", "-q", "-F", name, "HEAD", "--"], cwd=root).returncode == 0
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
                    " when it changes (review Correctness lens, derived-data table)."
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
    read = edited = None
    if os.path.exists(marker):
        # Cleared only once the checks finish (clear_checked): a stop that crashes or is killed leaves it for the next.
        with post_edit.marker_lock(marker), open(marker) as fh:
            read = fh.read()
        edited = [p for p in read.splitlines() if p]
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
    root_of, unfinished = {}, {}
    for path in edited or [cwd]:
        try:
            root_of[path] = checkout_of(path)
        except RuntimeError as error:
            unfinished[path] = error
    roots = list(dict.fromkeys(root for root in root_of.values() if root))
    questions, others, asked, tamper_asked = list(problems), [], [], []
    any_verify_failed = False
    for root in roots:
        try:
            unmapped, tampered, found, failed_verify = check_checkout(root, session, payload)
            name = f"[{review_stamp.repo_name(root)}] " if len(roots) > 1 else ""
        except FileNotFoundError:
            if os.path.isdir(root):
                raise
        except RuntimeError as error:  # a git call timed out or failed: say so, and check its edits again next stop
            unfinished[root] = error
            continue
        if not os.path.isdir(root):  # removed while checked, e.g. a worktree freed as the session ends
            log("stop_checks", "checkout-removed", payload, root)
            continue
        if unmapped:
            questions.append(name + unmapped_question(unmapped))
            asked += [f"{root}\t{path}" for path in unmapped]
        questions += [name + question for _, question in tampered]
        tamper_asked += [key for key, _ in tampered]
        others += [name + p for p in found]
        any_verify_failed = any_verify_failed or failed_verify
    questions += [f"Couldn't finish checking {where}: {error}. Its edits are checked again at the next stop."
                  for where, error in unfinished.items()]
    # The cut hides only problems the next check finds again; a question is asked once, so it is always shown.
    room = max(MAX_PROBLEMS - len(questions), 0)
    hidden = len(others) - room
    listed = questions + others[:room] + [f"…and {hidden} more problems not listed: fixing these brings them up at "
                                          "the next stop."] * (hidden > 0)
    streak = record_verify_streak(session, any_verify_failed)
    block(payload, listed, streak, notice)
    sys.stdout.flush()  # the block is out before its questions are recorded as asked
    mark_asked(session, "map-asked", asked)
    mark_asked(session, "tamper-asked", tamper_asked)
    clear_checked(marker, read, [path for path in edited if path in unfinished or root_of.get(path) in unfinished])
    return 0


def checkout_of(path):
    """The top level of the checkout holding path, found from its nearest existing directory, since a deleted file's
    directories may be gone too; "" outside a repository."""
    directory = start = path if os.path.isdir(path) else os.path.dirname(path)
    while not os.path.isdir(start) and os.path.dirname(start) != start:
        start = os.path.dirname(start)
    root = git(["rev-parse", "--show-toplevel"], start).strip()
    # A removed worktree nested in an ignored directory of its main checkout would otherwise read as the main's edit.
    # A file the checkout tracks (in its index, or in HEAD once `git rm` staged the deletion) is still its own.
    relative = os.path.relpath(os.path.realpath(path), root) if root else ""
    if (root and start != directory
            and review_stamp.run_git(["check-ignore", "-q", relative], cwd=root).returncode == 0
            and not git(["ls-tree", "--name-only", "HEAD", "--", relative], root).strip()):
        return ""
    return root


def clear_checked(marker, read, kept):
    """Remove from the edit marker the lines this stop read, keeping `kept` (paths it couldn't check) and every line
    post_edit appended meanwhile: a subagent can edit while the checks run."""
    with post_edit.marker_lock(marker):
        try:
            with open(marker) as fh:
                later = fh.read().removeprefix(read)
        except FileNotFoundError:
            later = ""
        rest = "".join(path + "\n" for path in kept) + later
        if rest:
            with open(f"{marker}.{os.getpid()}", "w") as fh:
                fh.write(rest)
            os.replace(fh.name, marker)
        elif os.path.exists(marker):
            os.remove(marker)


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
    except (OSError, ValueError, RuntimeError):
        return None
    if len(out) != 2 or out[1] == "HEAD":
        return None
    common_dir, branch = out
    try:  # the phase the status line last computed (bin/phase); asking GitHub here would slow every stop
        label = json.load(open(os.path.join(common_dir, "agents", "phase", review_stamp.branch_key(branch) + ".json")))["label"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    merged = label.startswith("merged")
    if not (merged or label.startswith("PR open")):
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
    state = "is merged" if merged else "is open"
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
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
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
            + "\n- ".join(problems)
        )
        output = {"decision": "block", "reason": reason}
        for problem in problems:
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
    """Changed code files the branch's spec has no Change map entry for, leaving out those already asked about in
    this session (mark_asked)."""
    # path.sh finds the spec wherever it is kept: after a branch rename, in a worktree's old place or in $TMPDIR.
    try:
        found = subprocess.run([SPEC_PATH], cwd=root, capture_output=True, text=True)
    except OSError as error:
        raise RuntimeError(f"couldn't look up the spec with {SPEC_PATH}: {error}") from None
    if found.returncode != 0:  # not "no spec": path.sh prints where a missing one would go
        raise RuntimeError(f"couldn't look up the spec: {SPEC_PATH} failed (exit {found.returncode}): "
                           f"{' '.join(found.stderr.split())}")
    spec = found.stdout.strip()
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
    asked = asked_before(session, "map-asked")
    return [p for p in unmapped if f"{root}\t{p}" not in asked]


def unmapped_question(unmapped):
    return ("Changed code files the spec's Change map doesn't name: " + ", ".join(f"`{p}`" for p in unmapped)
            + ". Add each to the map with what it changes, or split it into another branch.")


def asked_before(session, store):
    try:
        return set(open(os.path.join(MARKER_DIR, f"{session}.{store}")).read().splitlines())
    except OSError:
        return set()


def mark_asked(session, store, asked):
    """Record the questions a block showed, so each is asked once per session: Change-map ones as checkout and file
    in `map-asked`, weakened tests as checkout, file and assertion in `tamper-asked`. Written after the block: a stop
    that fails before showing one asks it again."""
    try:
        os.makedirs(MARKER_DIR, exist_ok=True)
        with open(os.path.join(MARKER_DIR, f"{session}.{store}"), "a") as fh:
            fh.writelines(line + "\n" for line in asked)
    except OSError:
        pass  # asking again next stop beats not asking


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
        off, unreadable = phase_switches.phases_off_or_error(root)
        if unreadable:
            log("stop_checks", "phase-switches", payload, unreadable)
        if "ci" in off:
            followed[root] = at  # still followed: switched back on, the push is asked about
            continue
        try:
            sha = git(["rev-parse", "--verify", "--quiet", "@{upstream}"], root).strip()
        except RuntimeError as error:  # timed out: ask about it, and read it again next stop
            followed[root] = at
            problems.append(f"Couldn't read the pushed commit of {root} to check its CI: {error}. It is read again "
                            "at the next stop.")
            continue
        if not sha:
            continue  # the push failed, or pushed nothing this checkout tracks
        wait = f"`~/.agents/bin/ci-wait --sha {sha}`"
        try:
            result = subprocess.run([CI_WAIT, "--sha", sha, "--once", "--no-log"], cwd=root, capture_output=True, text=True, timeout=90)
            code, out = result.returncode, result.stdout.strip()
        except subprocess.TimeoutExpired:
            code, out = 4, "ci-wait timed out"
        if code == 5 or (code == 3 and time.time() - at > NO_CHECKS_GRACE_SECS):
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
            "running": f"CI is still running on {sha[:12]}: wait for it in the foreground with {wait} (see the kit-ship "
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
    """Keep the last verify run as evidence for the end-of-run summary (`~/.agents/bin/reports`). Returns why it
    couldn't, or None: an earlier run's report left in place would read as this run's result, so it is removed."""
    reports = os.path.expanduser("~/.agents/bin/reports")
    stale = "an earlier report, if there is one, still reads as the last result."
    try:
        found = subprocess.run([reports, "path", "verify"], cwd=root, capture_output=True, text=True)
    except OSError as error:
        return f"Couldn't save the verify report: `{reports}` didn't run ({error}); {stale}"
    path = found.stdout.strip()
    if not path:
        return (f"Couldn't save the verify report: `{reports} path verify` gave no path "
                f"({' '.join(found.stderr.split())}); {stale}")
    output = output.strip()[-6000:] or "(no output)"
    try:
        with open(path, "w") as report:
            report.write(f"# Verify: {verdict}\n\n{time.strftime('%Y-%m-%d %H:%M:%S')} · `{verify}` in `{root}`\n\n"
                         f"```\n{output}\n```\n")
    except OSError as error:
        problem = f"Couldn't save the verify report {path}: {error}."
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as removal:
            return f"{problem} The earlier one couldn't be removed either ({removal}): the stale report is still there."
        return problem
    return None


def primary_checkout(root):
    """The main working tree of root's repository, where the kit-validate skill runs the stack."""
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
    """Return (changed files the Change map doesn't name, (key, question) for each weakened test not asked about yet,
    other problems, verify_failed) for one checkout."""
    off, unreadable = phase_switches.phases_off_or_error(root)
    if unreadable:  # not a problem to block on: the agent can't fix the user's store; the gates and status line say it
        log("stop_checks", "phase-switches", payload, unreadable)
    problems = []
    unmapped = [] if "spec" in off else unmapped_files(root, session)
    hunks = branch_hunks(root)
    lines = [(path, line) for path, _, added in hunks for line in added]
    asked = asked_before(session, "tamper-asked")
    tampered = [(key, question) for key, question in weakened_assertions(root, hunks) if key not in asked]
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
        problems.append(f"Temporary {override} from the kit-validate skill is still there: delete it and run "
                        "`docker compose up -d` so the stack serves the primary checkout again.")
    deleted = git(["diff", review_stamp.merge_base(root), "--name-only", "--diff-filter=D"], root).splitlines()
    problems += [f"Test file deleted: {p}" for p in deleted if TEST_FILE.search(p)]

    if "verify" in off:
        return unmapped, tampered, problems, False
    verify_problems, verify_failed, _, _, stamp_error = run_verify(root)
    problems += verify_problems
    if stamp_error:  # logged, not blocking: a missing stamp only makes a skill run verify again
        log("stop_checks", "verify-stamp", payload, f"{root}: {stamp_error}")
    return unmapped, tampered, problems, verify_failed


def run_snapshot(overlay, root):
    """Run a copy of the overlay, beside it so paths relative to it still work: bash reads a script as it runs it,
    and another session editing the overlay mid-run made this one fail half-way."""
    with tempfile.NamedTemporaryFile(prefix=".verify-run-", dir=os.path.dirname(overlay)) as snapshot:
        with open(overlay, "rb") as source:
            snapshot.write(source.read())
        snapshot.flush()
        os.chmod(snapshot.name, 0o700)
        result = subprocess.run([snapshot.name], cwd=root, capture_output=True, text=True, errors="replace",
                                timeout=VERIFY_TIMEOUT)
    # A syntax error names the file bash ran: name the overlay the user edits instead.
    result.stdout, result.stderr = (out.replace(snapshot.name, overlay) for out in (result.stdout, result.stderr))
    return result


def run_verify(root):
    """Run the repo's verify in root, save its report and leave only this run's stamp: the only path to a verify
    stamp. Returns (problems, failed, output, whether it checked anything, stamp error or None)."""
    verify = os.path.expanduser(f"~/.agents/repos/{review_stamp.repo_name(root)}/verify")
    if not os.access(verify, os.X_OK):
        verify = AUTO_VERIFY

    def finish(problems, verdict, output="", checked=False, kind=None, checked_fingerprint=""):
        """Every run leaves a report and a stamp; one that failed, or checked nothing, leaves no verify stamp."""
        report_error = save_verify_report(root, verify, output, verdict)
        failed = verdict.startswith("FAIL")
        return (problems + [report_error] * bool(report_error), failed, output, checked,
                record_verify(root, kind, checked_fingerprint))

    if not os.access(verify, os.X_OK):
        return finish([f"No verify to run: neither {verify} nor ~/.agents/repos/<repo>/verify is executable."],
                      "FAIL (no verify to run)")
    # Stamped with the content verify started from: an edit made while it ran leaves the change unstamped.
    try:
        checked_fingerprint = in_checkout(root, review_stamp.fingerprint)
    except RuntimeError as error:
        # An earlier run's report and stamp must not outlive a run that couldn't check anything. A timeout is
        # raised, so the stop keeps the checkout's edits for the next one; another failure would only repeat.
        result = finish([f"Couldn't fingerprint the change, so verify can't stamp it: {error}"],
                        f"FAIL (couldn't fingerprint the change: {error})")
        if isinstance(error, review_stamp.GitTimeout):
            problems, stamp_error = result[0][1:], result[4]
            raise review_stamp.GitTimeout("; ".join([str(error), *problems, *[stamp_error] * bool(stamp_error)])) from None
        return result
    try:
        result = run_snapshot(verify, root) if verify != AUTO_VERIFY else subprocess.run(
            [verify], cwd=root, capture_output=True, text=True, errors="replace", timeout=VERIFY_TIMEOUT)
    except subprocess.TimeoutExpired as timeout:
        # On POSIX the partial output comes back as bytes even with text=True.
        output = "".join(part.decode(errors="replace") if isinstance(part, bytes) else part or ""
                         for part in (timeout.stdout, timeout.stderr))
        return finish([f"{verify} timed out after {VERIFY_TIMEOUT}s."], f"FAIL (timed out after {VERIFY_TIMEOUT}s)", output)
    except OSError as error:
        return finish([f"{verify} couldn't start: {error}"], f"FAIL (couldn't start: {error})")
    output = result.stdout + result.stderr
    if result.returncode != 0:
        return finish([f"{verify} failed (exit {result.returncode}):\n{output.strip()[-3000:]}"],
                      f"FAIL (exit {result.returncode})", output)
    checked = checked_something(result.stdout + "\n" + result.stderr)
    # Lets the skills skip re-running verify on a change it already passed. A run that checked nothing
    # gets its own stamp, so the flow moves on without reporting it as a pass.
    return finish([], "PASS" if checked else "PASS, but nothing was checked", output, checked,
                  "verify" if checked else "verify-empty", checked_fingerprint)


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
    try:
        toplevel = review_stamp.run_git(["rev-parse", "--show-toplevel"])
        if toplevel.returncode != 0:
            print(f"stop_checks.py verify: verify runs on a repository's changes; {toplevel.stderr.strip()}", file=sys.stderr)
            return 1
        root = toplevel.stdout.strip()
        problems, failed, output, checked, stamp_error = run_verify(root)
    except RuntimeError as error:  # a git call timed out or failed
        print(f"stop_checks.py verify: {error}", file=sys.stderr)
        return 1
    print(output, end="")
    for problem in problems + [stamp_error] * bool(stamp_error):
        print(problem.splitlines()[0], file=sys.stderr)
    # Warnings, not problems: the stop hook asks each once, and verify's verdict and stamp stay its own.
    try:
        tampered = [question for _, question in weakened_assertions(root, branch_hunks(root))]
    except RuntimeError as error:
        tampered = [f"couldn't look for weakened tests: {error}"]
    for question in tampered:
        print(f"warning: {question}", file=sys.stderr)
    if not failed and not checked:
        print("verify passed, but it checked nothing: no check printed a ran: line", file=sys.stderr)
    return 1 if problems or stamp_error else 0


if __name__ == "__main__":
    command = sys.argv[1:]
    sys.exit(leftover_overrides() if command == ["leftover-overrides"] else verify_here() if command == ["verify"] else main())
