#!/usr/bin/env python3
"""PreToolUse guard for shell commands, shared by Claude Code and Codex.

Blocks irreversible or outward-facing commands. It is a seatbelt against
agent mistakes, not a security boundary: a determined command can evade
regexes. It reads code, not text: a quoted string or heredoc that only a
reader, printer or interpreter takes as text doesn't count, unless a SQL
client is in the command; `rm` resolves variables the same command assigns.
Records the checkout of each allowed `git push`, so the stop hook can follow
that commit's CI.
"""
import bisect
import functools
import io
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import time
import tokenize
import unicodedata

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hooklog import harness, log  # noqa: E402
import design_files  # noqa: E402
import phase_switches  # noqa: E402
import private_terms  # noqa: E402

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
MARKER_DIR = os.path.join(os.environ.get("TMPDIR", "/tmp"), "agent-hooks")

# What can stand before a command's name and still run it: VAR=value assignments, and wrappers that run their
# arguments, with the options that take a value. Each piece matches one way only: an ambiguous one backtracks
# exponentially on a long env line and runs past the hook's timeout.
ASSIGNMENT = r"""\w+=(?:"[^"]*"|'[^']*'|[^\s"';&|])*\s+"""
PREFIXES = (r"(?:" + ASSIGNMENT + r"|command\s+(?:-p\s+|--\s+)*|nohup\s+(?:--\s+)?"
            r"|exec\s+(?:-a(?:\s+\S+|\S+)\s+|-[cl]+\s+|--\s+)*"
            r"|time\s+(?:-[fo](?:\s+\S+|\S+)\s+|--(?:output|format)(?:=|\s+)\S+\s+|-[pva]+\s+|--(?:portability|verbose|append)\s+|--\s+)*"
            r"|env\s+(?:-[i0v]*[uCS](?:\s+\S+|\S+)\s+|--(?:unset|chdir|split-string)(?:=|\s+)\S+\s+|-[i0v]+\s+"
            r"|--(?:ignore-environment|null|debug)\s+|--\s+|-\s+)*)*")
# Where a command starts: after a separator, and any shell keywords after it (`; then gh pr ready`, `{ gh pr ready; }`).
# [^\S\n], not \s: a newline starts a command itself, and \s there would make each line re-read every line after it.
COMMAND_START = r"(?:^|[;&|(\n`{])[^\S\n]*(?:(?:then|do|else|elif|if|while|until)[^\S\n]+|![^\S\n]*)*"

GIT = r"\bgit\s+(?:(?:-C|-c)\s+\S+\s+|--[\w-]+(?:=\S+)?\s+)*"
PROTECTED_BRANCHES = r"(main|master|trunk|develop|production|release(/[\w.-]+)?)"
DATABASE_STATEMENT = r"\bDROP{space}+(DATABASE|TABLE|SCHEMA)\b|\bTRUNCATE{space}+TABLE\b"
DATABASE_DESTROY = "(?i:" + DATABASE_STATEMENT.format(space=r"\s") + ")"
DATABASE_ALLOW_LINE = re.compile(r"allow[ \t]+(" + DATABASE_STATEMENT.format(space="[ \t]") + ")", re.I)
ACTION_ALLOW_LINE = re.compile(rf"allow[ \t]+(gh[ \t]+pr[ \t]+merge|git[ \t]+push[ \t]+{PROTECTED_BRANCHES})", re.I)
MERGE = r"\bgh\s+pr\s+merge\b"
PROTECTED_PUSH = GIT + rf"push\b[^;&|]*\s(\S+:)?{PROTECTED_BRANCHES}(\s|$)"
RULES = [
    (GIT + r"push\b[^;&|]*\s(--force(?!-with-lease)\b|-f\b|\+\S)",
     "`git push --force`: rewrites remote history."),
    (PROTECTED_PUSH, "`git push` to main/master/trunk/develop/production/release: bypasses review. Instead of running "
                     "it themselves, the user can allow it for that branch until their next message, by asking for it "
                     "in plain words (`push to main`) or by starting their message with the line `allow git push main`. "
                     "A first push to a remote with no branches yet needs no approval."),
    (GIT + r"reset\s+--hard\b", "`git reset --hard`: discards uncommitted work."),
    (GIT + r"clean\s+-\w*f", "`git clean -f`: deletes untracked files."),
    (GIT + r"checkout(\s+-[\w=-]+)*\s+(--\s+)?\./?(\s|$)",
     "`git checkout .`: discards all uncommitted changes."),
    # --staged alone only unstages; with --worktree it discards like the rest
    (GIT + r"restore(?=[^;&|\n]*\s(--worktree|-[a-zA-Z]*W[a-zA-Z]*)(\s|$)"
     r"|(?![^;&|\n]*\s(--staged|-[a-zA-Z]*S[a-zA-Z]*)(\s|$)))"
     r"(\s+(-s|--source)\s+[^\s;&|]+|\s+-[\w=~^./@{}-]+)*\s+(--\s+)?\./?(\s|$)",
     "`git restore .`: discards all uncommitted changes."),
    (GIT + r"branch\s+-D\b", "`git branch -D`: force-deletes a branch."),
    (GIT + r"stash\s+(drop|clear)\b", "`git stash drop|clear`: deletes stashed work."),
    (MERGE, "`gh pr merge`: merging is the user's decision. Instead of running it themselves, the user can allow it "
            "until their next message, by asking for it in plain words (`merge it`, `mergéalo`) or by starting their "
            "message with the line `allow gh pr merge`. `--admin` stays theirs."),
    (r"\bgh\s+repo\s+(delete|archive)\b", "`gh repo delete|archive`: deletes or archives a repository."),
    (r"\bgh\s+release\s+(create|delete)\b", "`gh release create|delete`: publishes or deletes a release."),
    (r"\bgh\s+api\b[^;&|]*(-X|--method)\s*DELETE\b", "`gh api -X DELETE`: deletes through the GitHub API."),
    (r"\b(npm|pnpm|yarn)\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bdocker\s+push\b",
     "`npm|pnpm|yarn publish`, `twine upload`, `gem push`, `docker push`: publishes a package or image."),
    (DATABASE_DESTROY, "`DROP DATABASE|TABLE|SCHEMA`, `TRUNCATE TABLE`: destroys database data. Instead of running it "
                       "themselves, the user can allow one until their next message, by sending a message that only "
                       "asks for it in plain words (`drop the test databases`) or that starts with the line "
                       "`allow DROP DATABASE` (or the statement needed)."),
    (r"\bwp\s+(db\s+(drop|reset|clean)|site\s+(empty|delete))\b", "`wp db drop|reset|clean`, `wp site empty|delete`: destroys WordPress data."),
    (r"\b(curl|wget)\b[^|;&]*\|\s*(sudo\s+)?(ba|z)?sh\b", "`curl … | sh`: pipes a download straight into a shell."),
    (COMMAND_START + PREFIXES + r"sudo\b", "`sudo`: runs with root privileges."),
    (r"\b(make|npm\s+run|pnpm(\s+run)?|yarn(\s+run)?|composer(\s+run)?)\s+[\w:.-]*(deploy|release|sync_db|ssh_prod)",
     "`make|npm run|composer … deploy|release|sync_db|ssh_prod`: deploys, releases or touches production."),
    (r"\bchmod\s+(-R\s+)?777\b", "`chmod 777`: world-writable permissions."),
]

# Whether a command, or a word that is one (`bash -c '…'`), may hold the call: read its words only then.
APPROVE_GRANT = re.compile(r"(?<![\w-])approve\b[\s\S]*\b(grant|once)\b")
GRANTS_APPROVAL = ("`bin/approve grant|once`: an approval comes from the user, in their message or in the dialog Claude "
                   "Code shows when a guard blocks a call, not from the agent.")
SWITCH_CLI = re.compile(r"(?<![\w-])phase_switches\b[\s\S]*\bset\b")
# Only `$(cat <<EOF …)`, the form commit and PR bodies take: a heredoc fed to a shell is run.
HEREDOC_BODY = re.compile(r"\$\(cat[ \t]+<<-?[ \t]*(['\"])(\w+)\1[^\n]*\n[\s\S]*?\n[ \t]*\2(?=[\s)]|$)")
SEPARATORS = set(";&|()")
WRITERS = {"rm", "rmdir", "mv", "cp", "touch", "mkdir", "tee", "ln", "chmod", "chown", "rsync", "install", "dd",
           "truncate", "unlink", "shred"}
APPROVALS_WHY = ("Touching `~/.agents/approvals`: approvals for shared-system writes must come from the user, not the "
                 "agent.")
SWITCHES_PHASE = ("`phase_switches.py set`: a phase is switched by the user, with a `phase off|on` line in their message or "
                  "`/flow off|on` in Claude Code, not by the agent.")
REDIRECT = re.compile(r"&?[<>]+[&|]?")  # >, >>, 2>&1's >&, &>, >|, <<
CONTINUATION = "\\\n"


def shell_words(command):
    """The words the shell hands the commands in `command`: a quoted note can hold `;`, `|` or a newline, a line
    continuation joins what it splits, and a redirect (`> /dev/null`, `2>&1`) is no word."""
    command = command.replace(CONTINUATION, "")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:  # an unclosed quote: no shell would run it, so read its plain words
        return [word.strip("'\"") for word in command.split()]
    kept, target = [], False
    for i, word in enumerate(words):
        if target:
            target = False
        elif REDIRECT.fullmatch(word):
            target = True
        elif not (word.isdigit() and i + 1 < len(words) and REDIRECT.fullmatch(words[i + 1])):  # the 2 of 2> log
            kept.append(word)
    return kept


def runs_call(command, is_program, takes):
    """Whether the shell runs a program is_program(word, the word before) names, with arguments takes(args) accepts,
    in the command or in a word the shell may run as one (`bash -c '…'`, `eval`, `… | sh`, `$(…)`). A word is text only
    when a known program just reads or prints it: a reader's pattern, git's or gh's arguments (a message, a title), a
    `for … in` list, what echo or printf prints where no pipe or `$(…)` takes it; even there a `$(…)` or backticks the
    shell would run is read. A quoted heredoc written to a file the command doesn't run, or a `$(cat <<'EOF' …)` body,
    is only text."""
    text = HEREDOC_BODY.sub("$(cat", without_prose(command)).replace(CONTINUATION, "")
    words = shell_words(text.replace("\n", ";"))  # a new line starts a new command, whatever the last one was
    substitutes = bool(re.search(r"\$\(|`", shell_code(command)))  # none in single quotes: there they're text
    for i, word in enumerate(words):
        before = words[i - 1] if i else ""
        rest = list(itertools.takewhile(lambda w: not set(w) <= SEPARATORS, words[i + 1:]))
        if is_program(word, before) and takes(rest):
            return True
        simple_command = list(itertools.takewhile(lambda w: not set(w) <= SEPARATORS, reversed(words[:i])))[::-1]
        program = next((os.path.basename(w) for w in simple_command if not re.fullmatch(r"\w+=.*", w)), "")
        end = (words[i + 1 + len(rest):i + 2 + len(rest)] or [""])[0]
        output_used = (end.startswith("|") and end != "||") or end.startswith(")")  # a pipe, or $(…) around it
        git_runs = program == "git" and (before in ("-x", "--exec") or "=!" in word or word.startswith("!"))  # an alias
        text = program in READERS or program == "for" or (program in ("git", "gh") and not git_runs) or (program in ("echo", "printf") and not output_used)
        if word != command and (not text or (substitutes and ("$(" in word or "`" in word))) \
                and runs_call(word, is_program, takes):
            return True
    return False


def switches_phase(command):
    """`phase_switches.py set`, or `python3 -m phase_switches set`, in the command or in a word the shell runs as one."""
    if not SWITCH_CLI.search(command.replace(CONTINUATION, "")):
        return None
    cli = (lambda word, before: re.search(r"(?<![\w-])phase_switches\.py$", word)
           or (before == "-m" and re.fullmatch(r"(?:hooks\.)?phase_switches", word)))
    return SWITCHES_PHASE if runs_call(command, cli, lambda args: args[:1] == ["set"]) else None


def grants_approval(command):
    """`bin/approve grant|once` in the command, or in a word the shell runs as one: only the mod's dialog runs it."""
    if not APPROVE_GRANT.search(command.replace(CONTINUATION, "")):
        return None
    grant = lambda args: args[:1] in (["grant"], ["once"])  # noqa: E731
    return GRANTS_APPROVAL if runs_call(command, lambda word, _: re.search(r"(?<![\w-])approve$", word), grant) else None


def touches_approvals(command, cwd, runs=False):
    """A path into ~/.agents/approvals, written out or relative to the working directory or to a `cd` in the command
    (`cd ~/.agents && touch approvals/linear`), or in code an interpreter runs (`bash -c`, `python3 -c`, `eval`). What
    a reader (grep, cat, ls…) or cd is given, a sentence, and the prose of a message or an echo, is only text; a bare
    name is a path only to a program that writes files; a redirect's target is always a path. In code that `runs`,
    any mention of the approvals counts: its readers and sentences are no shell's. In a heredoc script only an
    interpreter reads, a whitespace-free string literal is a path; one through variables the command assigns counts
    by where it points."""
    prose_free = without_prose(command)
    mentions = re.compile(r"\.agents/approvals")
    if runs and mentions.search(prose_free):
        return APPROVALS_WHY
    lines = prose_free.split("\n")
    bodies = None if RUNS_QUOTED_TEXT.search(prose_free) else read_heredocs(lines)
    if bodies is None and "<<" in prose_free and mentions.search(prose_free):
        return APPROVALS_WHY  # a heredoc it can't read may be run
    for start, end, kind, interpreter in bodies or []:
        body = "\n".join(lines[start:end])
        if mentions.search(body):
            # In a script only an interpreter reads, a path is a string literal on its own, not part of a longer text.
            literals = string_literals(body, interpreter) if kind == "script" else None
            if literals is None or any(mentions.search(s) and not re.search(r"\s", s) for s in literals):
                return APPROVALS_WHY
        if kind == "script":
            lines[start:end] = [""] * (end - start)
    shell = "\n".join(lines)
    seen = {}
    for segment, known in segments_with_values(shell):
        for m in VARIABLE.finditer(segment):
            seen.setdefault(m.group(1) or m.group(3), set()).add(known.get(m.group(1) or m.group(3), UNKNOWN))
    # A variable counts by its value only when every use sees the same one: used before it is set, it is unresolved.
    values = {name: found.pop() for name, found in seen.items() if len(found) == 1 and UNKNOWN not in found}
    text = re.sub(r"\$\{HOME\}|\$HOME\b", "~", shell.replace(CONTINUATION, "")).replace("\n", ";")
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = text.split()
    approvals = {os.path.normpath(design_files.APPROVALS_DIR), os.path.realpath(design_files.APPROVALS_DIR)}

    def inside(path):
        path = os.path.normpath(path)
        return any(path == a or path.startswith(a + os.sep) for a in approvals)
    places, program, redirected, read_from = {cwd}, None, False, False  # every directory a cd may have left it in
    for i, word in enumerate(words):
        if set(word) <= SEPARATORS or (set(word) <= SEPARATORS | {"<", ">"} and "(" in word):  # <(…), >(…)
            program = None
            continue
        if REDIRECT.fullmatch(word):
            redirected, read_from = ">" in word, "<" in word and ">" not in word
            continue
        if program is None and not redirected and not read_from and not re.fullmatch(r"\w+=.*", word) \
                and word not in ("command", "builtin", "env", "time", "nohup", "exec"):
            program = os.path.basename(word)
        code = [w for w in words[max(0, i - 2):i] if w != "--"][-1:]
        runs_word = (code and re.fullmatch(r"-\w*[ce]|--eval|eval", code[0])) or "$(" in word or "`" in word
        if runs_word and word != command and any(touches_approvals(word, place, runs=True) for place in places):
            return APPROVALS_WHY  # code the shell or an interpreter runs: bash -c [--], python3 -c, eval, $(…)
        if read_from:
            read_from = False
        elif program in ("cd", "pushd") and not redirected:
            if os.path.basename(word) != program and not word.startswith("-") and "$" not in word and "`" not in word:
                places |= {cd_into(place, word) for place in places}
        elif program not in READERS or redirected:
            for target in {word, word.split("=", 1)[-1]}:  # of=approvals/x, --target-directory=approvals
                if re.search(r"\s", target) and not re.match(r"~|\.{0,2}/|\$", target):
                    continue  # a sentence is no path, but a quoted path can hold spaces
                if mentions.search(target):
                    resolved = expand(target, values) if "$" in target else None
                    if resolved is None or re.search(r"[$`]", resolved):
                        return APPROVALS_WHY
                    target = resolved  # a path through variables counts by where it points
                target = os.path.expanduser(target)
                # A bare name is a path to a program that writes files, or to any program once a cd is in there.
                bare_is_path = program in WRITERS or redirected or any(map(inside, places))
                if "$" in target or "`" in target or ("/" not in target and not bare_is_path):
                    continue
                if any(inside(os.path.join(place, target)) for place in places):
                    return APPROVALS_WHY
        redirected = False
    return None


UNKNOWN = object()
SAFE_RM_ROOTS = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")


def dangerous_rm(command, cwd, run_by=None):
    """Recursive deletes outside the working directory or temp dirs, each judged from where the `cd`s before it
    leave the shell (a subshell's cd ends with it), with the variables segments_with_values() knows substituted.
    After a cd the guard can't resolve, a relative target is unresolved too. A quoted string the command runs as a
    command (bash -c, eval, trap) is judged the same way, run_by giving the directory and variables of its segment."""
    here, known = run_by or (cwd, None)  # here is None after an unresolvable cd
    # An unquoted heredoc writes \$T as $T: T=\$(mktemp -d) there makes a temp dir. Elsewhere a target with $ is
    # unresolved anyway.
    command = command.replace("\\$", "$")
    text, scripts = without_scripts(command)[0], set(strings_running_rm(command))
    # Found in the whole text: a string spanning lines spans segments too, and runs from the one it starts in.
    runs = [(a, b) for a, b in quoted_spans(text) if text[a + 1:b - 1] in scripts]
    run_files = files_run(text)
    lines = text.split("\n")
    heredocs = read_heredocs(lines) or ()
    spawning = {i for first, end, kind, _ in heredocs if kind == "spawns" for i in range(first, end)}
    # A body only written to files the command doesn't run is text: its trap or eval runs nowhere yet.
    written_only = {i for first, end, kind, _ in heredocs
                    if kind == "other" and (written := written_files(lines[first - 1])) and not written & run_files
                    for i in range(first, end)}
    # Code an interpreter runs (python3 -c '… os.system("rm …")') runs its strings wherever they stand in it.
    code_spawns = run_by is not None and bool(SPAWNS.search(text))
    outer, at, line, next_run = [], 0, 0, 0
    for segment, values in segments_with_values(text, known):
        start = text.find(segment, at)
        line += text.count("\n", at, start)
        at = start + len(segment)
        stripped = segment.strip()
        outer += [here] * (len(stripped) - len(stripped.lstrip("(")))
        try:  # the subshell's parentheses only: a quoted "(old)" in a name stays
            tokens = [expand(t, values) for t in shlex.split(stripped.lstrip("(").rstrip(")"))]
        except ValueError:
            tokens = []
        tokens, chdir = run_words(tokens)
        runs_in = here
        if chdir is not None and (here is not None or os.path.isabs(chdir)):  # relative to an unresolved cd: unresolved
            runs_in = cd_into(here or cwd, chdir)
        if tokens and tokens[0] in ("cd", "pushd"):
            args = [t for t in tokens[1:] if t == "-" or not t.startswith("-")]
            target = (args[0] if args else "~").replace("${HOME}", "~").replace("$HOME", "~")
            if target == "-" or "$" in target or "`" in target:
                here = None
            elif here is not None or os.path.isabs(os.path.expanduser(target)):
                here = cd_into(here or cwd, target)
        elif tokens and os.path.basename(tokens[0]) == "rm":
            flags = "".join(t.lstrip("-") for t in tokens[1:] if t.startswith("-") and not t.startswith("--"))
            if "r" in flags.lower() or "--recursive" in tokens:
                targets = [t for t in tokens[1:] if t and not t.startswith("-")]
                if run_by:
                    # Run as text, the caller's arguments are what it passes in (find -exec sh -c '… "$1"').
                    targets = [t for t in targets if not re.search(r"\$\{?[\d@*#]", t) and (runs_in is not None or os.path.isabs(t))]
                denied = dangerous_rm_targets(targets, runs_in, cwd)
                if denied:
                    return denied
        runs_strings = None
        while next_run < len(runs) and runs[next_run][0] < at:
            a, b = runs[next_run]
            next_run += 1
            if runs_strings is None:  # from the words before the segment's first string: a segment can hold thousands
                try:
                    words = run_words(shlex.split(text[start:a]))[0]
                except ValueError:
                    words = []
                runs_strings = code_spawns or line in spawning or (
                    line not in written_only and runs_its_strings(words, segment, text[at:], run_files))
            if runs_strings:
                script = PROCESS_QUOTE.sub(lambda m: "\n" + "".join(g or "" for g in m.group(1, 2, 3, 5, 6)) + "\n",
                                           text[a + 1:b - 1].replace("\\n", "\n"))
                denied = dangerous_rm(script, cwd, (runs_in, values))
                if denied:
                    return denied
        for _ in range(len(stripped) - len(stripped.rstrip(")"))):
            here = outer.pop() if outer else here
        line += segment.count("\n")
    return None


def runs_its_strings(words, segment, rest, run_files):
    """Whether a segment may run its quoted strings as commands, read from its words before them: a shell (bash -c,
    bash <<<, xargs sh -c), eval, trap or an interpreter given code, or an echo piped to one (rest is the text after
    the segment) or into a file the command runs. Never on another machine or in a container: ssh, docker exec."""
    names = [os.path.basename(w) for w in words]
    if any(name in REMOTE_RUNNERS for name in names):
        return False
    for i, name in enumerate(names):
        if re.fullmatch(r"(?:ba|z|da|k)?sh|eval|trap", name):
            return True
        interpreter = re.fullmatch(INTERPRETER, name)
        flags = interpreter and CODE_FLAGS[interpreter.group(1).rstrip("0123456789.")]
        if flags and any(re.fullmatch(rf"-(?:{flags})", w) for w in words[i + 1:]):
            return True
    return bool(PIPED_TO_RUNNER.match(rest) or written_files(segment) & run_files)


# A pipe into a shell or interpreter, which runs what it reads; after a 2>&1 the segment ends at the &.
PIPED_TO_RUNNER = re.compile(r"(?:&\d*\s*)?\|(?!\|)\s*" + PREFIXES + r"(?:sudo\s+(?:-\S+\s+)*)?(?:\S*/)?(?:(?:ba|z|da|k)?sh|xargs|python[\d.]*|node|ruby|perl|php)(?:\s|$)")
# Programs whose quoted commands run on another machine or in a container, where the local paths mean nothing.
REMOTE_RUNNERS = {"ssh", "docker", "podman", "kubectl", "vagrant", "ansible"}
# Ruby's %x(…) and Perl's qx{…} or backticks around a command inside interpreter code.
PROCESS_QUOTE = re.compile(r"(?:%x|\bqx\s*)(?:\(([^)]*)\)|\{([^}]*)\}|\[([^\]]*)\]|([/|])(.*?)\4)|`([^`]*)`")
# Wrappers that run the rest of their words, with their options that take a value (`env -u NAME`, `exec -a NAME`).
# PREFIXES reads the same wrappers for the regex rules: keep the two in step.
WRAPPER_VALUE_OPTIONS = {"env": "uCS", "time": "fo", "exec": "a", "command": "", "nohup": ""}
WRAPPER_LONG_VALUE_OPTIONS = ("--unset", "--chdir", "--split-string", "--output", "--format")


def run_words(tokens):
    """The words the shell runs once the keywords, VAR=value assignments and wrappers before them step aside, and the
    directory `env -C` runs them in (None when no wrapper moves)."""
    chdir, i = None, 0
    while i < len(tokens):
        word = tokens[i]
        i += 1
        if word in WRAPPER_VALUE_OPTIONS:
            while i < len(tokens) and tokens[i].startswith("-"):
                option = tokens[i]
                i += 1
                if option == "--":
                    break
                if word == "command" and option in ("-v", "-V"):
                    return [], chdir  # it only looks the names up
                long = option.startswith("--")
                if long:
                    name, value = option.partition("=")[::2]
                else:  # flags grouped up to the first that takes a value: -iu NAME
                    at = next((j for j, c in enumerate(option[1:], 1) if c in WRAPPER_VALUE_OPTIONS[word]), len(option))
                    name, value = "-" + option[at:at + 1], option[at + 1:]
                if name in WRAPPER_LONG_VALUE_OPTIONS if long else name != "-":
                    if not value and not (long and "=" in option):
                        value = tokens[i] if i < len(tokens) else ""
                        i += 1
                    if name in ("-C", "--chdir"):
                        chdir = value if chdir is None else os.path.join(chdir, value)  # env -C / env -C usr: /usr
        elif word not in ("builtin", "{", "then", "do", "else", "elif", "if", "while", "until", "!") \
                and not re.fullmatch(r"\w+=.*", word, re.S):
            return tokens[i - 1:], chdir
    return [], chdir


def dangerous_rm_targets(targets, here, cwd):
    """Each target judged from the tracked directory and from cwd as well: a cd the tracking gets wrong (in a
    pipeline, after `false &&`, undone by popd) can then only deny more than judging from cwd alone did."""
    home = os.path.realpath(os.path.expanduser("~"))
    for target in targets:
        target = os.path.expanduser(target)
        if "$" in target or "`" in target or "*" == target.strip("/") or (here is None and not os.path.isabs(target)):
            return f"Recursive delete of an unresolved or wildcard path: {target}"
        for place in {here or cwd, cwd}:
            path = os.path.realpath(os.path.join(place, target))
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


# Claude Code and Codex give the hook 10 s and let the command through when it runs out. All
# the git and stamp calls of one run share this budget, so the deny still arrives in time.
HOOK_BUDGET = 8
deadline = None  # set by main(); the tests that import this module have none


def time_left():
    return HOOK_BUDGET if deadline is None else max(deadline - time.monotonic(), 0.1)


def pr_checkout(command, cwd):
    """The checkout `gh pr create` acts on: a `cd <dir>` before it, else the worktree holding --head."""
    before = re.split(r"\b" + GH_PR + PR_CREATE + r"\b", command)[0]
    cds = re.findall(r"(?:^|[;&|]\s*)cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|]+)", before)
    runs_in = cwd
    for target in cds:
        runs_in = cd_into(runs_in, target)
    if cds:
        return os.path.realpath(runs_in)
    head = re.search(r"--head(?:=|\s+)(\S+)", command)
    if head:
        branch = head.group(1).split(":")[-1].strip("\"'")
        listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=cwd, timeout=time_left(),
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


# gh's --repo may come before or after `pr`, attached or not: `gh -R owner/repo pr ready 12`, `gh pr -Rowner/repo ready`.
REPO_FLAG = r"(?:(?:-R|--repo)(?:=|\s+)?\S+\s+)?"
GH_PR = r"gh\s+" + REPO_FLAG + r"pr\s+" + REPO_FLAG
PR_CREATE = r"(?:create|new)"  # `gh pr new` is gh's alias for create


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


GH_PR_CREATE_SWITCHES = {"--draft", "-d", "--dry-run", "--editor", "-e", "--fill", "-f", "--fill-first", "--fill-verbose",
                         "--no-maintainer-edit", "--web", "-w", "--help"}


def pr_target(words):
    """The branch `gh pr create` targets with --base/-B, the last one given as gh reads it; empty when it has
    none (review_stamp.py base resolves what gh then picks)."""
    target, value_of = "", None
    for word in words:
        if value_of:
            target = word if value_of in ("--base", "-B") else target
            value_of = None
        elif word in ("--base", "-B"):
            value_of = word
        elif word.startswith("--base="):
            target = word.split("=", 1)[1]
        elif word.startswith("-B") and len(word) > 2:
            target = word[2:].removeprefix("=")
        elif re.fullmatch(r"-[A-Za-z]|--[a-z-]+", word) and word not in GH_PR_CREATE_SWITCHES:
            value_of = word  # its value is the next word, even one that starts with -B (a title)
    return target


def unreviewed_pr(command, cwd):
    """Opening a PR requires a review stamp for the exact current change, measured against the branch the PR
    targets, and a validate stamp for a behavior change; each unless the user switched its phase off."""
    command = without_scripts(command)[0]
    creates = [m for m in re.finditer(COMMAND_START + PREFIXES + GH_PR + PR_CREATE + r"\b([^;&|\n]*)", shell_code(command))
               if not {"--help", "-h"} & set(pr_args(command, m, 1))]
    if not creates:
        return None
    cwd = pr_checkout(command, cwd)
    stamp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_stamp.py")

    def ok(*args):
        return subprocess.run([sys.executable, stamp, *args], cwd=cwd, timeout=time_left()).returncode == 0

    # The stamps are read before anything in the command runs, so one written by the same command can't count yet.
    order = (" This command writes a stamp itself, but the guard checks before anything in it runs: run "
             "review_stamp.py write (and --kind validate) as a command of its own, then this one."
             if re.search(r"review_stamp\.py\b[^;&|\n]*\bwrite\b", re.sub(r"\\\n", " ", shell_code(command))) else "")
    off, unreadable = phase_switches.phases_off_or_error(cwd, time_left())
    store = f" {unreadable}." if unreadable else ""
    if not {"review", "validate"} <= off.keys():
        for target in {pr_target(pr_args(command, match, 1)) for match in creates}:
            matched = subprocess.run([sys.executable, stamp, "base", "--matches", target], cwd=cwd,
                                     capture_output=True, text=True, timeout=time_left())
            if matched.returncode != 0:
                return (matched.stdout.strip() or matched.stderr.strip()
                        or f"review_stamp.py base failed (exit {matched.returncode}), so the PR's target can't be checked.")
    if "review" not in off and not ok("check", "--kind", "review"):
        return ("No review recorded for the current change. Run the kit-review skill first "
                "(it ends with review_stamp.py write); any edit after the review needs a new one." + order + store)
    if "validate" not in off and ok("needs-validate") and not ok("check", "--kind", "validate"):
        return ("The change touches behavior but has no validation recorded for it. Run the kit-validate skill "
                "(it ends with review_stamp.py write --kind validate); any edit after validating needs a new run."
                + order + store)
    return None


# The one form both this check and pr_checkout() read the same way: a literal absolute path (no expansion or glob),
# and && so gh only runs where the cd succeeded.
ABSOLUTE_CD_FIRST = re.compile(r"""cd[ \t]+((?:~[\w.-]*)?/[^\s;&|()$`'"\\*?\[]*|"/[^"$`\\]*"|'/[^']*')[ \t]*&&""")
# Anywhere, not only in command position: a wrapper, `then` or `{` in front must not hide it, and a false match only
# asks for the cd form.
GH_PR_WORDS = re.compile(r"\b" + GH_PR + r"(create|new|edit)\b([^;&|\n]*)")
CD_WORD = re.compile(r"(?<![\w/.-])(?:cd|pushd|popd)(?![\w/.-])")


def pr_checkout_unknown(command, payload):
    """Codex runs a command in its per-call workdir but sends the session's cwd (openai/codex#33986), so the PR
    checks would judge the wrong checkout unless the command starts by cd-ing to an existing absolute path, and
    neither cds again nor backgrounds the chain. Guards against a model's slip, not a hostile one."""
    code = shell_code(command)
    if harness(payload) != "codex" or not any(m.group(1) == "edit" or not {"--help", "-h"} & set(pr_args(command, m, 2))
                                              for m in GH_PR_WORDS.finditer(code)):
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
        origin = subprocess.run(["git", "-C", KIT, "remote", "get-url", "origin"], capture_output=True, text=True,
                                timeout=time_left()).stdout
        if not origin.strip():
            return False
        (kit_host, kit_slug), (host, slug) = repo_id(origin), repo_id(repo, host)
        return slug == kit_slug and host in (None, kit_host)
    def common_dir(path):
        return subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=path,
                              capture_output=True, text=True, timeout=time_left()).stdout.strip()
    # KIT/.git is a file when ~/.agents is itself a worktree (a verify testing a branch): compare common dirs.
    # Only KIT's own repository: a ~/.agents that isn't one could sit inside another (a dotfiles repo in HOME).
    common, kit = common_dir(cwd), common_dir(KIT) if os.path.exists(os.path.join(KIT, ".git")) else ""
    return bool(common and kit) and os.path.realpath(common) == os.path.realpath(kit)


COPY_COMMANDS = {"cp", "mv", "install", "ln"}
# Agents look for PRODUCT.md and DESIGN.md often; these only read what they name, unless the output is redirected.
READERS = {"cat", "head", "tail", "less", "grep", "rg", "ls", "stat", "test", "[", "wc", "file", "diff"}
READ_ONLY_IMPECCABLE = {"context", "detect", "doctor", "help", "version", "live-status"}
IMPECCABLE_FLAGS_ONLY = {"--help", "-h", "--schema", "--version"}
# What can stand before the program a segment runs: shell keywords, wrappers, runners, their flags, VAR=value.
LEADING = re.compile(r"-\S*|\w+=\S*|if|then|else|elif|do|while|until|\{|!|time|env|\S*/env|command|exec|nohup|"
                     r"npx|bunx|pnpm|dlx|yarn|npm|sudo")
# Quotes split words too: `sh -c 'touch PRODUCT.md'` names PRODUCT.md, and a path with spaces is rare here.
WORD = re.compile(r"[^\s\"'<>;&|()]+")


# Prose a command carries without running it, judged by the command that owns it: the message of git commit, tag,
# merge or notes and of gh pr or issue; what a lone, unpiped echo or printf prints; and a quoted heredoc fed to cat or
# tee. Everything else is read as before, however it might run.
MESSAGE_FLAG = re.compile(r"(?:^|\s)(?:-[a-zA-Z]*m|--(?:message|title|body|notes|subject))(?:=|\s+)$")
LEADING_ASSIGNMENTS = r"\s*(?:\w+=\S*\s+)*"
MESSAGE_COMMAND = re.compile(LEADING_ASSIGNMENTS + r"(?:git\s+(?:-[Cc]\s+\S+\s+)*(?:commit|tag|merge|notes)|gh\s+(?:pr|issue))\s")
PRINTING_COMMAND = re.compile(LEADING_ASSIGNMENTS + r"(?:echo|printf)(?:\s|$)")
PROSE_HEREDOC_RECEIVER = re.compile(LEADING_ASSIGNMENTS + r"(?:cat|tee)(?:\s|$)")
HEREDOC = re.compile(r"(?<!<)<<(?!<)-?\s*(?:(['\"])([^'\"\s]+)\1|\\([^\s;&|<>()]+)|([^\s;&|<>()'\"\\]+))")


@functools.lru_cache(maxsize=256)
def line_layout(line):
    """What heredoc_feeds() asks of a line, read once since a line can hold thousands of heredocs: the line with each
    closed string masked, the (position, closing position or None) of each $( on it, where each command it chains
    starts, where each one ends, and its pipes."""
    masked = list(line)
    for start, end in quoted_spans(line):
        if end - start > 1 and line[end - 1] == line[start]:  # a closed string, like "$(path.sh)", hides its ( and |
            masked[start:end] = "q" * (end - start)
    masked = "".join(masked)
    closes, opened = {}, []
    for i, c in enumerate(masked):
        if c == "(":
            opened.append(i)
        elif c == ")" and opened:
            closes[opened.pop()] = i
    substitutions = [(i - 1, closes.get(i)) for i in range(1, len(masked)) if masked[i - 1:i + 1] == "$("]
    masked = REDIRECT_AMPERSAND.sub(" ", masked)
    starts = [0] + [m.end() for m in re.finditer(r"&&|\|\||[;|(&]", masked)]
    ends = [m.start() for m in re.finditer(r"&&|\|\||[;&]", REDIRECT_AMPERSAND.sub(" ", line))] + [len(line)]
    return masked, substitutions, starts, ends, [i for i, c in enumerate(line) if c == "|"]


def heredoc_feeds(line, m, receiver):
    """The match of `receiver` when bash only feeds this heredoc's literal body to the program it matches: a quoted
    delimiter, that program owning the <<, and nothing piped onward."""
    masked, substitutions, starts, ends, pipes = line_layout(line)
    before = masked[:m.start()]
    owner = before[starts[bisect.bisect_right(starts, m.start()) - 1]:]
    onward_end = ends[bisect.bisect_left(ends, m.end())]
    piped_onward = bisect.bisect_left(pipes, m.end()) < bisect.bisect_left(pipes, onward_end)
    # Inside an open $(...) the text goes on to the enclosing command: only a message's, as in -m "$(cat <<'EOF', is text.
    unclosed = [i for i, close in substitutions if i < m.start() and (close is None or close > m.start())]
    enclosing = before[:unclosed[-1]] if unclosed else None
    if not m.group(4) and not piped_onward and (enclosing is None or MESSAGE_COMMAND.match(enclosing)):
        return receiver.match(owner)
    return None


def heredoc_body_is_prose(line, m):
    """Whether bash only feeds this heredoc's body to cat or tee as text."""
    return bool(heredoc_feeds(line, m, PROSE_HEREDOC_RECEIVER))


def without_prose(command):
    """The command with the prose it carries blanked, so a mention of PRODUCT.md, DESIGN.md or Impeccable in a commit
    message or an echoed sentence isn't read as a write: impeccable_files() takes its targets from the command's own
    text, quoted strings included, so a quoted path still counts. Whatever this can't read for sure, it leaves whole:
    missing a write costs more than asking about a mention."""
    # (delimiter, the files a prose body is written to, else None) of each heredoc still open, in order
    kept, heredocs, had_prose = [], [], False
    shell, rest = [], []  # the lines outside heredoc bodies; the same without the commands writing a prose body
    prose_lines = []  # (index in kept, the files its body is written to) of each prose body line
    for line in command.split("\n"):
        if heredocs:
            delimiter, writes = heredocs[0]
            if line.lstrip("\t") == delimiter:
                heredocs.pop(0)
                kept.append(line)
                shell.append(line)
                rest.append(line)
            elif writes is not None:
                prose_lines.append((len(kept), writes))
                kept.append(line)
            elif "$(" in line or "`" in line:
                return command  # an unquoted body expands, quotes and all
            else:
                kept.append(line)
            continue
        kept.append(line)
        shell.append(line)
        comment = re.search(r"(?:^|\s)#", line)  # a << after it is text
        heredocs, header = [], line
        for m in HEREDOC.finditer(line):
            if comment and m.start() >= comment.start():
                continue
            writes = None
            if heredoc_body_is_prose(line, m):  # its command: from the separator before it to the one after
                a = max(line.rfind(c, 0, m.start()) for c in ";&|") + 1
                b = min([i for i in (line.find(c, m.end()) for c in ";&|") if i >= 0] or [len(line)])
                writes = written_files(line[a:b])
                header = header[:a] + " " * (b - a) + header[b:]
            heredocs.append((m.group(2) or m.group(3) or m.group(4), writes))
        rest.append(header)
        had_prose |= any(writes is not None for _, writes in heredocs)
        if heredocs and line.endswith("\\"):
            return command  # the header goes on, redirects and all, on the next line
    run = files_run("\n".join(shell))
    for i, writes in prose_lines:
        if not writes & run:  # a body written to a script the command runs is code, not prose
            kept[i] = ""
    text = "\n".join(kept)
    if heredocs or RUNS_QUOTED_TEXT.search(text):
        return command  # a heredoc that never ends was only a mention of one
    if had_prose and RUNS_SCRIPT_FILE.search(text) or written_files("\n".join(rest)) & run:
        return command  # the heredoc or echo may have written the script it runs

    spans = quoted_spans(text)
    masked = list(text)
    for start, end in spans:
        masked[start:end] = "q" * (end - start)
    masked = REDIRECT_AMPERSAND.sub(" ", "".join(masked).replace(CONTINUATION, "  "))  # continued lines are one command
    # Each command judged once: per string, its slice of a long command would make this quadratic. No cut at a
    # parenthesis: what an echo prints inside $(...) or <(...) is fed to another command.
    cuts = [0] + [i for m in re.finditer(r"&&|\|\||[;&\n]", masked) for i in m.span()] + [len(masked)]
    starts = cuts[::2]
    kinds = []
    for a, b in zip(starts, cuts[1::2]):
        segment = masked[a:b]
        runs_inside = any(token in segment for token in ("$(", "`", "|", "<(", ">("))
        kinds.append(None if runs_inside else "message" if MESSAGE_COMMAND.match(segment)
                     else "printing" if PRINTING_COMMAND.match(segment) else None)
    out, last = [], 0
    for start, end in spans:
        quoted, before = text[start:end], masked[max(0, start - 40):start]
        kind = kinds[bisect.bisect_right(starts, start) - 1]
        whole_word = (start == 0 or masked[start - 1] in " \t=") and (end == len(masked) or masked[end] in " \t\n;&|)")
        if not kind or not whole_word or (quoted[0] == '"' and ("$" in quoted or "`" in quoted)):
            continue
        if MESSAGE_FLAG.search(before) if kind == "message" else not re.search(r"[<>]\s*$", before):
            out += [text[last:start], "''"]
            last = end
    return "".join(out) + text[last:]


INTERPRETER = r"(?:\S*/)?(python[\d.]*|node|ruby|perl|php)"
INTERPRETER_RECEIVER = re.compile(LEADING_ASSIGNMENTS + INTERPRETER + r"(?:\s|$)")
# Interpreter code that starts a process: its strings may run as a command. Named only inside a string, it still
# counts, which only denies more.
PROCESS_CALLS = (r"\bos\.(?:system|exec\w*|spawn\w*)\b|\bsubprocess\b|\bpopen\b|\bexec(?:[lv]p?e?)?(?:\s*[(\[]|\s+[\"'])|"
                 r"(?<![\w-])system\s*[(\"'\[]|pty\.spawn|create_subprocess_\w+|\bspawn\s*\(")
# Calls only one language makes: in a Python string, Open3 or grep -qx "…" is only a word.
LANGUAGE_PROCESS_CALLS = {"node": r"child_process", "ruby": r"\bOpen3\b|%x[{(\[|]", "perl": r"(?<![\w-])qx\s*[{(\[/|'\"]",
                          "php": r"shell_exec|passthru|proc_open"}
SPAWNS = re.compile("|".join([PROCESS_CALLS, *LANGUAGE_PROCESS_CALLS.values()]), re.I)
SPAWNS_IN = {language: re.compile(PROCESS_CALLS + "|" + calls, re.I) for language, calls in LANGUAGE_PROCESS_CALLS.items()}
PYTHON_SPAWNS = re.compile(PROCESS_CALLS, re.I)
SHELL_BACKTICKS = re.compile(r"ruby|perl|php")
READS_FILE = re.compile(r"\.read(?:_text|_bytes)?\s*\(|readFileSync|File\.read|file_get_contents")
# Python's exec() runs Python code, not a process: exec("x = 1") in a test helper starts nothing.
PYTHON_EXEC = re.compile(r"(?<![\w.])exec\s*\(")
# The flag that makes an interpreter's next argument its code: its other arguments are data a script may run.
CODE_FLAGS = {"python": "c", "node": "e|p|-eval|-print", "ruby": "e", "perl": "e|E", "php": "r"}
SQL_USED = re.compile(r"\b(?:psql|mysqli?|mariadb|sqlite3|psycopg2?|pymysql)|\bwp\s+db\s+(?:query|cli)\b|\.(?:execute|query)\s*\(|"
                      r"exec_driver_sql|\bduckdb\b|\.raw\s*\(|\$(?:execute|query)Raw(?:Unsafe)?\b")
# A command that runs a script file: a heredoc it writes to a file may be that script.
RUNS_SCRIPT_FILE = re.compile(r"(?:^|[;&|(\n]|\b(?:then|do))[^\S\n]*(?:(?:command|exec)\s+)?(?:(?:(?:/[\w.-]+)*/)?(?:ba|z|da|k)?sh\s+(?:-\w+\s+)*[^\s<-]|source\s|\.\s+\S|"
                              r"\.{0,2}/\S+\.sh\b)")
# A redirection's & (2>&1, >&2, &>) joins no commands.
REDIRECT_AMPERSAND = re.compile(r"(?<=[<>])&|&(?=>)")
# A file a command writes: the target of > or >> (not >&2 nor a process substitution), or of tee.
WRITES_FILE = re.compile(r"(?:(?<![<>&\d])\d*>>?|&>>?|\btee\s+(?:-\w+\s+)*)(?![&>(|])\s*(['\"]?)(?P<file>[^\s;&|<>()'\"]+)\1")
# The file a command runs: given to a shell, source or an interpreter, or run by its own path.
RUNS_FILE = re.compile(COMMAND_START + PREFIXES + r"(?![!{]|(?:if|then|do|else|elif|while|until)(?:\s|$))(?P<runner>(?:(?:/[\w.-]+)*/)?(?:ba|z|da|k)?sh\s+"
                       r"(?:-(?!\w*n)\w+\s+|--[-\w]+\s+)*|source\s+|\.\s+|(?:[\w.~-]*/)*(?:python[\d.]*|node|ruby|perl|php)\s+(?:-(?![mce]\s)[-\w]+\s+)*)?"
                       r"(['\"]?)(?!\w+=)(?P<file>[^\s;&|<>()'\"-][^\s;&|<>()'\"]*)\2")


def written_files(text):
    """The files the text's commands write, as given (./ dropped); /dev/null and the like are not files."""
    return {m.group("file").removeprefix("./") for m in WRITES_FILE.finditer(text) if not m.group("file").startswith("/dev/")}


@functools.lru_cache(maxsize=8)
def files_run(text):
    """The script files the text's commands run, as given (./ dropped): a bare name only when a runner is given it,
    since a shell looks the name up in PATH. `bash -n` only reads the file for its syntax."""
    return {m.group("file").removeprefix("./") for m in RUNS_FILE.finditer(text) if m.group("runner") or "/" in m.group("file")}


@functools.lru_cache(maxsize=8)
def files_interpreted(text):
    """{file: interpreter} for the script files the text's commands give an interpreter (python3 x.py)."""
    return {m.group("file").removeprefix("./"): interpreter.group(1) for m in RUNS_FILE.finditer(text)
            if (interpreter := re.match(INTERPRETER + r"\s", m.group("runner") or ""))}


# Programs whose quoted arguments are only text: what they read, match or print.
TEXT_PROGRAMS = READERS | {"sed", "git", "gh", "echo", "printf"}
TEXT_HINT = ("The match is in quoted text or a heredoc body: the guard reads that text as a command because something "
             "here may run it (a shell, eval, a SQL client, a process call); if it is only text, write it with a file-writing "
             "tool instead.")


def starts_process(code, interpreter):
    """Whether interpreter code may start a process, so its strings may run as a command."""
    if interpreter.startswith("python"):
        code = PYTHON_EXEC.sub("", code)
    spawns = SPAWNS_IN.get(interpreter, PYTHON_SPAWNS)
    return bool(spawns.search(code) or (SHELL_BACKTICKS.match(interpreter) and "`" in code))


def read_heredocs(lines):
    """(first body line, end, kind, interpreter) of each heredoc in the lines; kind is "script" for a quoted body only
    an interpreter reads, fed to it or written to a file the lines give it, that starts no process, "spawns" when it
    starts one, else "other". A heredoc that never
    ends runs to the last line. None when a header goes on past its line, which this doesn't read."""
    found, queue = [], []  # queue: [delimiter, header line, match, first body line] of each heredoc still open
    for i, line in enumerate(lines):
        if queue:
            if line.lstrip("\t") == queue[0][0]:
                found.append(queue.pop(0)[1:] + [i])
                if queue:
                    queue[0][3] = i + 1
            continue
        comment = re.search(r"(?:^|\s)#", line)
        quoted = quoted_spans(line)
        queue = [[m.group(2) or m.group(3) or m.group(4), line, m, i + 1] for m in HEREDOC.finditer(line)
                 if (not comment or m.start() < comment.start()) and not any(a < m.start() < b for a, b in quoted)]
        if queue and line.endswith("\\"):
            return None
    found += [q[1:] + [len(lines)] for q in queue]
    in_body = {i for _, _, start, end in found for i in range(start, end)}
    interpreted = files_interpreted("\n".join("" if i in in_body else line for i, line in enumerate(lines)))
    bodies = []
    for header, m, start, end in found:
        program = heredoc_feeds(header, m, INTERPRETER_RECEIVER)
        program = program.group(1) if program else next(
            (interpreted[f] for f in written_files(header) if f in interpreted and not m.group(4)), "")
        body = "\n".join(lines[start:end])
        kind = "other" if not program else "spawns" if starts_process(body, program) else "script"
        bodies.append((start, end, kind, program))
    return bodies


@functools.lru_cache(maxsize=8)
def without_scripts(command):
    """without_prose(command) with the body of each heredoc read_heredocs() calls a script blanked too, and the
    numbers of the lines every heredoc body takes; None for those when eval or sh -c may run quoted text, or a
    heredoc can't be read: then nothing more is blanked."""
    text = without_prose(command)
    lines = text.split("\n")
    bodies = read_heredocs(lines)
    if bodies is None:
        return text, None
    in_body = {i for start, end, _, _ in bodies for i in range(start, end)}
    shell = "\n".join("" if i in in_body else line for i, line in enumerate(lines))
    if RUNS_QUOTED_TEXT.search(shell):  # in a heredoc body, eval or sh -c is that body's own code or text
        return text, None
    # Only a file a shell runs reads the script's strings as commands: one edited, then run by python3, is Python.
    run = files_run(shell) - files_interpreted(shell).keys()
    # An absolute path by its whole spelling: a script that imports git writes no /usr/bin/git.
    names = re.compile("|".join(r"(?<![\w.-])" + re.escape(f if f.startswith("/") else os.path.basename(f)) + r"(?![\w.-])"
                                for f in run) or r"(?!)")
    for start, end, kind, _ in bodies:
        body = "\n".join(lines[start:end])
        # Naming a file it runs, it may write it; reading a file too, it edits one, and adds that file's own code
        # as an editor would, which the guard never reads either.
        if kind == "script" and (not names.search(body) or READS_FILE.search(body)):
            lines[start:end] = [""] * (end - start)
    return "\n".join(lines), frozenset(in_body)


def blank_strings(text, in_body, text_in):
    """The text with the inside of each quoted string on its shell lines blanked (same length, new lines kept) when
    it is only text: text_in(segment, program) says which strings are, for the segment holding them and the program
    it runs, given also with its strings as written."""
    lines = text.split("\n")
    shell = "\n".join("" if i in in_body else line for i, line in enumerate(lines))
    spans = quoted_spans(shell)
    masked = list(shell)
    for start, end in spans:
        masked[start:end] = "q" * (end - start)
    masked = REDIRECT_AMPERSAND.sub(" ", "".join(masked).replace(CONTINUATION, "  "))
    cuts = [0] + [i for m in re.finditer(r"&&|\|\||[;&|\n]", masked) for i in m.span()] + [len(masked)]
    starts, out, judged = cuts[::2], list(shell), {}
    for start, end in spans:
        segment = bisect.bisect_right(starts, start) - 1
        if segment not in judged:  # once per segment: a segment can hold thousands of strings
            a, b = starts[segment], cuts[2 * segment + 1]
            named = re.match(r"[\s({!]*(?:(?:\w+=\S*|then|do|else|elif|if|while|until|time|command|builtin|nohup)\s+)*"
                             r"(\S+)", masked[a:b])
            separator_end = cuts[2 * segment + 2] if 2 * segment + 2 < len(cuts) else b
            judged[segment] = text_in(masked[a:separator_end], os.path.basename(named.group(1)) if named else "",
                                      shell[a:separator_end])
        if judged[segment](shell[start:end]):
            out[start + 1:end - 1] = ["\n" if c == "\n" else "x" for c in shell[start + 1:end - 1]]
    out = "".join(out).split("\n")
    return "\n".join(line if i in in_body else out[i] for i, line in enumerate(lines))


def text_strings(segment, program, source):
    """Which quoted strings of a segment, separator included, are only text to the program it runs: those of
    TEXT_PROGRAMS, and the strings of an interpreter given code with CODE_FLAGS when nothing in their segment starts a
    process. None are when the segment runs a $(…) or backticks, nor a double-quoted string holding one, nor those of a
    git alias, -x, submodule foreach, filter-branch or bisect run, gh api, or an echo, printf or interpreter a pipe
    reads."""
    def runs(string):
        return string[0] == '"' and ("$(" in string or "`" in string)
    if "$(" in segment or "`" in segment:
        return lambda string: False
    piped = re.search(r"(?<!\|)\|(?!\|)$", segment)
    interpreter = re.fullmatch(INTERPRETER, program)
    if interpreter:
        code = re.search(rf"\s-(?:{CODE_FLAGS[interpreter.group(1).rstrip('0123456789.')]})(?:\s|$)", segment)
        text = code and not piped and not starts_process(source, interpreter.group(1))
        return lambda string: text and not runs(string)
    if program in ("echo", "printf"):
        text = not piped
    elif program == "git":
        text = not re.search(r"\s(?:-x|--exec)(?:[\s=]|$)|\bsubmodule\s+(?:-\S+\s+)*foreach\b|\bfilter-branch\b|\bbisect\s+run\b",
                             segment) and not re.search(r"\balias\b", source)
    elif program == "gh":
        text = not re.search(r"\s(?:api|alias)\s", segment)
    else:
        text = program in TEXT_PROGRAMS
    return lambda string: text and not runs(string)


def code_text(command):
    """The command as the RULES read it: without its prose, its interpreter scripts, and the strings of programs that
    only read or print them (text_strings())."""
    text, in_body = without_scripts(command)
    return text if in_body is None else blank_text_strings(text, in_body)


def blank_text_strings(text, in_body):
    """blank_strings() of the strings text_strings() calls only text, keeping those an echo or printf writes to a
    script the text runs."""
    run = files_run("\n".join("" if i in in_body else line for i, line in enumerate(text.split("\n"))))

    def text_in(segment, program, source):
        if program in ("echo", "printf") and written_files(source) & run:
            return lambda string: False  # it writes a script the command runs
        return text_strings(segment, program, source)
    return blank_strings(text, in_body, text_in)


def strings_running_rm(command):
    """The quoted strings holding an rm that a shell, eval or a spawning script may run as a command: those
    blank_text_strings() keeps, even where code_text() keeps every string (a `git commit -m` beside an eval runs none)."""
    text, in_body = without_scripts(command)
    code = blank_text_strings(text, in_body or frozenset())
    return [text[a + 1:b - 1] for a, b in quoted_spans(text)
            if code[a + 1:b - 1] == text[a + 1:b - 1] and re.search(r"\brm\s", text[a + 1:b - 1])]


def bare_code(command):
    """The command with every quoted string and heredoc body blanked: a rule that matches code_text() and not this
    matched only text that might run."""
    text, in_body = without_scripts(command)
    lines = text.split("\n")
    in_body = in_body if in_body is not None else set()
    shell = "\n".join("" if i in in_body else line for i, line in enumerate(lines))
    return blank_strings(shell, set(), lambda segment, program, source: lambda string: True)


def string_literals(code, interpreter):
    """The string literals of Python code, as its tokenizer reads them; None for other languages, whose comments,
    template literals and q() strings a shell's quote reader misreads, or when it can't."""
    if not interpreter.startswith("python"):
        return None
    kinds = {tokenize.STRING, getattr(tokenize, "FSTRING_MIDDLE", tokenize.STRING)}
    try:
        return [t.string for t in tokenize.generate_tokens(io.StringIO(code).readline) if t.type in kinds]
    except (tokenize.TokenError, SyntaxError):
        return None


# A variable assignment's value, each piece matched one way only: plain characters, $ not opening $(, a $(…) without
# nested parentheses, a double- or single-quoted string. Assignments end at whitespace, or a=b=c would split many ways.
ASSIGNED_VALUE = r"""(?:[^\s"'$`()]|\$(?!\()|\$\([^()]*\)|"[^"]*"|'[^']*')*"""
ASSIGNMENTS_ONLY = re.compile(r"(?:(?:export|local|readonly|declare)\s+)?((?:\w+=" + ASSIGNED_VALUE + r"(?:\s+|$))+)")
ONE_ASSIGNMENT = re.compile(r"(\w+)=(" + ASSIGNED_VALUE + r")")
ASSIGNED_NAME = re.compile(r"(?<![\w$.:/-])(\w+)\+?=")
VARIABLE = re.compile(r"\$\{(\w+)(:?-[^}]*)?\}|\$(\w+)")
PATH_MAX = 4096
SEGMENT_BREAK = re.compile(r"[;&|\n][;&|\n \t]*")
COMPOUND_OPENS = {"if", "while", "until", "for", "select", "case", "{"}
COMPOUND_CLOSES = {"fi", "done", "esac"}
MKTEMP = re.compile(r'"?\$\(\s*mktemp\b([^()]*)\)"?')


def expand(word, values):
    """The word with each variable values holds substituted; a name values holds as None is known to be unset, so
    its ${NAME:-default} is the default. Anything else stays as written."""
    def one(m):
        name, default = m.group(1) or m.group(3), m.group(2)
        if name not in values:
            return m.group(0)
        value = values[name]
        if default and (value is None or (value == "" and default.startswith(":"))):
            return default.split("-", 1)[1]
        return m.group(0) if value is None else value
    return VARIABLE.sub(one, word)


def mktemp_path(args, values):
    """Where `mktemp <args>` makes its file or directory: its -p directory, else the temp directory with -t or no
    template, else the template itself (relative to where it runs). None when it can't be told."""
    try:
        words = shlex.split(expand(args, values))
    except ValueError:
        return None
    if any("$" in w or "`" in w for w in words):
        return None
    if "TMPDIR" not in values:
        return None  # set some way it can't follow
    temp = values["TMPDIR"] or "/tmp"
    directory, temp_flag, templates, value_of = None, False, [], None
    for word in words:
        if value_of:
            directory, value_of = word, None
        elif word.startswith("--tmpdir"):
            directory = word.partition("=")[2] or temp
        elif word.startswith("--"):
            continue
        elif re.match(r"-[a-oq-z]*p", word):
            temp_flag |= "t" in word[:word.index("p")]
            directory = word[word.index("p") + 1:] or None
            value_of = not directory
        elif word.startswith("-"):
            temp_flag |= "t" in word
        else:
            templates.append(word)
    if temp_flag or not templates:
        return os.path.join(directory or temp, "tmp.XXXXXX")
    return os.path.join(directory, templates[-1]) if directory else templates[-1]


def assigned_value(raw, values):
    """The value an assignment's raw text gives, or None when it isn't a literal path or a mktemp: one with spaces or
    glob characters is several paths, or a pattern, where it isn't quoted."""
    made = MKTEMP.fullmatch(raw)
    if made:
        value = mktemp_path(made.group(1), values)
    elif raw.startswith("'"):
        value = raw[1:-1] if len(raw) > 1 and raw.endswith("'") and "'" not in raw[1:-1] else None
    else:
        value = expand(raw.replace('"', ""), values)
        if re.search(r"[$`'()]", value):
            return None
    return None if value is None or re.search(r"[\s*?\[]", value) or len(value) > PATH_MAX else value


def chained_by(separator):
    """The chain a segment separator joins into: "&&", "||", "|", "&" for a background job, or ";" for ; and new lines."""
    for kind in ("&&", "||", "|"):
        if kind in separator:
            return kind
    return "&" if separator.strip() == "&" else ";"


def segments_with_values(text, inherited=None):
    """Each segment the text splits into at ; & | and new lines, with the variables known to hold a value when it
    runs, then ("", the values known at the end). A variable is known after an assignment that surely ran: a
    segment of only assignments, outside a subshell, $(…), compound command, pipeline or background job, at the start,
    after ; or a new line, or earlier in the same && chain. One set some other way too (twice, by read or a loop, ${X:=…}) stays unknown. $HOME and
    $TMPDIR come from the hook's environment unless the command sets them, or all from inherited: the values known
    where the text runs as a command (eval)."""
    names = ASSIGNED_NAME.findall(text)
    unsure = {name for name in names if names.count(name) > 1} if len(names) < 200 else set(names)
    unsure |= {n for pair in re.findall(r"\b(?:for|select)\s+(\w+)|\$\{(\w+):?=", text) for n in pair if n}
    for words in re.findall(r"\b(?:read|mapfile|readarray|getopts|printf\s+-v|unset)\b([^;&|\n]*)", text):
        unsure |= set(re.findall(r"\w+", words))
    base = dict(inherited) if inherited is not None else {"HOME": os.path.expanduser("~"), "TMPDIR": os.environ.get("TMPDIR")}
    for name in set(names) | unsure:
        base.pop(name, None)
    known, chain, parens, compound = {}, {}, 0, 0
    by_line = "\n".join(line_layout(line)[0] for line in text.split("\n")).replace(CONTINUATION, "  ")
    whole = list(text.replace(CONTINUATION, "  "))
    for start, end in quoted_spans(text):
        whole[start:end] = "q" * (end - start)
    whole = "".join(whole)
    # A string spanning lines fools the line reading, an apostrophe in a heredoc body the whole-text one: a separator
    # either leaves outside a string splits, and only separators both see frame a trusted assignment.
    masked = "".join(a if a == b else text[i] for i, (a, b) in enumerate(zip(by_line, whole)))
    breaks = [0] + [i for m in SEGMENT_BREAK.finditer(masked) for i in m.span()] + [len(text)]
    parts = [text[a:b] for a, b in zip(breaks, breaks[1:])]
    sure = [by_line[a:b] == masked[a:b] == whole[a:b] for a, b in zip(breaks, breaks[1:])]
    for k in range(0, len(parts), 2):
        before = chained_by(parts[k - 1]) if k else ";"
        after = chained_by(parts[k + 1]) if k + 1 < len(parts) else ";"
        if before != "&&":
            chain = {}
        values = {**base, **known, **chain}
        yield parts[k], values
        stripped, code = parts[k].strip(), masked[breaks[k]:breaks[k + 1]].strip()
        first = (code.split() or [""])[0]
        leading = len(code) - len(code.lstrip("("))
        parens += leading
        compound += first in COMPOUND_OPENS or code.endswith("{")
        assignments = ASSIGNMENTS_ONLY.fullmatch(stripped)
        framed = (k == 0 or sure[k - 1]) and (k + 1 >= len(parts) or sure[k + 1])
        if assignments and framed and parens == compound == 0 and before in (";", "&&") and after not in ("|", "&"):
            for m in ONE_ASSIGNMENT.finditer(assignments.group(1)):
                value = None if m.group(1) in unsure else assigned_value(m.group(2), {**base, **known, **chain})
                if value is not None:
                    (known if before == ";" else chain)[m.group(1)] = value
        parens = max(0, parens + code.count("(") - code.count(")") - leading)  # a case pattern's ) opened nothing
        compound = max(0, compound - (first in COMPOUND_CLOSES or first.startswith("}")))
    yield "", {**base, **known}


def database_grant(statement):
    """The approval a DROP or TRUNCATE statement needs, the same for any case or spacing: `sql.drop-database`."""
    return "sql." + "-".join(statement.lower().split())


def database_approval_names(prompt):
    """The approvals a user's message grants: the lines it opens with that read `allow DROP DATABASE` (or another
    statement), or a message that only asks in plain words (plain_request_grants). Only there: questions, refusals,
    quotes, pasted dumps and code examples name them anywhere else."""
    grants = plain_request_grants(prompt)
    for line in opening_allow_lines(prompt):
        allow = DATABASE_ALLOW_LINE.fullmatch(line)
        if allow:
            grants.add(database_grant(allow.group(1)))
    return sorted(grants)


def opening_allow_lines(prompt):
    """The `allow …` lines a user's message opens with, of either kind, in any order."""
    lines = []
    for line in prompt.splitlines():  # not lstrip(): an indented first line is a code example
        if not (DATABASE_ALLOW_LINE.fullmatch(line.rstrip()) or ACTION_ALLOW_LINE.fullmatch(line.rstrip())):
            break
        lines.append(line.rstrip())
    return lines


PLAIN_VERBS = {**dict.fromkeys(["borra", "borrad", "elimina", "eliminad", "suprime", "suprimid", "drop", "delete",
                                "remove", "destroy", "wipe"], "DROP"),
               **dict.fromkeys(["vacía", "vacia", "vaciad", "trunca", "truncad", "truncate", "empty"], "TRUNCATE")}
PLAIN_OBJECTS = {**dict.fromkeys(["database", "databases", "db", "dbs", "bd", "bds", "bbdd", "base", "bases"],
                                 "DATABASE"),
                 **dict.fromkeys(["table", "tables", "tabla", "tablas"], "TABLE")}  # a schema is mostly Zod's or JSON's
PLAIN_DETERMINERS = {"the", "a", "an", "all", "every", "each", "both", "this", "that", "these", "those", "my", "our",
                     "your", "el", "la", "los", "las", "un", "una", "unos", "unas", "todo", "toda", "todos", "todas",
                     "ese", "esa", "esos", "esas", "este", "esta", "estos", "estas", "mi", "mis", "tu", "tus", "su",
                     "sus", "nuestro", "nuestra", "nuestros", "nuestras", "ambos", "ambas"}
PLAIN_THANKS = re.compile(r"((please|por favor|gracias|muchas gracias|thanks) )*")
PLAIN_YES = re.compile(r"((sí|si|yes|ok|okay|vale|claro|perfecto|perfect|genial|great|sure|adelante|go ahead|dale|"
                       r"venga|de acuerdo|bien|good|please|por favor|gracias|muchas gracias|thanks) )*")
PLAIN_LEADS = {"please", "por", "favor", "now", "ahora", "also", "too", "también", "tambien", "then", "just"}
PLAIN_MODIFIERS = {"test", "tests", "testing", "old", "unused", "temporary", "temp", "local", "stale", "scratch", "dev",
                   "legacy", "leftover", "orphaned", "duplicate", "empty", "fixture", "viejo", "vieja", "viejos",
                   "viejas", "antiguo", "antigua", "antiguos", "antiguas", "temporal", "temporales"}
PLAIN_TAIL = re.compile(r"((de|of) (prueba|pruebas|test|tests|testing) )?"
                        r"((please|por favor|now|ahora|too|también|tambien|thanks|gracias) )*")


def plain_request_grants(prompt):
    """The approvals a message grants when, after any opening allow lines, all it says is yes and asks in plain
    words: "Sí, borra las bases de prueba", "Perfect. Drop the test databases and empty the runs table." One clause
    that isn't a yes or a request (another instruction, a question, a condition, a retraction), or a character
    beyond words and `,.!¡-*` (a label, a quote, code, markup), and the message grants nothing."""
    lines = prompt.splitlines()[len(opening_allow_lines(prompt)):]
    grants = set()
    for line in filter(str.strip, lines):
        if not re.fullmatch(r"[\w ,.!¡*-]+", line) or line[0] == " " or ".." in line:  # an example; "pero..."
            return set()
        for sentence in re.split(r"(?<=[.!])\s+", line.rstrip()):
            sentence = sentence.replace("¡", "").rstrip(".! ")
            unfinished = re.search(r"(,|\b(?:and|y)\b)\s*(,|$)", sentence, re.I)  # "Drop the tables and"
            if unfinished or not re.search(r"\w", sentence):
                return set()
            for clause in re.split(r",|\b(?:and|y)\b", sentence, flags=re.I):
                words = re.findall(r"[\w*.-]+", clause)
                said = "".join(w.lower() + " " for w in words)
                if (PLAIN_THANKS if grants else PLAIN_YES).fullmatch(said):  # "…, si" is an if; "por" alone isn't
                    continue
                grants.add(plain_request(words))
    return set() if None in grants else grants


def plain_request(words):
    """The approval an imperative clause asks for, `[please] VERB [too] DET [DET] [old, test, arm_test_*] OBJECT
    [de prueba] [please]` ("borra también las tablas de prueba", "drop the old test databases"), else None."""
    lower = [w.lower() for w in words] + [""]
    i = 0
    while lower[i] in PLAIN_LEADS:
        i += 1
    if lower[i] not in PLAIN_VERBS or words[i].isupper():
        return None
    verb = PLAIN_VERBS[lower[i]]
    i += 1
    while lower[i] in PLAIN_LEADS:
        i += 1
    if lower[i] not in PLAIN_DETERMINERS:
        return None
    i += 1 + (lower[i + 1] in PLAIN_DETERMINERS)
    while lower[i] in PLAIN_MODIFIERS or re.fullmatch(r"(?=.*[\d_*])[^\W_][\w*]*", lower[i]):  # arm_test_*
        i += 1
    if lower[i] not in PLAIN_OBJECTS:
        return None
    tail = " ".join(lower[i + 1:-1])
    if lower[i] in ("base", "bases"):  # alone it's a base class or image: "base de datos", "bases de prueba"
        if not re.match(r"de (datos|prueba|pruebas|test|tests)\b", tail):
            return None
        tail = re.sub(r"^de datos ?", "", tail)
    statement = f"{verb} {PLAIN_OBJECTS[lower[i]]}"
    if not PLAIN_TAIL.fullmatch(tail + " " if tail else "") or not re.fullmatch(DATABASE_DESTROY, statement):
        return None  # something after the object ("the table headers"), or emptying a database
    return database_grant(statement)


def approved_database_grants(command, session):
    """The approvals of the DROP or TRUNCATE statements in the command, when every one is approved by an `allow`
    line in the user's message this turn or by the user's own approval file (guard_mcp.approved); else []."""
    grants = sorted({database_grant(m.group()) for m in re.finditer(DATABASE_DESTROY, command)})
    return grants if all(design_files.guard_mcp().approved(g, session) for g in grants) else []


def push_grant(branch):
    return "git.push-" + branch.lower().replace("/", "-")


MERGE_GRANT = "git.merge"
# What may follow a request: courtesy, timing, the base branch, or a condition on CI ("merge it once CI passes").
REQUEST_TAIL = (rf"(?: (?:now|please|por favor|ya|ahora|ahora mismo|too|also|tambien|thanks|gracias|right away|asap|"
                rf"(?:into|to|en|a) (?:la rama )?{PROTECTED_BRANCHES}))*"
                r"(?: (?:when|once|if|after|si|cuando|en cuanto|una vez|una vez que) "
                r"(?=[\w ]*\b(?:ci|checks?|tests?|build|green|verde)\b)[\w ]*)?")
# The whole clause, accents dropped: "Merged.", "after the merge" and "Merge the PR failed" ask for nothing.
MERGE_REQUEST = re.compile(r"(?:merge(?:a|alo|ala|alos|adlo|ad)|mergea(?:d)? (?:el|este|ese) (?:pr|pull request)|(?:haz|"
                           r"haced) (?:el )?merge|merge(?: (?:it|this|that|them|the pr|the pull request|pr|pr #?\d+|"
                           r"#\d+))?)" + REQUEST_TAIL)
PUSH_REQUEST = re.compile(rf"(?:pushea(?:lo)?|haz push|haced push|sube(?:lo)?|push(?: it| this| the branch| the changes)?) "
                          rf"(?:a|to) (?:la rama )?(?P<branch>{PROTECTED_BRANCHES})" + REQUEST_TAIL)
REQUEST_LEADS = re.compile(r"((si|yes|ok|okay|vale|claro|perfecto|perfect|genial|great|sure|dale|venga|bien|good|"
                           r"please|por favor|gracias|thanks|thank you|then|luego|ahora|now|also|tambien|ya|so|pues|"
                           r"just|go ahead( and)?|adelante|listo|de acuerdo) )*")
# A condition only the user can meet holds the sentence's request back: the grant would end before they meet it.
REQUEST_HELD = re.compile(r"\b(after|once|when|until|before|cuando|hasta|en cuanto|una vez que|despues de que|antes de "
                          r"que) (i|yo|te lo|lo)\b|\b(i say|say so|tell you|te diga|myself|yo mismo|let me|dejame)\b|"
                          r"\b(i|yo) (approve|review|check|confirm)\b")
# Later holds back the clause it's in ("Mergéalo y después instálalo" still merges), or the sentence when it's alone.
REQUEST_LATER = re.compile(r"\b(later|despues|mas tarde|tomorrow|manana)\b")
LATER_CLAUSE = re.compile(r"(?:(?:but|pero|only|solo) )*(?:later|despues|mas tarde|tomorrow|manana)")
ACTION_WORD = re.compile(r"\bmerg|\bpush|\bsube")
RETRACTION = re.compile(r"\b(wait|espera|actually|nevermind|never mind|olvidalo|scratch that|mejor no|cancel|"
                        r"cancela|stop|abort|hold on|hold off|no lo hagas|not yet|todavia no|aun no|kidding|es broma)\b|"
                        r"(?:^|[,;] *)(no|nope|nah)$|^(don't|do not)( do it| do that)?$")
REFUSAL = re.compile(r"\b(no|not|don'?t|do not|never|nunca|todavia|yet)\b|n't\b")
# Text the user quotes: removed, and a quote left open drops the rest of the line, as does a colon ("the reviewer
# wrote: merge it"). An apostrophe inside a word ("don't") opens nothing.
QUOTED = re.compile(r"\"[^\"]*\"|“[^”]*”|‘[^’]*’|«[^»]*»|`[^`]*`|(?<!\w)'[^']*'(?!\w)")
QUOTE_OPEN = re.compile(r"[\"“”‘«»`:]|(?<!\w)'")


def action_approval_names(prompt):
    """The merge and protected-push approvals a user's message grants: its opening `allow gh pr merge` or `allow git
    push main` lines, and each clause of its own text that is a whole request (MERGE_REQUEST, PUSH_REQUEST) with no
    condition only the user can meet. A question, quoted text, an indented or fenced line and the lines after one
    ending in a colon ask nothing, and a sentence that refuses or takes back a merge or push ("Merge it, actually
    wait.", "Do not merge it") revokes what came before it."""
    allow_lines = opening_allow_lines(prompt)
    grants = set()
    for line in allow_lines:
        allow = ACTION_ALLOW_LINE.fullmatch(line)
        if allow:
            grants.add(MERGE_GRANT if allow.group(2) is None else push_grant(allow.group(2)))
    text = unicodedata.normalize("NFD", "\n".join(prompt.splitlines()[len(allow_lines):]).lower())
    text = "".join(c for c in text if not unicodedata.combining(c)).replace("’", "'")
    fenced = reported = False
    for line in text.split("\n"):
        if re.match(r"\s*(```|~~~)", line):
            fenced = not fenced
        if not line.strip():
            reported = False
        skipped = fenced or reported or re.match(r"\s*(```|~~~)|\s|>", line)
        reported = reported or line.rstrip().endswith(":")
        if skipped:
            continue
        line, *reported_rest = QUOTE_OPEN.split(QUOTED.sub(" ", line), maxsplit=1)
        for sentence in re.split(r"(?<=[.!?])\s+", line):
            sentence = sentence.strip(" .!¡")
            if "?" in sentence or "¿" in sentence:
                continue
            asked, held = set(), REQUEST_HELD.search(sentence)
            for clause in re.split(r"[,;]|\b(?:y|e|and|then)\b", sentence):
                clause = clause.strip(" .!¡")
                said = clause[REQUEST_LEADS.match(clause + " ").end():].strip()
                if REQUEST_LATER.search(said):
                    held = held or LATER_CLAUSE.fullmatch(said)
                    continue
                if MERGE_REQUEST.fullmatch(said):
                    asked.add(MERGE_GRANT)
                push = PUSH_REQUEST.fullmatch(said)
                if push:
                    asked.add(push_grant(push.group("branch")))
            if RETRACTION.search(sentence) or (asked or ACTION_WORD.search(sentence)) and REFUSAL.search(sentence):
                grants = set()
            elif not held:
                grants |= asked
        # What a colon reports asks for nothing, but a refusal there ("Note: do not merge it") still takes back.
        if any(RETRACTION.search(s.strip(" .!¡")) or ACTION_WORD.search(s) and REFUSAL.search(s)
               for rest in reported_rest for s in re.split(r"(?<=[.!?])\s+", rest)):
            grants = set()
    return sorted(grants)


def git_pushes(code):
    """Each `git push` in the code, with the protected branches it names."""
    return [(push, [b.group(1) for b in re.finditer(rf"\s(?:\S+:)?(?:refs/heads/)?{PROTECTED_BRANCHES}(?=\s|$)", push.group(1))])
            for push in re.finditer(GIT + r"push\b([^;&|\n]*)", code)]


def joined_code(command):
    """code_text() with line continuations joined, so a flag or push on the next line belongs to its command."""
    return code_text(command.replace("\\\n", " "))


def unquoted(command):
    """The command with its continuations joined and its quote marks dropped: `"--admin"` is still a flag."""
    return re.sub(r"[\"']", "", command.replace("\\\n", " "))


def pushed_branches(command):
    """The protected branches the command's pushes name, by the approval each one needs."""
    return {push_grant(b): b for _, named in git_pushes(unquoted(command)) for b in named}


# A force, delete, mirror or prune push does more to the branch than the push the user asked for.
PUSH_REWRITES = re.compile(r"--force\S*|--delete|--mirror|--prune|-[a-zA-Z]*[fd][a-zA-Z]*|[+:]\S+")


def action_grants(command):
    """The approvals the command's `gh pr merge` and protected pushes need, or None when no approval lifts their block:
    `--admin` skips the required checks, a PUSH_REWRITES push anywhere in it isn't the push asked for, and a push the
    rule reads across lines names no branch here."""
    code, flags = joined_code(command), unquoted(command)
    pushed = [w for push in re.finditer(GIT + r"push\b([^;&|\n]*)", flags) for w in push.group(1).split()]
    grants = []
    if re.search(MERGE, code):
        if re.search(r"(?<!\S)--admin\b", flags):
            return None
        grants.append(MERGE_GRANT)
    if re.search(PROTECTED_PUSH, code):
        branches = sorted(pushed_branches(command))
        if not branches:
            return None
        grants += branches
    if grants and any(PUSH_REWRITES.fullmatch(w) for w in pushed):
        return None
    return grants


def approved_action_grants(command, session, cwd):
    """The approvals that let the command's `gh pr merge` or protected `git push` run: the user's message this turn,
    or their own approval file (guard_mcp.approved), for each one action_grants() names; else `empty-remote` for a
    lone first push to a remote with no branches; else []."""
    grants = action_grants(command)
    if not grants:
        return []
    if all(design_files.guard_mcp().approved(g, session) for g in grants):
        return grants
    if MERGE_GRANT not in grants and first_push_to_empty_remote(command, cwd):
        return ["empty-remote"]
    return []


PUSH_FLAGS = {"-u", "--set-upstream", "-q", "--quiet", "-v", "--verbose", "--no-verify", "--follow-tags", "--atomic"}
MAX_PUSH_URLS = 4
# Git told where to push by something else in the command: the probe would ask the wrong remote.
GIT_REDIRECTED = re.compile(r"\bgit\s+(-c|--git-dir|--work-tree|--exec-path|--namespace)\b|\bGIT_\w*=|"
                            r"\bgit\s+(-C\s+\S+\s+)*(remote|config)\b")


def first_push_to_empty_remote(command, cwd):
    """Whether the command is one `git push [-u] <remote> <branch>` to a protected branch of a remote whose every
    push URL lists no branch. Any doubt (another push, an option that takes a value, git's config changed in the
    command, git failing or slow) is a no: git's time is bounded by the hook's own budget, so the hook can't time out
    and let the push through."""
    pushes, dirs = git_pushes(joined_code(command)), pushed_from(command, cwd)
    if len(pushes) != 1 or len(pushes[0][1]) != 1 or len(dirs) != 1 or GIT_REDIRECTED.search(unquoted(command)):
        return False
    words = pushes[0][0].group(1).split()
    if any(w.startswith("-") and w not in PUSH_FLAGS for w in words):
        return False
    words = [w for w in words if not w.startswith("-")]
    if len(words) < 2:
        return False

    def git(*args):
        if time_left() <= 0.5:  # time_left() floors at 0.1: past the deadline, every call would still get that
            raise subprocess.TimeoutExpired(args, 0)
        return subprocess.run(["git", *args], cwd=dirs[0], capture_output=True, text=True,
                              timeout=min(design_files.GIT_TIMEOUT, time_left()),
                              env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})

    try:
        urls = git("remote", "get-url", "--push", "--all", words[0])
        if urls.returncode != 0 and "/" not in words[0]:
            return False
        urls = urls.stdout.split() if urls.returncode == 0 else [words[0]]
        if len(urls) > MAX_PUSH_URLS:
            return False
        for url in urls:
            heads = git("ls-remote", "--heads", "--", url)
            if heads.returncode != 0 or heads.stdout.strip():
                return False
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def impeccable_files(command, cwd, session):
    """Why a command needs the user first under design_files.py: it names PRODUCT.md, DESIGN.md or `.impeccable`
    where none exists yet, adds them to a .gitignore, or runs Impeccable's `live`, `hooks on`/`reset` or another
    command that would create `.impeccable/`. None otherwise.

    Coarse on purpose: a name in the command counts as a write, and every folder it cd's into counts as where it
    runs. Shell is too varied to tell writes from mentions, and a wrong guess costs a question, not a file in someone
    else's repository."""
    if not re.search(r"product\.md|design\.md|impeccable", command, re.I):
        return None  # most commands: no git call
    text = without_prose(command)
    code = shell_code(text)
    # A newline inside quotes is part of an argument (sed's a\ text), not the end of a command.
    joined = re.sub(r"'[x\n]*'|\"[x\n]*\"", lambda m: m.group().replace("\n", "x"), code)
    cuts = [(0, 0)] + [m.span() for m in re.finditer(r"&&|\|\||(?<![<>&])&(?![>&])|(?<!>)\||[;\n()]", joined)]
    folders, gitignores, targets, runs = [cwd], [], [], []
    # A patch run through the shell (Codex's apply_patch) names its files in a heredoc body.
    for path in re.findall(r"^\*\*\* (?:Add File|Update File|Move to): (.+)$", command, re.M):
        (gitignores if os.path.basename(path).lower() == ".gitignore" else targets).append(path)
    for (_, start), (boundary, _) in zip(cuts, cuts[1:] + [(len(code), len(code))]):
        if re.fullmatch(r"\s*x*\s*", code[start:boundary]):  # a heredoc body line, not a command
            continue
        segment = text[start:boundary]
        cd = re.match(r"\s*cd\s+(\"[^\"]*\"|'[^']*'|\S+)", segment)
        if cd:
            folders.append(cd_into(folders[-1], cd.group(1)))
        words = WORD.findall(segment)
        words = words[next((i for i, w in enumerate(words) if not LEADING.fullmatch(w)), len(words)):]
        if not words or (words[0] in READERS and ">" not in code[start:boundary]):
            continue
        if any(os.path.basename(w).lower() == ".gitignore" for w in words):
            gitignores += [w for w in words if os.path.basename(w).lower() == ".gitignore"]
            continue  # the names in it are ignore rules, judged below
        targets += [w for w in words if design_files.DESIGN_NAME.search(w)]
        args = [w for w in words[1:] if not w.startswith("-")]
        if os.path.basename(words[0]) in COPY_COMMANDS and args:
            # Into a folder (or ln's implicit `.`), each source keeps its name there.
            dest = args[-1] if len(args) > 1 else "."
            targets += [os.path.join(dest, os.path.basename(a.rstrip("/"))) for a in args[:-1] or args
                        if design_files.DESIGN_NAME.search(os.path.basename(a.rstrip("/")))]
        if os.path.basename(words[0]).split("@")[0] == "impeccable":
            runs.append(words[1:])
    for folder in dict.fromkeys(folders):
        why = (design_files.blocked(targets, folder, session)
               or design_files.blocked(gitignores, folder, session, command)
               or next(filter(None, (impeccable_run(rest, folder, session) for rest in runs)), None))
        if why:
            return why
    return None


def impeccable_run(rest, folder, session):
    """Why running `impeccable <rest>` in folder needs the user first, or None."""
    if IMPECCABLE_FLAGS_ONLY & set(rest):
        return None
    named = [w for w in rest if not w.startswith("-")]
    sub, action = (named or ["help"])[0], (named[1:] or [""])[0]
    if sub == "live":
        name, what = "impeccable live", "Starting `impeccable live`, which injects a script into project files,"
    elif sub in ("hooks", "hook-admin") and action in ("on", "reset"):
        name, what = f"hooks {action}", f"Running `impeccable {sub} {action}`, which writes hook settings into the project,"
    elif (sub in READ_ONLY_IMPECCABLE and "--fix" not in rest) or (sub in ("hooks", "hook-admin") and action in ("", "status")):
        return None
    elif os.path.isdir(os.path.join(folder, ".impeccable")):  # Impeccable writes .impeccable/ where it runs
        return None
    else:
        name, what = ".impeccable", f"Running `impeccable {sub}`, which creates .impeccable/,"
    if design_files.approved(design_files.APPROVALS[name], session) or not design_files.shared_root(folder):
        return None
    return design_files.reason(what, name)


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

    for match in re.finditer(r"(?:^|[;&|(\n`])[^\S\n]*(" + PREFIXES + r")" + GH_PR + r"(?:" + PR_CREATE + r"|edit)\b", code):
        before = command[:match.start(1)]
        # gh reads a relative --body-file from where it runs, not the --head worktree pr_checkout() may pick.
        # The cds are found in the code, where a quote may read as ; around eval's argument, and read from the command.
        runs_in = cwd
        for cd in re.finditer(r"(?:^|[;&|\n][^\S\n]*)(cd)\s+(\"[^\"]+\"|'[^']+'|;[^;\n]+;|[^\s;&|]+)", code[:match.start(1)]):
            if not quote_around(cd.start(1)):
                runs_in = cd_into(runs_in, command[cd.start(2):cd.end(2)])
        exported = dict(re.findall(r"(?:^|[;&|\n][^\S\n]*)(?:export\s+)?(GH_REPO|GH_HOST)=([^\s;&|]+)(?=\s*(?:[;&|\n]|$))",
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
        if not ("$" in (repo or "") or targets_kit(repo, host, pr_checkout(shlex.join(args), runs_in))):
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
                    "private, and the kit is public. Rewrite it without them; `python3 ~/.agents/hooks/private_terms.py "
                    "pr <title> <body file>` shows the line each one is on.")
    return None


def pushed_from(command, cwd):
    """The directory each `git push` in the command runs in, following earlier `cd`s outside a closed subshell and
    every `git -C`."""
    command = without_scripts(command)[0]
    code, dirs = shell_code(command), []

    def word(start, end):
        return command[start:end].replace("\\ ", " ")

    for push in re.finditer(GIT + r"push\b", code):
        runs_in = cwd
        for cd in re.finditer(r"(?:^|[;&|(\n])[^\S\n]*cd\s+((?:\\ |[^\s;&|)])+)", code[:push.start()]):
            between = code[cd.end():push.start()]
            if between.count(")") <= between.count("("):
                runs_in = cd_into(runs_in, word(cd.start(1), cd.end(1)))
        for option in re.finditer(r"-C\s+((?:\\ |[^\s;&|)])+)", code[push.start():push.end()]):
            runs_in = cd_into(runs_in, word(push.start() + option.start(1), push.start() + option.end(1)))
        dirs.append(runs_in)
    return dirs


def record_push(command, cwd, payload):
    """Note the checkout and time of each allowed `git push`, so the stop hook follows the CI of what it pushed."""
    roots = {subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=d, capture_output=True, text=True,
                            timeout=time_left()).stdout.strip()
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
    global deadline
    deadline = time.monotonic() + HOOK_BUDGET
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if isinstance(command, list):
        command = " ".join(command)
    if not command:
        return 0
    command = without_evidence_runner(command)
    cwd = payload.get("cwd") or ""
    approved, action_approved = [], None
    try:
        cwd = os.path.realpath(cwd or os.getcwd())  # getcwd raises when the directory was deleted
        reason = (pr_checkout_unknown(command, payload) or dangerous_rm(command, cwd)
                  or private_terms_in_kit_pr(command, cwd) or unreviewed_pr(command, cwd)
                  or impeccable_files(command, cwd, payload.get("session_id"))
                  or grants_approval(command) or switches_phase(command) or touches_approvals(command, cwd))
        if not reason:
            code = code_text(command)
            for pattern, why in RULES:
                # A SQL client or driver runs the quoted statements and scripts every other rule reads as text.
                if re.search(pattern, command if pattern == DATABASE_DESTROY and SQL_USED.search(without_prose(command)) else code):
                    if pattern == DATABASE_DESTROY:
                        granted = approved_database_grants(command, payload.get("session_id"))
                        if granted:
                            approved += granted
                            continue
                    if pattern in (MERGE, PROTECTED_PUSH):
                        if action_approved is None:  # both rules can match one command; git runs once
                            action_approved = approved_action_grants(command, payload.get("session_id"), cwd)
                        if action_approved:
                            approved += action_approved
                            continue
                    reason = why if re.search(pattern, bare_code(command)) else f"{why} {TEXT_HINT}"
                    break
    except Exception as error:  # every harness lets a command through when its hook crashes or times out
        reason = (f"The guard failed ({type(error).__name__}: {error}), so it can't tell whether this command is safe. "
                  "Tell the user: the error is in ~/.agents/logs/hooks.jsonl.")
    if not reason:
        if approved:  # the grants, not the command: it can carry a password (PGPASSWORD=…)
            log("guard_bash", "allow-approved", payload, ", ".join(dict.fromkeys(approved)))
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
