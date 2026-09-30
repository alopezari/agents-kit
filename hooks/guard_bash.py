#!/usr/bin/env python3
"""PreToolUse guard for shell commands, shared by Claude Code, Codex and Pi (via adapters/pi).

Blocks irreversible or outward-facing commands. It is a seatbelt against
agent mistakes, not a security boundary: a determined command can evade
regexes. Records the checkout of each allowed `git push`, so the stop hook
can follow that commit's CI.
"""
import json
import os
import re
import shlex
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import harness, log  # noqa: E402
import private_terms  # noqa: E402

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
MARKER_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks")

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
        home = os.path.realpath(os.path.expanduser("~"))
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


RUNS_QUOTED_TEXT = re.compile(r"\beval\b|\b(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*-\w*c\b")


def cd_into(cwd, target):
    """Where `cd <target>` leaves a shell that was in cwd. A cd that fails leaves it where it was."""
    path = os.path.normpath(os.path.join(cwd, os.path.expanduser(target.strip("\"'"))))
    return path if os.path.isdir(path) and os.access(path, os.X_OK) else cwd


def quoted_spans(command):
    """(start, end) of each quoted string in the command, quotes included; an unclosed one runs to the end.
    Heredoc bodies aren't skipped: an apostrophe in one can pair with a later quote."""
    spans, i, n = [], 0, len(command)
    while i < n:
        c = command[i]
        if c == "\\":
            i += 2
        elif c in "'\"":
            k = i + 1
            while k < n and command[k] != c:
                k += 2 if c == '"' and command[k] == "\\" else 1
            spans.append((i, min(k + 1, n)))
            i = k + 1
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):  # a comment's quotes open nothing
            end = command.find("\n", i)
            i = n if end < 0 else end
        else:
            i += 1
    return spans


def pr_checkout(command, cwd):
    """The checkout `gh pr create` or `gh pr ready` acts on: a `cd <dir>` before it, else the worktree holding --head."""
    before = re.split(r"\b" + GH_PR + r"(?:create|ready)\b", command)[0]
    cds = re.findall(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", before)
    runs_in = cwd
    for target in cds:
        runs_in = cd_into(runs_in, target)
    if cds:
        return os.path.realpath(runs_in)
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


def opened(command):
    """The command with every quote read as ; when eval or sh -c runs quoted text as code."""
    return re.sub(r"['\"]", ";", command) if RUNS_QUOTED_TEXT.search(command) else command


def shell_code(command):
    """The command with text that can't run blanked out (same length): quoted strings and heredoc bodies.
    Blanking uses x, not spaces, so a VAR="a b"c prefix still reads as one word. Where bash would run $(...) or
    backticks, or read <<\\ or $'...', the command is returned whole (opened()): parsing those is where a real gh would hide,
    and a false match only blocks. Inside single quotes, comments and quoted-delimiter heredocs they are only text."""
    if re.search(r"\$(?:\\\n)+[('`]", command):  # bash joins $\<newline>( back into $(
        return opened(command)
    out, n, i, heredocs = list(command), len(command), 0, []

    def in_brackets(k):  # arr[x<<2] is a shift
        line = command[command.rfind("\n", 0, k) + 1:k]
        return line.rfind("[") > line.rfind("]")

    def blank(start, end):
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = "x"

    while i < n:
        c = command[i]
        if c == "\\":
            i += 2
        elif c == "`" or command.startswith("$(", i) or command.startswith("$'", i):
            return opened(command)
        elif c in "'\"":
            k = i + 1
            while k < n and command[k] != c:
                if c == '"' and (command[k] == "`" or command.startswith("$(", k)):
                    return opened(command)
                k += 2 if c == '"' and command[k] == "\\" else 1
            blank(i + 1, min(k, n))
            i = k + 1
        elif c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            # Skipped, not blanked: a quote or << in a comment opens nothing, and a misread # hides nothing.
            end = command.find("\n", i)
            i = n if end < 0 else end
        elif command.startswith("((", i):  # arithmetic, where << is a shift
            close = command.find("))", i + 2)
            i = n if close < 0 else close + 2
        elif command.startswith("<<", i) and not command.startswith("<<<", i) and not in_brackets(i):
            if re.match(r"<<-?[ \t]*\\", command[i:]):
                return opened(command)
            m = re.match(r"<<(-?)[ \t]*(?:(['\"])([^'\"\n]+)\2|([A-Za-z_][\w-]*))", command[i:])
            if m and i + m.end() < n and command[i + m.end()] not in " \t\n;&|<>)":
                return opened(command)  # <<'EOF'x or <<EOF'x': bash's delimiter is EOFx, and quoting any part makes the body literal
            if m:
                heredocs.append((m.group(3) or m.group(4), bool(m.group(1)), bool(m.group(2))))
            i += m.end() if m else 2
        elif c == "\n" and heredocs:
            k = i + 1
            while heredocs and k < n:
                end = command.find("\n", k)
                end = n if end < 0 else end
                word, tabs, literal = heredocs[0]
                if (command[k:end].lstrip("\t") if tabs else command[k:end]) == word:
                    heredocs.pop(0)
                elif not literal and ("`" in command[k:end] or "$(" in command[k:end]):
                    return opened(command)
                else:
                    blank(k, end)
                k = end + 1
            i = k
        else:
            i += 1
    masked = "".join(out)
    return re.sub(r"['\"]", ";", command) if RUNS_QUOTED_TEXT.search(masked) else masked


# What can stand before a command's name and still run it: VAR=value assignments, and wrappers that run their
# arguments, with the options that take a value. Each piece matches one way only: an ambiguous one backtracks
# exponentially on a long env line and runs past the hook's timeout.
ASSIGNMENT = r"""\w+=(?:"[^"]*"|'[^']*'|[^\s"'])*\s+"""
PREFIXES = (r"(?:" + ASSIGNMENT + r"|command\s+(?:-p\s+|--\s+)*|nohup\s+(?:--\s+)?"
            r"|exec\s+(?:-a(?:\s+\S+|\S+)\s+|-[cl]+\s+|--\s+)*|time\s+(?:-[fo](?:\s+\S+|\S+)\s+|-[pv]+\s+|--\s+)*"
            r"|env\s+(?:-[uCS](?:\s+\S+|\S+)\s+|--(?:unset|chdir)=\S+\s+|-[i0v]+\s+|--\s+|-\s+)*)*")
# bin/evidence <id> runs the rest of its line. A one-character lookbehind, with its path cut in Python: a path
# pattern tried after every separator was quadratic on a long line.
EVIDENCE_RUNNER = re.compile(r"(?<![^\s;&|(`/])evidence\s+(['\"]?)[A-Za-z0-9][\w.-]*\1\s+")


def without_evidence_runner(command):
    """The command as the shell runs it once bin/evidence steps aside: the checks that look for a command at the start
    of one must see it there."""
    kept, last = [], 0
    for match in EVIDENCE_RUNNER.finditer(command):
        start = match.start()
        while start > last and command[start - 1] not in " \t\n;&|(`":
            start -= 1
        kept.append(command[last:start])
        last = match.end()
    return "".join(kept) + command[last:]
ENV_CLEARED = re.compile(r"\benv\s+(?:\S+\s+)*?(?:-[0v]*i[0v]*|--ignore-environment|-)\s")


# Where a command starts: after a separator, or after a shell keyword or brace (`then gh pr ready`, `{ gh pr ready; }`).
COMMAND_START = r"(?:^|[;&|(\n`{]|\b(?:then|do|else|elif|if|while|until)\b|!)\s*"
# gh's --repo may come before or after `pr`, attached or not: `gh -R owner/repo pr ready 12`, `gh pr -Rowner/repo ready`.
REPO_FLAG = r"(?:(?:-R|--repo)(?:=|\s+)?\S+\s+)?"
GH_PR = r"gh\s+" + REPO_FLAG + r"pr\s+" + REPO_FLAG


def pr_args(command, match, group):
    """The words of a matched PR command's arguments as the shell passes them, without redirections or a comment:
    a quoted "12" is 12, a quoted title is one word, and `> ready.log`, `2>&1` or `# --draft` isn't an argument."""
    text = command[match.start(group):match.end(group)]  # the raw text: shell_code() masks quoted words
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    try:
        words = list(lexer)
    except ValueError:
        words = text.split()
    args, skip = [], False
    for word in words:
        if skip:
            skip = False
        elif re.fullmatch(r"\d*(?:>>?|<|>&|<&)", word):
            skip = True  # its target is the next word
        elif not re.match(r"\d*[<>]", word):
            args.append(word)
    return args


def unreviewed_pr(command, cwd):
    """Opening a PR requires a self-review stamp for the exact current change, a validate stamp for a behavior
    change, and, unless it's a draft, the staging steps before the merge passed."""
    creates = list(re.finditer(COMMAND_START + PREFIXES + GH_PR + r"create\b([^;&|\n]*)", shell_code(command)))
    if not creates:
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
    if any(not {"--draft", "-d"} & set(pr_args(command, match, 1)) for match in creates):
        return staging_reason(cwd)  # a draft may wait for staging; any create in the command that isn't one may not
    return None


def staging_reason(cwd):
    """Why `review_stamp.py staging` holds the PR in cwd, or None when it passes; a check that fails silently holds it too."""
    stamp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_stamp.py")
    result = subprocess.run([sys.executable, stamp, "staging"], cwd=cwd, capture_output=True, text=True)
    if result.returncode == 0:
        return None
    return result.stdout.strip() or f"Couldn't read the staging results: {result.stderr.strip()[-300:] or 'no output'}"


def unready_pr(command, cwd):
    """`gh pr ready` marks a PR ready for review: its staging steps before the merge must have passed, with evidence."""
    code = shell_code(command)
    readies = [m for m in re.finditer(COMMAND_START + PREFIXES + r"(" + GH_PR + r")ready\b([^;&|\n]*)", code)
               if "--undo" not in pr_args(command, m, 2)]
    if not readies:
        return None
    cwd = pr_checkout(command, cwd)
    if not os.path.isdir(cwd):  # a hook that crashes lets the command through
        return f"`gh pr ready`: its checkout {cwd} doesn't exist, so its staging results can't be checked."
    here = subprocess.run(["git", "branch", "--show-current"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    targets = set()
    for match in readies:
        if re.search(r"(?:^|\s)(?:-R|--repo)", match.group(1) + " " + match.group(2)):
            return ("`gh pr ready --repo`: the staging results are checked in a checkout of the PR's branch. "
                    "Run it there without --repo.")
        targets.add(next((word for word in pr_args(command, match, 2) if not word.startswith("-")), None))
    targets.discard(None)
    if len(targets) > 1:  # each lookup may take its whole timeout, and the hook's own runs out at 10 s
        return "One `gh pr ready` per command: run each PR's from its own branch's checkout."
    for target in targets:
        # A number, URL or branch names the PR; its staging results live under its branch.
        try:
            branch = subprocess.run(["gh", "pr", "view", target, "--json", "headRefName", "-q", ".headRefName"],
                                    cwd=cwd, capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            branch = ""
        if not branch or branch != here:
            return (f"`gh pr ready {target}`: can't check its staging results from here (its branch is "
                    f"{branch or 'unknown'}, this checkout is on {here or 'no branch'}). "
                    "Run it from that branch's checkout.")
    return staging_reason(cwd)


# The one form both this check and pr_checkout() read the same way: a literal absolute path (no expansion or glob),
# and && so gh only runs where the cd succeeded.
ABSOLUTE_CD_FIRST = re.compile(r"""cd[ \t]+((?:~[\w.-]*)?/[^\s;&|()$`'"\\*?\[]*|"/[^"$`\\]*"|'/[^']*')[ \t]*&&""")
# Anywhere, not only in command position: a wrapper, `then` or `{` in front must not hide it, and a false match only
# asks for the cd form.
GH_PR_WORDS = re.compile(r"\b" + GH_PR + r"(?:create|edit|ready)\b")
CD_WORD = re.compile(r"(?<![\w/.-])(?:cd|pushd|popd)(?![\w/.-])")


def pr_checkout_unknown(command, payload):
    """Codex runs a command in its per-call workdir but sends the session's cwd (openai/codex#33986), so the PR
    checks would judge the wrong checkout unless the command starts by cd-ing to an existing absolute path, and
    neither cds again nor backgrounds the chain. Guards against a model's slip, not a hostile one."""
    code = shell_code(command)
    if harness(payload) != "codex" or not GH_PR_WORDS.search(code):
        return None
    first = ABSOLUTE_CD_FIRST.match(command)
    if first and os.path.isdir(os.path.expanduser(first.group(1).strip("\"'"))):
        rest = code[first.end():]
        if not CD_WORD.search(rest) and not re.search(r"(?<![&>])&(?![&>])", rest):
            return None
    return ("Codex doesn't tell hooks the workdir a command runs in (openai/codex#33986), so this PR command can't be "
            "checked against the right checkout. Start the command with `cd /absolute/path/to/checkout && ` (an existing, "
            "literal path; no other cd, no `&`), and don't set a workdir.")


def repo_id(repo, default_host=None):
    """(host, owner/repo) from OWNER/REPO, HOST/OWNER/REPO, a URL (a PR's too) or a git remote URL."""
    path = re.sub(r"^(?:\w+://)?(?:[^@/]+@)?", "", repo.strip()).replace(":", "/")
    path = re.sub(r"/pull/\d+.*$", "", path).rstrip("/")
    parts = re.sub(r"\.git$", "", path).lower().split("/")
    return (parts[-3] if len(parts) > 2 else default_host), "/".join(parts[-2:])


def targets_kit(repo, host, cwd):
    """Whether a gh command acts on the kit's own repository: its --repo (on GH_HOST, if set), else the checkout it
    runs in."""
    if repo:
        origin = subprocess.run(["git", "-C", KIT, "remote", "get-url", "origin"], capture_output=True, text=True).stdout
        if not origin.strip():
            return False
        (kit_host, kit_slug), (host, slug) = repo_id(origin), repo_id(repo, host)
        return slug == kit_slug and host in (None, kit_host)
    def common_dir(path):
        return subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
                              capture_output=True, text=True).stdout.strip()
    # KIT/.git is a file when ~/.agents is itself a worktree (a verify testing a branch): compare common dirs.
    # Only KIT's own repository: a ~/.agents that isn't one could sit inside another (a dotfiles repo in HOME).
    common, kit = common_dir(cwd), common_dir(KIT) if os.path.exists(os.path.join(KIT, ".git")) else ""
    return bool(common and kit) and os.path.realpath(common) == os.path.realpath(kit)


def pr_command_args(command, start):
    """The arguments of the gh command starting at `start`, up to the next shell operator."""
    lexer = shlex.shlex(command[start:], posix=True, punctuation_chars=";&|\n")
    # No comments: bash starts one only after whitespace (`abc#x` is one word); keeping `#` checks more, never less.
    lexer.whitespace, lexer.whitespace_split, lexer.commenters = " \t\r", True, ""
    args = []
    for token in lexer:
        if token.strip(";&|\n") == "":
            break
        args.append(token)
    return args


def private_terms_in_kit_pr(command, cwd, env=os.environ):
    """The kit is public: a pull request to it must not carry a profile's private terms."""
    code = shell_code(command)
    spans, checked = quoted_spans(command), set()

    def quote_around(pos):
        return next(((start, end) for start, end in spans if start < pos < end), None)

    for match in re.finditer(r"(?:^|[;&|(\n`])\s*(" + PREFIXES + r")" + GH_PR + r"(?:create|edit)\b", code):
        before = command[:match.start(1)]
        # gh reads a relative --body-file from where it runs, not the --head worktree pr_checkout() may pick.
        # The cds are found in the code, where a quote may read as ; around eval's argument, and read from the command.
        runs_in = cwd
        for cd in re.finditer(r"(?:^|[;&|\n]\s*)(cd)\s+(\"[^\"]+\"|'[^']+'|;[^;\n]+;|[^\s;&|]+)", code[:match.start(1)]):
            if not quote_around(cd.start(1)):
                runs_in = cd_into(runs_in, command[cd.start(2):cd.end(2)])
        exported = dict(re.findall(r"(?:^|[;&|\n]\s*)(?:export\s+)?(GH_REPO|GH_HOST)=([^\s;&|]+)(?=\s*(?:[;&|\n]|$))",
                                   before))
        wrapper = quote_around(match.end(1))
        if wrapper:
            # eval '...' or sh -c '...': check the quoted script as a command of its own, from where it starts.
            if wrapper not in checked:
                checked.add(wrapper)
                start, end = wrapper
                # The script is the whole word ('gh pr edit -t 'ACME-4 is one), and every argument of eval, which joins them.
                stops = "\n;&|)" if re.search(r"\beval\s+\$?$", command[:start]) else " \t\n;&|)"
                while end < len(command) and command[end] not in stops:
                    quoted = quote_around(end + 1) if command[end] in "'\"" else None
                    end = quoted[1] if quoted else end + (2 if command[end] == "\\" else 1)
                prefix = len(re.split(r"[;&|\n]", code[:start])[-1])  # GH_REPO=x sh -c '...'
                exported.update(re.findall(r"\b(GH_REPO|GH_HOST)=([^\s;&|]+)", command[start - prefix:start]))
                try:
                    script = " ".join(shlex.split(command[start:end]))
                except ValueError:  # an unclosed quote
                    script = command[start + 1:end]
                reason = private_terms_in_kit_pr(script, runs_in, {**env, **exported})
                if reason:
                    return reason
            continue
        exported.update(re.findall(r"\b(GH_REPO|GH_HOST)=(\S+)", command[match.start(1):match.end(1)]))
        try:
            args = pr_command_args(command, match.end(1))
        except ValueError:
            continue  # unbalanced quotes: text inside a heredoc or string, not a command gh would run
        prefix = command[match.start(1):match.end(1)]
        inherited = {} if ENV_CLEARED.search(prefix) else dict(env)  # env -i and -u drop what gh would inherit
        for name in re.findall(r"(?:-u\s*|--unset=)(\w+)", prefix) if "env" in prefix else []:
            inherited.pop(name, None)
        repo, host = (exported.get(k, inherited.get(k, "")).strip("\"'") or None for k in ("GH_REPO", "GH_HOST"))
        values, files = [], []
        for i, arg in enumerate(args[1:], 1):  # gh's -R may come before `pr`
            flag, eq, inline = arg.partition("=")
            if re.fullmatch(r"-[tbFR]..*", arg):  # -tTitle: gh accepts short flags with the value attached
                flag, eq, inline = arg[:2], "=", arg[2:]
            value = inline if eq else (args[i + 1] if i + 1 < len(args) else "")
            if flag in ("-R", "--repo"):
                repo = value
            elif flag in ("-t", "--title", "-b", "--body"):
                values.append(value)
            elif flag in ("-F", "--body-file"):
                files.append(value)
            elif re.match(r"https?://\S+/pull/\d+", arg):
                repo = arg
        if not (values or files):
            continue
        if not ("$" in (repo or "") or targets_kit(repo, host, pr_checkout(command[match.start(1):], runs_in))):
            continue
        # Checked before the shell runs, so fail closed on text only the shell will produce.
        if any(re.search(r"[$`]", v) for v in values):
            return ("This pull request's title or description comes from a shell expansion, which can't be checked "
                    "for a profile's private terms. Pass the text literally or in a --body-file.")
        for path in files:
            try:
                if path == "-" or os.path.basename(path) in before:  # stdin, or a file this command writes first
                    raise OSError
                values.append(open(os.path.join(runs_in, os.path.expanduser(path))).read())
            except (OSError, ValueError):  # ValueError: a path open() rejects, like one with a NUL
                return (f"This pull request's description file ({path}) can't be read yet, so it can't be checked for "
                        "a profile's private terms. Write the file first, then run gh in a separate command.")
        terms = private_terms.found("\n".join(values))
        if terms:
            return (f"This pull request's title or description names {', '.join(terms)}, which a profile marks as "
                    "private, and the kit is public. Rewrite it without them.")
    return None


def pushed_from(command, cwd):
    """The directory each `git push` in the command runs in, following earlier `cd`s outside a closed subshell and
    every `git -C`."""
    code, dirs = shell_code(command), []

    def word(start, end):
        return command[start:end].replace("\\ ", " ")

    for push in re.finditer(GIT + r"push\b", code):
        runs_in = cwd
        for cd in re.finditer(r"(?:^|[;&|(\n])\s*cd\s+((?:\\ |[^\s;&|)])+)", code[:push.start()]):
            between = code[cd.end():push.start()]
            if between.count(")") <= between.count("("):
                runs_in = cd_into(runs_in, word(cd.start(1), cd.end(1)))
        for option in re.finditer(r"-C\s+((?:\\ |[^\s;&|)])+)", code[push.start():push.end()]):
            runs_in = cd_into(runs_in, word(push.start() + option.start(1), push.start() + option.end(1)))
        dirs.append(runs_in)
    return dirs


def record_push(command, cwd, payload):
    """Note the checkout and time of each allowed `git push`, so the stop hook follows the CI of what it pushed."""
    roots = {subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=d, capture_output=True, text=True).stdout.strip()
             for d in pushed_from(command, cwd)} - {""}
    session = re.sub(r"[^\w-]", "_", str(payload.get("session_id") or "unknown"))
    if roots:
        try:
            os.makedirs(MARKER_DIR, exist_ok=True)
            with open(os.path.join(MARKER_DIR, f"{session}.pushed"), "a") as fh:
                fh.writelines(f"{root}\t{time.time()}\n" for root in sorted(roots))
        except OSError:
            pass  # the push still runs; only the CI follow-up is lost


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
    command = without_evidence_runner(command)

    reason = (pr_checkout_unknown(command, payload) or dangerous_rm(command, cwd) or private_terms_in_kit_pr(command, cwd)
              or unreviewed_pr(command, cwd) or unready_pr(command, cwd))
    if not reason:
        for pattern, why in RULES:
            if re.search(pattern, command):
                reason = why
                break
    if not reason:
        record_push(command, cwd, payload)
        return 0

    log("guard_bash", "deny", payload, f"{reason} | {command[:200]}")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"Blocked by ~/.agents/hooks/guard_bash.py: {reason} "
                "Nothing in this command ran, including any steps chained before or after the blocked one. "
                "If this is really needed, stop and ask the user to run it themselves."
            ),
        }
    }))
    return 0


if __name__ == "__main__":
    sys.exit(main())
