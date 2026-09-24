#!/usr/bin/env python3
"""PreToolUse guard for shell commands, shared by Claude Code, Codex and Pi (via adapters/pi).

Blocks irreversible or outward-facing commands. It is a seatbelt against
agent mistakes, not a security boundary: a determined command can evade
regexes.
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import log  # noqa: E402

GIT = r"\bgit\s+(?:(?:-C|-c)\s+\S+\s+|--[\w-]+(?:=\S+)?\s+)*"
PROTECTED_BRANCHES = r"(main|master|trunk|develop|production|release(/[\w.-]+)?)"

RULES = [
    (GIT + r"push\b[^;&|]*\s(--force(?!-with-lease)\b|-f\b|\+\S)",
     "`git push --force`: rewrites remote history."),
    (GIT + rf"push\b[^;&|]*\s(\S+:)?{PROTECTED_BRANCHES}(\s|$)",
     "`git push` to main/master/trunk/develop/production/release: bypasses review."),
    (GIT + r"reset\s+--hard\b", "`git reset --hard`: discards uncommitted work."),
    (GIT + r"clean\s+-\w*f", "`git clean -f`: deletes untracked files."),
    (GIT + r"(checkout|restore)\s+(--\s+)?\.(\s|$)", "`git checkout .` / `git restore .`: discards all uncommitted changes."),
    (GIT + r"branch\s+-D\b", "`git branch -D`: force-deletes a branch."),
    (GIT + r"stash\s+(drop|clear)\b", "`git stash drop|clear`: deletes stashed work."),
    (r"\bgh\s+pr\s+merge\b", "`gh pr merge`: merging is a human decision."),
    (r"\bgh\s+repo\s+(delete|archive)\b", "`gh repo delete|archive`: deletes or archives a repository."),
    (r"\bgh\s+release\s+(create|delete)\b", "`gh release create|delete`: publishes or deletes a release."),
    (r"\bgh\s+api\b[^;&|]*(-X|--method)\s*DELETE\b", "`gh api -X DELETE`: deletes through the GitHub API."),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bdocker\s+push\b",
     "`npm|pnpm|yarn publish`, `twine upload`, `gem push`, `docker push`: publishes a package or image."),
    (r"(?i:\bDROP\s+(DATABASE|TABLE|SCHEMA)\b|\bTRUNCATE\s+TABLE\b)", "`DROP DATABASE|TABLE|SCHEMA`, `TRUNCATE TABLE`: destroys database data."),
    (r"\bwp\s+(db\s+(drop|reset|clean)|site\s+(empty|delete))\b", "`wp db drop|reset|clean`, `wp site empty|delete`: destroys WordPress data."),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "`curl … | sh`: pipes a download straight into a shell."),
    (r"(^|[;&|]\s*)sudo\b", "`sudo`: runs with root privileges."),
    (r"\b(make|npm\s+run|pnpm(\s+run)?|yarn(\s+run)?|composer(\s+run)?)\s+[\w:.-]*(deploy|release|sync_db|ssh_prod)",
     "`make|npm run|composer … deploy|release|sync_db|ssh_prod`: deploys, releases or touches production."),
    (r"\bchmod\s+(-R\s+)?777\b", "`chmod 777`: world-writable permissions."),
    (r"\.agents/approvals", "Touching `~/.agents/approvals`: approvals for shared-system writes must come from the user, not the agent."),
]

SAFE_RM_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


def dangerous_rm(command, cwd):
    """Recursive deletes outside the working directory or temp dirs."""
    for segment in re.split(r"[;&|]+", command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            continue
        if not tokens or os.path.basename(tokens[0]) != "rm":
            continue
        flags = "".join(t.lstrip("-") for t in tokens[1:] if t.startswith("-") and not t.startswith("--"))
        if "r" not in flags.lower() and "--recursive" not in tokens:
            continue
        home = os.path.expanduser("~")
        for target in (t for t in tokens[1:] if not t.startswith("-")):
            if "$" in target or "`" in target or "*" == target.strip("/"):
                return f"Recursive delete of an unresolved or wildcard path: {target}"
            path = os.path.realpath(os.path.join(cwd, os.path.expanduser(target)))
            if path in ("/", home) or cwd.startswith(path + os.sep) or path == cwd:
                return f"Recursive delete of {path}, which contains the working directory or home."
            inside_cwd = path.startswith(cwd + os.sep)
            inside_tmp = any(path == r or path.startswith(r + os.sep) for r in SAFE_RM_ROOTS)
            if not (inside_cwd or inside_tmp):
                return f"Recursive delete outside the working directory: {path}"
    return None


def pr_checkout(command, cwd):
    """The checkout `gh pr create` acts on: a `cd <dir>` before it, else the worktree holding --head."""
    before = re.split(r"\bgh\s+pr\s+create\b", command)[0]
    cds = re.findall(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", before)
    if cds:
        target = os.path.expanduser(cds[-1].strip("\"'"))
        return os.path.realpath(os.path.join(cwd, target))
    head = re.search(r"--head(?:=|\s+)(\S+)", command)
    if head:
        branch = head.group(1).split(":")[-1].strip("\"'")
        listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd,
                                 capture_output=True, text=True).stdout
        path = None
        for line in listing.splitlines():
            if line.startswith("worktree "):
                path = line[len("worktree "):]
            elif line == f"branch refs/heads/{branch}" and path:
                return path
    return cwd


def unreviewed_pr(command, cwd):
    """Opening a PR requires a self-review stamp for the exact current change."""
    # Only in command position: the phrase inside a quoted string, grep pattern or heredoc is not a PR.
    if not re.search(r"(?:^|[;&|(\n])\s*(?:\w+=\S*\s+)*gh\s+pr\s+create\b", command):
        return None
    cwd = pr_checkout(command, cwd)
    stamp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_stamp.py")

    def ok(*args):
        return subprocess.run([sys.executable, stamp, *args], cwd=cwd).returncode == 0

    if not ok("check", "--kind", "review"):
        return ("No self-review recorded for the current change. Run the self-review skill first "
                "(it ends with review_stamp.py write); any edit after the review needs a new one.")
    if ok("needs-validate") and not ok("check", "--kind", "validate"):
        return ("The change touches behavior but has no validation recorded for it. Run the validate skill "
                "(it ends with review_stamp.py write --kind validate); any edit after validating needs a new run.")
    return None


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if isinstance(command, list):
        command = " ".join(command)
    if not command:
        return 0
    cwd = os.path.realpath(payload.get("cwd") or os.getcwd())

    reason = dangerous_rm(command, cwd) or unreviewed_pr(command, cwd)
    if not reason:
        for pattern, why in RULES:
            if re.search(pattern, command):
                reason = why
                break
    if not reason:
        return 0

    log("guard_bash", "deny", payload, f"{reason} | {command[:200]}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked by ~/.agents/hooks/guard_bash.py: {reason} "
                "If this is really needed, stop and ask the user to run it themselves."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
