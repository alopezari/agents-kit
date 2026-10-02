"""Impeccable's project files in a repository other people work in: PRODUCT.md, DESIGN.md and `.impeccable/` are
created, gitignored, or Impeccable edits project files (`live`, `hooks on`/`reset`), only when the user's message in
this turn allows it. Shared by guard_files.py (file tools) and guard_bash.py (shell).

Shared means the checkout has an origin whose host/owner no profile lists in personal-repos.txt (one `host/owner`
per line): a forgotten list costs a question, not a file in someone else's repository. Stops a careless agent, not a
determined one; a path built at runtime by a script gets through.
"""
import glob
import os
import re
import subprocess

from guard_mcp import APPROVALS as APPROVALS_DIR, PROFILES, TURN_APPROVALS

# What the user names to allow it -> the approval it grants. The `design.` prefix keeps them apart from services.
APPROVALS = {
    "PRODUCT.md": "design.product-md",
    "DESIGN.md": "design.design-md",
    ".impeccable": "design.impeccable-dir",
    "impeccable live": "design.impeccable-live",
    "hooks on": "design.impeccable-hooks",
    "hooks reset": "design.impeccable-hooks",
    ".gitignore": "design.gitignore",
}
# "no crees PRODUCT.md", "don't touch DESIGN.md": a refusal close before the name grants nothing.
REFUSAL = re.compile(r"(?<!\w)(?:no|not|don'?t|do not|never|without|sin|nunca|ni)(?!\w)[^.!?\n]{0,40}$", re.I)
DESIGN_NAMES = re.compile(r"(?<![\w.-])(PRODUCT\.md|DESIGN\.md|\.impeccable)(?![\w.-])")
GIT_TIMEOUT = 3


def approval_names(prompt):
    """The approvals a user's message grants: each item it names, as a word of its own, not just after a refusal."""
    return sorted({grant for name, grant in APPROVALS.items()
                   for match in re.finditer(rf"(?<![\w.-]){re.escape(name)}(?![\w-])", prompt, re.I)
                   if not REFUSAL.search(prompt[:match.start()])})


def approved(session, grant):
    session = re.sub(r"[^\w-]", "", str(session or ""))
    return bool(session) and os.path.exists(os.path.join(TURN_APPROVALS, session, grant))


def personal_owners():
    owners = set()
    for path in glob.glob(os.path.join(PROFILES, "*", "personal-repos.txt")):
        with open(path, encoding="utf-8") as fh:
            owners |= {line.strip().lower().strip("/") for line in fh if line.strip() and not line.startswith("#")}
    return owners


def shared_root(path):
    """The top of the shared repository holding `path`, or None: not in a repository, no origin, or a personal one."""
    directory = path if os.path.isdir(path) else os.path.dirname(path) or "."
    while directory and not os.path.isdir(directory):  # a path whose folders don't exist yet
        directory = os.path.dirname(directory)

    def git(*args):
        # A slow or broken git can't prove the repository is personal: it counts as shared.
        result = subprocess.run(["git", *args], cwd=directory or "/", capture_output=True, text=True, timeout=GIT_TIMEOUT)
        return result.stdout.strip()

    try:
        root = git("rev-parse", "--show-toplevel")
        if not root:
            return None
        remotes = git("remote", "-v").split()
    except (OSError, subprocess.TimeoutExpired):
        return directory or "/"
    urls = {word for word in remotes[1::3]}
    if not urls:
        return None
    from guard_bash import repo_id  # noqa: E402 — guard_bash imports this module too
    mine = personal_owners()
    personal = all("/".join((host or "", slug.split("/")[0])) in mine for host, slug in map(repo_id, urls))
    return None if personal else root


def creation_grant(path):
    """The approval creating `path` needs, when it creates PRODUCT.md, DESIGN.md or the `.impeccable/` folder."""
    name = os.path.basename(path.rstrip("/"))
    if name in ("PRODUCT.md", "DESIGN.md"):
        return None if os.path.exists(path) else APPROVALS[name]
    parts = os.path.normpath(path).split(os.sep)
    if ".impeccable" in parts:
        folder = os.sep.join(parts[:parts.index(".impeccable") + 1]) or os.sep
        return None if os.path.isdir(folder) else APPROVALS[".impeccable"]
    return None


def blocked(paths, cwd, session, gitignore_additions=""):
    """Why writing `paths` (and adding `gitignore_additions` to a .gitignore) needs the user first, or None."""
    for path in paths:
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
        grant = creation_grant(path)
        if grant and not approved(session, grant) and shared_root(path):
            return reason(f"Creating {os.path.basename(path) if 'impeccable' not in grant else '.impeccable/'}", grant)
        if os.path.basename(path) == ".gitignore" and gitignore_additions and shared_root(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    listed = set(DESIGN_NAMES.findall(fh.read()))
            except OSError:
                listed = set()
            if set(DESIGN_NAMES.findall(gitignore_additions)) - listed and not approved(session, APPROVALS[".gitignore"]):
                return reason("Adding Impeccable's files to .gitignore", APPROVALS[".gitignore"])
    return None


def reason(action, grant):
    name = next(name for name, g in APPROVALS.items() if g == grant)
    return (f"{action} in a repository other people work in needs the user's say-so first (AGENTS.md → Design). "
            f"Ask them; their next message naming `{name}` allows it for that turn. If they say no, carry on without it.")


READ_ONLY_IMPECCABLE = {"context", "detect", "doctor", "help", "--help", "-h", "version", "--version", "live-status"}
READ_ONLY_FLAGS_OFF = {"doctor": "--fix"}  # `doctor --fix` repairs project files
WRITE_COMMANDS = {"cp", "mv", "install", "ln", "mkdir", "touch", "tee"}


def blocked_command(command, cwd, session):
    """Why a shell command needs the user first: it writes PRODUCT.md, DESIGN.md or `.impeccable/` (or adds them to
    .gitignore), or runs an Impeccable command that creates `.impeccable/` or edits project files. None otherwise."""
    for segment in re.split(r"&&|\|\||[;|\n]", command):
        words = re.findall(r"\"[^\"]*\"|'[^']*'|\S+", segment)
        words = [w.strip("\"'") for w in words]
        if not words:
            continue
        if words[0] == "cd" and len(words) > 1:  # later segments run there
            cwd = os.path.normpath(os.path.join(cwd, os.path.expanduser(words[1])))
            continue
        targets = [words[i + 1] for i, w in enumerate(words[:-1]) if re.fullmatch(r"\d?>>?|&>", w)]
        targets += [m for m in re.findall(r"\d?>>?(\S+)", segment) if not m.startswith(">")]
        program = os.path.basename(words[0])
        args = [w for w in words[1:] if not w.startswith("-")]
        if program in ("mkdir", "touch", "tee"):
            targets += args
        elif program in WRITE_COMMANDS and args:
            targets.append(args[-1])
        if re.search(r"write_text|open\([^)]*['\"][wa]", segment):
            targets += [m.group(0) for m in re.finditer(r"[\w./~-]*(?:PRODUCT\.md|DESIGN\.md|\.impeccable[\w./-]*)", segment)]
        targets = [t.strip("\"'") for t in targets]
        gitignore = segment if any(os.path.basename(t) == ".gitignore" for t in targets) else ""
        why = blocked(targets, cwd, session, gitignore)
        if why:
            return why
        if program == "impeccable" or (program == "npx" and len(words) > 1 and words[1] == "impeccable"):
            rest = words[2:] if program == "npx" else words[1:]
            sub = rest[0] if rest else "help"
            if READ_ONLY_FLAGS_OFF.get(sub) in rest or (sub == "hooks" and rest[1:2] not in ([], ["status"])
                                                       and rest[1] not in ("on", "reset")):
                sub = f"{sub}-writing"
            if sub.startswith("live") and sub not in READ_ONLY_IMPECCABLE:
                grant, action = APPROVALS["impeccable live"], f"Running `impeccable {sub}`, which injects a script into project files,"
            elif sub == "hooks" and len(rest) > 1 and rest[1] in ("on", "reset"):
                grant, action = APPROVALS[f"hooks {rest[1]}"], f"Running `impeccable hooks {rest[1]}`, which writes hook settings into the project,"
            elif sub not in READ_ONLY_IMPECCABLE:
                root = shared_root(cwd)
                if not root or os.path.isdir(os.path.join(root, ".impeccable")):
                    continue
                grant, action = APPROVALS[".impeccable"], f"Running `impeccable {sub}`, which creates .impeccable/,"
            else:
                continue
            if not approved(session, grant) and shared_root(cwd):
                return reason(action, grant)
    return None
