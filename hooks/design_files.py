"""Impeccable's project files in a repository other people work in: PRODUCT.md, DESIGN.md and `.impeccable/` are
created, or added to .gitignore, only when the user's message in this turn allows it. guard_files.py checks file
tools with it, guard_bash.py shell commands, Impeccable's own `live` and `hooks on`/`reset` included.

A repository is shared when it has a remote whose host/owner no profile lists in personal-repos.txt (one `host/owner`
per line): a fork with a work upstream is shared, and a forgotten list costs a question, not a file in someone else's
repository. Names match in any case, as macOS's filesystem does. Stops a careless agent, not a determined one; a
path a script builds at runtime gets through.
"""
import functools
import glob
import os
import re
import subprocess

from review_stamp import repo_id

# What the user names to allow it -> the approval it grants. The `design.` prefix keeps them apart from services.
APPROVALS = {
    "PRODUCT.md": "design.product-md",
    "DESIGN.md": "design.design-md",
    ".impeccable": "design.impeccable-dir",
    "impeccable live": "design.impeccable-live",
    "hooks on": "design.impeccable-hooks",
    "hooks reset": "design.impeccable-hooks",
}
FILES = ("PRODUCT.md", "DESIGN.md", ".impeccable")
GITIGNORE = "design.gitignore"  # granted by naming .gitignore together with one of the files
DESIGN_NAME = re.compile(r"(?<![\w-])(PRODUCT\.md|DESIGN\.md|\.impeccable)(?![\w-]|\.\w)", re.I)
# "no crees PRODUCT.md", "don’t touch DESIGN.md", "DESIGN.md? not yet": a refusal near the name grants nothing.
# A sentence ends at . ! ? followed by a space, not at the dot inside PRODUCT.md.
CLAUSE = r"(?:[^.!?\n]|[.!?](?!\s))"
REFUSAL_BEFORE = re.compile(rf"(?<!\w)(?:no|not|don['’]?t|do not|never|without|sin|nunca|ni){CLAUSE}{{0,80}}$", re.I)
REFUSAL_AFTER = re.compile(rf"^[?:,]?\s*{CLAUSE}{{0,15}}(?<!\w)(?:no|not yet|todavía no|aún no)(?!\w)", re.I)
GIT_TIMEOUT = 3


def guard_mcp():
    """guard_mcp reads every profile's mcp-writes.json when imported: a broken one must not take the shell guard
    down with it, so it loads only when a design file is at stake (inside the callers' error handling)."""
    import guard_mcp as module
    return module


def approved(grant, session):
    return guard_mcp().approved(grant, session)


def approval_names(prompt):
    """The approvals a user's message grants: each item it names as a word of its own, not next to a refusal."""
    def named(name):
        return any(not REFUSAL_BEFORE.search(prompt[:m.start()]) and not REFUSAL_AFTER.search(prompt[m.end():])
                   for m in re.finditer(rf"(?<![\w.-]){re.escape(name)}(?![\w-]|\.\w)", prompt, re.I))
    grants = {grant for name, grant in APPROVALS.items() if named(name)}
    if named(".gitignore") and grants & {APPROVALS[n] for n in FILES}:
        grants.add(GITIGNORE)
    return sorted(grants)


def personal_owners():
    owners = set()
    for path in glob.glob(os.path.join(guard_mcp().PROFILES, "*", "personal-repos.txt")):
        with open(path, encoding="utf-8") as fh:
            owners |= {line.strip().lower().strip("/") for line in fh if line.strip() and not line.startswith("#")}
    return owners


@functools.lru_cache(maxsize=None)  # one hook run asks about the same folder several times
def shared_root(directory):
    """The top of the shared repository `directory` is in, or None: not a repository, no remote, or only personal
    ones. When git fails, hangs or can't start, the repository can't be shown to be personal: it counts as shared."""
    def git(*args):
        return subprocess.run(["git", *args], cwd=directory, capture_output=True, text=True, timeout=GIT_TIMEOUT,
                              env={**os.environ, "LC_ALL": "C"})  # the "not a git repository" check reads English

    try:
        top = git("rev-parse", "--show-toplevel")
        if top.returncode != 0:
            return None if "not a git repository" in top.stderr else directory
        remotes = git("config", "--get-regexp", r"^remote\..*\.url$")
    except (OSError, subprocess.TimeoutExpired):
        return directory
    if remotes.returncode not in (0, 1):  # 1: no remote at all
        return directory
    urls, mine = remotes.stdout.split()[1::2], personal_owners()
    if not urls or all(f"{host}/{slug.split('/')[0]}" in mine for host, slug in map(repo_id, urls)):
        return None
    return top.stdout.strip()


def existing_folder(path):
    """The nearest folder of `path` that exists: where git can tell which repository a new file would go in."""
    folder = os.path.dirname(path)
    while folder and not os.path.isdir(folder):
        folder = os.path.dirname(folder)
    return folder or "/"


def creation_grant(path):
    """The approval creating `path` needs, when it creates PRODUCT.md, DESIGN.md or a `.impeccable/` folder."""
    parts = os.path.normpath(path).split(os.sep)
    for i, part in enumerate(parts):
        name = next((n for n in FILES if part.lower() == n.lower()), None)
        if name == ".impeccable" or (name and i == len(parts) - 1):
            return None if os.path.exists(os.sep.join(parts[:i + 1]) or os.sep) else APPROVALS[name]
    return None


def new_ignore_rules(path, additions):
    """Ignore rules for Impeccable's files that `additions` brings and the .gitignore at `path` doesn't have yet."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            listed = {line.strip().lower() for line in fh if line.strip() and line.strip()[0] not in "#!"}
    except OSError:
        listed = set()
    words = (w.strip("\"'") for w in re.split(r"\s+", additions))
    return sorted({w for w in words if DESIGN_NAME.search(w) and w[:1] not in ("#", "!") and w.lower() not in listed})


def blocked(paths, cwd, session, gitignore_additions=""):
    """Why writing `paths`, and adding `gitignore_additions` to any .gitignore among them, needs the user first."""
    for path in paths:
        path = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
        grant = creation_grant(path)
        if grant and not approved(grant, session) and shared_root(existing_folder(path)):
            name = next(n for n, g in APPROVALS.items() if g == grant)
            return reason(f"Creating {name + '/' if name == '.impeccable' else name}", name)
        if os.path.basename(path).lower() == ".gitignore" and gitignore_additions:
            rules = new_ignore_rules(path, gitignore_additions)
            if rules and not approved(GITIGNORE, session) and shared_root(existing_folder(path)):
                return reason(f"Adding {', '.join(rules)} to .gitignore", f".gitignore` together with `{rules[0]}")
    return None


def reason(action, name):
    return (f"{action} in a repository other people work in needs the user's say-so first (AGENTS.md → Design). "
            f"Ask them; their next message naming `{name}` allows it for that turn. If they say no, carry on without it.")
