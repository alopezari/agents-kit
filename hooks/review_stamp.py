#!/usr/bin/env python3
"""Record or check that a skill ran on the exact current change.

  review_stamp.py write [--kind review|validate]   # end of review / validate; verify stamps come only from
                                                   # its runner (stop_checks.py), through record_verify(); validate
                                                   # needs a report whose PASS/FAIL rows name their evidence, no empty
                                                   # recording among it, no output recorded before a change to files
                                                   # other than tests, every criterion of the spec covered by a PASS
                                                   # and a negative row (or a reasoned NOT RUN when its check is
                                                   # manual), a Test types row for each type bin/triage selects, and,
                                                   # where ~/.agents/repos/<repo>/suite lists the full suite's commands,
                                                   # a green bin/evidence run of one on the code as it is now
  review_stamp.py check [--kind review|validate|verify|verify-empty]   # exit 1 if the change differs from the stamped one
  review_stamp.py needs-validate                   # exit 0 if the change touches behavior, not just tests/docs
  review_stamp.py fingerprint                      # the code's fingerprint, as verify's stamp holds it (bin/evidence records it)
  review_stamp.py follow-renames                   # move spec, reports and stamps from this branch's earlier names, and out of $TMPDIR
  review_stamp.py branch-key                       # the current branch as it appears in those file names
  review_stamp.py base [--matches <branch>]        # (both options also take --name=value) the ref the change is measured against (see branch_base), or
                                                   # exit 1, saying why, when a PR targeting <branch> (empty: the
                                                   # default branch) would show a different change

A stamp covers every file that differs from the merge-base with the branch's base (the default branch, or the
parent of a stacked branch: branch_base), except the changelog, so committing stamped changes keeps it valid
and any later edit invalidates it. Review and validate stamps hold the branch's diff, so merging the default branch
keeps them unless it moves the changed lines or their context; verify's holds the files' content, so the tests run again. Stamps live in the repository's shared git dir, per branch
(.git/agents/stamps/<branch key>/), never in the tree: they survive removing the worktree they were
written in, so they are still read from the main checkout once kit-ship frees the branch. They also follow `git branch -m`: agents often write the spec on a session branch and rename it afterwards.
"""
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

# Where path.sh keeps a spec and its reports while a sandbox makes .git read-only.
TMP_SPECS = os.path.join(os.environ.get("TMPDIR") or "/tmp", "agents-specs")
TEST_PATH = r"(^|/)(tests?|__tests__|spec)/|[._-](test|spec)\.[a-z]+$|Test\.php$"
TEST_FILE = re.compile(TEST_PATH, re.I)
NOT_BEHAVIOR = re.compile(TEST_PATH + r"|(^|/)docs?/|\.(md|txt|rst)$|(^|/)(CHANGELOG|README)", re.I)
SPEC_LOOKUP_SECS = 10
EVIDENCE_LINE_BYTES = 64 * 1024
TRIAGE_SECS = 60
NEGATIVE_CASE = re.compile(r"[\u2212\u00b1]|(?<![\w-])-(?![\w-])")  # −, ± or a lone hyphen; `regression` and `+` aren't
# A criterion no test can check: its `verify:` starts with manual/by hand (not a path: ./manual.py), or says it can't be automated.
MANUAL_VERIFY = re.compile(r"[\s*_]*(manual(ly)?|by hand)(?!\.?[\w/-])|.*\b(can[\u2019']t be automated|cannot be automated|not automatable)", re.I)
# CI checks the changelog entry, and it is often written last: adding it re-runs nothing.
NOT_STAMPED = re.compile(r"(^|/)CHANGELOG(\.(md|txt|rst))?\Z|(^|/)changelog\.d/[^/]+\.md\Z", re.I)
VERIFY_KINDS = ("verify", "verify-empty")
STAMP_KINDS = ("review", "validate", *VERIFY_KINDS)
COMMANDS = ("check", "write", "base", "branch-key", "follow-renames", "needs-validate", "fingerprint")
# Normal calls take milliseconds. A git that hangs (a stuck lock, a network filesystem) would hang every stop and PR
# gate that asks it. The stamps, the stop hook and the verify scripts' lookups run git through run_git; phase_switches
# and the branch-name notice set their own timeout, and --red-check's tree copy runs under verify's limit.
GIT_TIMEOUT = 30


class GitTimeout(RuntimeError):
    pass


def run_git(args, cwd=None, env=None, text=True):
    """git's completed process; raises RuntimeError naming the command when it runs past GIT_TIMEOUT."""
    try:
        return subprocess.run(["git", *args], capture_output=True, text=text, cwd=cwd, env=env, timeout=GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        command = next((arg for at, arg in enumerate(args)
                        if not arg.startswith("-") and (at == 0 or args[at - 1] not in ("-c", "-C"))), "")
        raise GitTimeout(f"git {command} timed out after {GIT_TIMEOUT}s") from None


def git(*args, cwd=None):
    return run_git(args, cwd=cwd).stdout.strip()


def git_checked(*args, env=None):
    """git's output as bytes; a failure raises, since an empty answer would make every change look stamped."""
    result = run_git(args, env=env, text=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {' '.join(result.stderr.decode(errors='replace').split())}")
    return result.stdout


def git_paths(command, *args):
    """The paths a git listing prints, NUL-separated so no name is quoted or split."""
    return [os.fsdecode(p) for p in git_checked(command, "-z", *args).split(b"\0") if p]


def base_ref(cwd=None):
    remote_head = git("symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD", cwd=cwd)
    candidates = [remote_head] if remote_head else []
    candidates += ["origin/trunk", "origin/main", "origin/master", "origin/develop", "trunk", "main", "master", "develop"]
    for ref in candidates:
        if git("rev-parse", "--verify", "--quiet", ref, cwd=cwd):
            return ref
    return "HEAD"


def repo_name(cwd=None):
    """The main checkout's directory name, the same from any worktree: the key of ~/.agents/repos/<name>."""
    return repo_name_of(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd))


def phase_cache(common, branch):
    """bin/phase's cached label for a branch of the repo whose git common dir is `common`."""
    return os.path.join(common, "agents", "phase", branch_key(branch) + ".json")


def phase_lock(common, branch):
    """bin/phase's lock on refreshing that label."""
    return os.path.join(common, "agents", "phase", branch_key(branch) + ".lock")


def repo_name_of(common):
    """repo_name() from the absolute git common dir."""
    name = os.path.basename(common)
    return os.path.basename(os.path.dirname(common)) if name == ".git" else name[:-len(".git")] if name.endswith(".git") else name


def branch_base(cwd=None):
    """(ref, why) this branch's change is measured against: the base recorded in `branch.<name>.agentsBase`, else the
    nearest pushed branch in its first-parent history (the parent of a stacked branch), else the default branch, with
    an empty why. A nearer base shrinks what the stamps cover, so only positive evidence moves it."""
    default = base_ref(cwd)
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd)
    if not branch:
        return default, ""
    # Not all of @{upstream}: a branch started from origin/parent tracks its parent; after `git push -u` it is the
    # branch's own copy, which the same-named refs below already cover.
    own = {branch} | {f"{remote}/{branch}" for remote in git("remote", cwd=cwd).split()}
    own_full = {f"refs/heads/{branch}"} | {f"refs/remotes/{ref}" for ref in own - {branch}}
    recorded = git("config", "--get", f"branch.{branch}.agentsBase", cwd=cwd)
    # HEAD and refs/heads/x spell the branch too; checked before the origin/ copy, as origin/HEAD is the default branch.
    if recorded and git("rev-parse", "--symbolic-full-name", recorded, cwd=cwd) in own_full:
        recorded = ""
    for ref in (f"origin/{recorded}", recorded) if recorded else ():
        own_copy = git("rev-parse", "--symbolic-full-name", ref, cwd=cwd) in own_full
        if not own_copy and git("rev-parse", "--verify", "--quiet", ref, cwd=cwd):
            if ref == default:
                return default, ""
            if git("merge-base", "HEAD", ref, cwd=cwd):
                return ref, f"recorded in branch.{branch}.agentsBase"
            break
    fork_point = git("merge-base", "HEAD", default, cwd=cwd)
    if not fork_point:
        return default, ""
    tips = {}
    for line in git("for-each-ref", "--format=%(objectname) %(refname:short)", "refs/remotes", cwd=cwd).splitlines():
        sha, ref = line.split(" ", 1)
        if ref not in own and "/" in ref and not ref.endswith("/HEAD"):  # origin/HEAD's short name is "origin"
            tips.setdefault(sha, ref)
    # HEAD itself is skipped: a pushed branch there would leave nothing to review.
    history = git("rev-list", "--first-parent", "--max-count=1000", f"{fork_point}..HEAD", cwd=cwd).split()[1:]
    ref = next((tips[sha] for sha in history if sha in tips), None)
    return (ref, f"{ref} is in this branch's history") if ref else (default, "")


def branch_name(ref, cwd=None):
    """A base ref as `gh pr create --base` names it: without its remote, so origin/feat/x is feat/x and a local
    feat/x stays feat/x."""
    full = git("rev-parse", "--symbolic-full-name", ref, cwd=cwd)
    return full.removeprefix("refs/remotes/").partition("/")[2] if full.startswith("refs/remotes/") else ref


def merge_base(cwd=None):
    """Where this branch left its base (branch_base); HEAD when on the default branch itself, or on a branch with no
    commit yet. Raises when they share no commit: measuring from HEAD would leave every committed line unchecked."""
    base = branch_base(cwd)[0]
    found = run_git(["merge-base", "HEAD", base], cwd=cwd)
    if found.returncode == 0:
        return found.stdout.strip()
    if not git("rev-parse", "--verify", "--quiet", "HEAD", cwd=cwd):
        return "HEAD"
    if found.returncode != 1:  # 1 is git's "no common ancestor"; anything else is git failing
        raise RuntimeError(f"git merge-base HEAD {base} failed: {' '.join(found.stderr.split())}")
    branch = git("branch", "--show-current", cwd=cwd)
    raise RuntimeError(f"git merge-base HEAD {base} found no common commit, so the change can't be measured. Record "
                       f"the branch it starts from with `git config branch.{branch}.agentsBase <branch>`, or fetch "
                       "the history a shallow clone is missing (`git fetch --unshallow`).")


def changed_paths():
    merge_base_sha = merge_base()
    paths = set(git_paths("diff", "--no-renames", "--name-only", merge_base_sha))  # a rename hides its source
    paths |= set(git_paths("ls-files", "--others", "--exclude-standard"))
    return merge_base_sha, sorted(p for p in paths if p)


def fingerprint():
    base, paths = changed_paths()
    digest = hashlib.sha256(base.encode())
    for path in (p for p in paths if not NOT_STAMPED.search(p)):
        digest.update(os.fsencode(path))
        digest.update(git_checked("hash-object", "--", path).strip() if os.path.isfile(path) else b"<deleted>")
    return digest.hexdigest()


def change_fingerprint():
    """The branch's own change: its diff against where it left the default branch, untracked files included, without
    the hunks' line numbers or the base's blob ids. Merging the default branch keeps it unless the merge moves the
    changed lines or their context."""
    base, paths = changed_paths()
    stamped = [f":(literal){p}" for p in paths if not NOT_STAMPED.search(p)]
    digest = hashlib.sha256()
    if not stamped:
        return digest.hexdigest()
    # Untracked files enter a copy of the index as intent-to-add, so they diff exactly as they will once added.
    with index_copy() as env:
        untracked = git_paths("ls-files", "--others", "--exclude-standard", "--", *stamped)
        commands = [["add", "--intent-to-add", "--", *(f":(literal){p}" for p in untracked)]] if untracked else []
        commands.append(["diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "--binary", "-U3", base,
                         "--", *stamped])
        for args in commands:
            # Bytes: decoding could make two different changes hash alike.
            patch = git_checked(*args, env=env)
    for line in patch.split(b"\n"):
        if not line.startswith(b"index "):
            digest.update(re.sub(rb"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", b"@@", line) + b"\n")
    return digest.hexdigest()


@contextlib.contextmanager
def index_copy():
    """An environment for git commands that stage into a copy of the repository's index (an empty one before the
    first commit), so the real index is never touched."""
    fd, index = tempfile.mkstemp(prefix="agents-stamp-index-")
    os.close(fd)
    try:
        real_index = os.fsdecode(git_checked("rev-parse", "--path-format=absolute", "--git-path", "index").rstrip(b"\n"))
        if os.path.exists(real_index):
            shutil.copyfile(real_index, index)
        else:
            os.remove(index)
        yield {**os.environ, "GIT_INDEX_FILE": index}
    finally:
        if os.path.exists(index):
            os.remove(index)


def working_tree():
    """The id of a tree holding the working tree's content, untracked files included, as bin/evidence records it."""
    with index_copy() as env:
        git_checked("add", "-A", env=env)
        return git_checked("write-tree", env=env).decode().strip()


def fingerprint_for(kind):
    # Verify reruns the tests on the code as it is, the default branch's changes included; review and validation
    # judged the branch's own change, which a merge of the default branch doesn't alter.
    return fingerprint() if kind in VERIFY_KINDS else change_fingerprint()


# Files skills/kit-spec/path.sh and bin/reports name <kind>-<repo>-<branch key>.md, next to the spec.
REPORT_KINDS = ("spec", "verify", "review", "validation", "checks-guide", "staging-guide", "follow-pr", "pr-body")


def branch_file_names(repo, key):
    return [f"{kind}-{repo}-{key}.md" for kind in REPORT_KINDS] + [f"browser-ab-spec-{repo}-{key}.json", f"evidence-{repo}-{key}"]


def renamed_from(branch):
    """Earlier names of this branch, newest first: git carries a branch's reflog across renames."""
    entries = git("reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    return re.findall(r"^Branch: renamed refs/heads/(.+) to refs/heads/", entries, re.M)


def branch_key(branch):
    """The branch in file names. `~` can't appear in a branch name, so `a/b` and `a-b` never share files."""
    return branch.replace("/", "~")


def follow_branch_renames():
    """Move the spec, reports and stamps kept under an earlier name or key of this branch to its current key, logging
    each rename that moved something, then move them out of $TMPDIR once .git is writable."""
    branch = git("branch", "--show-current")
    if not branch:
        return
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    repo, new = repo_name(), branch_key(branch)
    dirs = (os.path.join(common, "agents"), TMP_SPECS)
    earlier = renamed_from(branch)
    # Keys used to turn / into -: such a key is this branch's, unless a branch with that name really exists.
    dashed = [name.replace("/", "-") for name in [branch, *earlier] if "/" in name]
    for old, old_name in [(branch_key(name), name) for name in earlier] + [(key, None) for key in dashed]:
        moved = False
        moves = [(os.path.join(d, src), os.path.join(d, dst)) for d in dirs
                 for src, dst in zip(branch_file_names(repo, old), branch_file_names(repo, new))]
        moves.append((os.path.join(common, "agents", "stamps", old), os.path.join(common, "agents", "stamps", new)))
        moves.append((os.path.join(common, "agents", "phase", f"{old}.json"), os.path.join(common, "agents", "phase", f"{new}.json")))
        for src, dst in moves:
            if os.path.exists(src) and not os.path.exists(dst) and not (old in dashed and branch_exists(old)):
                try:
                    os.rename(src, dst)
                    moved = True
                    if dst.endswith(".md"):  # reports link their evidence by its directory's name
                        relink_evidence(dst, f"evidence-{repo}-{old}", f"evidence-{repo}-{new}")
                    print(f"moved {os.path.basename(src)} to {os.path.basename(dst)}", file=sys.stderr)
                except OSError as error:  # a sandbox with a read-only .git: the caller falls back to $TMPDIR
                    print(f"could not move {src} to {dst}: {error}", file=sys.stderr)
        if moved and old_name:
            log_rename(repo, old_name, branch)
    move_out_of_tmpdir(common, repo, new)


def relink_evidence(report, old_dir, new_dir):
    with open(report) as fh:
        text = fh.read()
    if old_dir in text:
        with open(report, "w") as fh:
            fh.write(text.replace(old_dir, new_dir))


def quality_log():
    """The main checkout's logs/quality.jsonl, even when ~/.agents links to a kit worktree: rows kept in a worktree are
    lost when it is removed. Under the suite (AGENTS_KIT_UNDER_TEST) they stay in the HOME it made."""
    kit = os.path.realpath(os.path.expanduser("~/.agents"))
    try:
        with open(os.path.join(kit, ".git")) as fh:
            linked = "/worktrees/" in fh.read()
    except OSError:  # a directory (the main checkout) or none (not a checkout, maybe inside a dotfiles repo)
        linked = False
    if not linked or os.environ.get("AGENTS_KIT_UNDER_TEST"):
        return os.path.join(kit, "logs", "quality.jsonl")
    # -C doesn't override a GIT_DIR the caller exported, which would name another repository.
    listed = run_git(["-C", kit, "worktree", "list", "--porcelain", "-z"],
                     env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")})
    main = listed.stdout.partition("\0")[0].removeprefix("worktree ") if listed.returncode == 0 else ""
    # git lists the git dir, not a checkout, for a repository made with --separate-git-dir.
    if main and os.path.exists(os.path.join(main, ".git")):
        return os.path.join(main, "logs", "quality.jsonl")
    print(f"quality log: couldn't find the kit's main checkout ({listed.stderr.strip() or main}), so this row goes to "
          f"{kit}, a worktree whose removal loses it", file=sys.stderr)
    return os.path.join(kit, "logs", "quality.jsonl")


def log_rename(repo, old, new):
    """Lens runs are logged under the branch's name at the time, and the monthly job joins them to the PR's branch."""
    try:
        path = quality_log()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "kind": "rename", "repo": repo,
                                 "name": old, "to": new}) + "\n")
    except OSError as error:  # a sandbox that can't write ~/.agents/logs must still get its spec path
        print(f"could not log the rename of {old} to {new}: {error}", file=sys.stderr)


def move_out_of_tmpdir(common, repo, key):
    """Move this branch's spec and reports, written while a sandbox kept .git read-only, into the shared git dir.
    macOS clears $TMPDIR, so a spec left there can vanish in the middle of a pull request."""
    shared = os.path.join(common, "agents")
    if not os.access(shared if os.path.isdir(shared) else common, os.W_OK):
        return  # still in the sandbox: path.sh keeps using the $TMPDIR copy, quietly, on every call
    for name in branch_file_names(repo, key):
        src, dst = os.path.join(TMP_SPECS, name), os.path.join(shared, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                os.makedirs(shared, exist_ok=True)
                shutil.move(src, dst)
                print(f"moved {name} out of $TMPDIR", file=sys.stderr)
            except OSError as error:
                print(f"could not move {src} to {dst}: {error}", file=sys.stderr)


def branch_exists(name):
    return run_git(["show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).returncode == 0


def stamp_path(kind):
    name = "self-review.stamp" if kind == "review" else f"{kind}.stamp"
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    branch = branch_key(git("branch", "--show-current")) or "detached"
    directory = os.path.join(common, "agents", "stamps", branch)
    if not os.path.isdir(directory):
        follow_branch_renames()
    return os.path.join(directory, name)


def legacy_stamp_path(kind):
    """Where stamps lived before they moved to the shared git dir; still read, never written."""
    name = "self-review.stamp" if kind == "review" else f"{kind}.stamp"
    return os.path.join(git("rev-parse", "--absolute-git-dir"), name)


def stamp_matches(kind, current):
    for path in (stamp_path(kind), legacy_stamp_path(kind)):
        try:
            with open(path) as fh:
                if fh.read().strip() == current:
                    return True
        except OSError:
            continue
    return False


def report_paths(*kinds):
    reports = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "reports")
    return [subprocess.run([reports, "path", kind], capture_output=True, text=True).stdout.strip() for kind in kinds]


def table_cells(line):
    """The cells of a Markdown table row (outer pipes optional, \\| is a pipe inside a cell), or None for a line that has no pipe."""
    if not re.search(r"(?<!\\)\|", line):
        return None
    return [c.strip() for c in re.split(r"(?<!\\)\|", re.sub(r"^\s*\||(?<!\\)\|\s*$", "", line))]


def evidence_files(evidence):
    """The names of the non-empty files in the evidence directory."""
    return [name for name in os.listdir(evidence) if os.path.isfile(os.path.join(evidence, name))
            and os.path.getsize(os.path.join(evidence, name)) > 0] if evidence and os.path.isdir(evidence) else []


def names_a_file(cell, files):
    return any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", cell) for name in files)


def has_reason(cell):
    return bool(cell.strip(" \t-\u2013\u2014*_`"))


def result_rows(lines, evidence):
    """The PASS, FAIL and NOT RUN rows of the tables in lines that have Result and Evidence columns, as (first cell,
    result, evidence cell, whether that cell names a non-empty file of the evidence directory, the criteria numbers
    its AC cell names (None in a table with no AC column), its Case cell, its Expected cell (None in a table with no
    Expected column))."""
    files = evidence_files(evidence)
    rows, columns = [], None
    for line in lines:
        cells = table_cells(line)
        if not cells:
            columns = None  # a table per block, each with its own header
        elif {"Result", "Evidence"} <= {c.strip("*_") for c in cells}:
            names = [c.strip("*_") for c in cells]
            columns = (names.index("Result"), names.index("Evidence"), names.index("AC") if "AC" in names else None,
                       next((i for i, n in enumerate(names) if n.startswith("Case")), None),
                       names.index("Expected") if "Expected" in names else None)
        elif columns and len(cells) > columns[0]:
            result = re.match(r"[*_]*(?:(PASS|FAIL|NOT RUN)\b)?", cells[columns[0]]).group(1)
            cell = cells[columns[1]] if len(cells) > columns[1] else ""
            ac, case, expected = (cells[i] if i is not None and len(cells) > i else "" for i in columns[2:])
            acs = None if columns[2] is None else [int(n) for n in re.findall(r"\bAC(\d+)\b", ac)]
            if result:
                rows.append((cells[0], result, cell, names_a_file(cell, files), acs, case,
                             None if columns[4] is None else expected))
    return rows


GUIDE_STEP = r"([MSP]\d+)[a-z]*\b"  # S: a step before the merge in a guide from before checks-guide; M3a: a sub-step of M3


def guide_steps(text):
    """{step: {"name", "owner": "agent" or "you", "result": "PASS", "FAIL" or None, "date": " (<date>)" or ""}} for each
    step of a checks guide, its result the latest one its results tables give, None while it has none to stand on."""
    steps, declared = {}, {}  # declared: the sub-steps given a heading of their own (### P2a, ### P2b)
    # A step ends at the next heading of any level; a results table only at the next `## `, past its own lines.
    for step, sub, name, body in re.findall(r"^### ([MSP]\d+)([a-z]*)\b\.?[ \t]*([^\n]*)\n?(.*?)(?=^##|\Z)", text, re.M | re.S):
        where = re.search(r"^[-*\s]*(?:\*\*)?Where:?(?:\*\*)?:?\s*the agent\b", body, re.M)
        steps[step] = {"name": name.strip(), "owner": "agent" if where else "you", "result": None, "date": ""}
        if sub:
            declared.setdefault(step, set()).add(step + sub)
    # Every results table counts, ship's `## Results after the deploy` too; a later NOT RUN asks for the step again.
    rows, dates = {}, {}  # the latest result of each row (P2 or its sub-step P2b), and each step's latest table
    for heading, table in re.findall(r"^## Results([^\n]*)\n?(.*?)(?=^## |\Z)", text, re.M | re.S):
        when = re.search(r"\(([^)]*)\)", heading)
        for first, result, *_ in result_rows(table.splitlines(), None):
            step = re.match(r"[*_]*(" + GUIDE_STEP + ")", first)
            if step:
                rows.setdefault(step.group(2), {})[step.group(1)] = result
                dates[step.group(2)] = f" ({when.group(1)})" if when else ""
    for step, found in rows.items():  # a step's sub-steps add up: any FAIL fails it, any NOT RUN or none leaves it to run
        if step not in found:
            found.update({sub: "NOT RUN" for sub in declared.get(step, ()) if sub not in found})
        result = next(r for r in ("FAIL", "NOT RUN", "PASS") if r in found.values())
        info = steps.setdefault(step, {"name": "", "owner": "you"})
        info["result"], info["date"] = (None, "") if result == "NOT RUN" else (result, dates[step])
    return steps


def evidence_tree(path):
    """The tree bin/evidence recorded the check's content as, from the `# in` line it writes first; None when that
    line has none: saved before it recorded trees, outside a repository, or after git failed."""
    with open(path, errors="replace") as fh:
        found = re.match(r"# in .*, exit \d+, commit .*, tree ([0-9a-f]{40,64})$", fh.readline().rstrip("\n"))
    return found and found.group(1)


ENVIRONMENT_RECORD = "environment.txt"
RECORD_ENVIRONMENT = "~/.agents/bin/evidence environment ~/.agents/bin/evidence-env"


def stale_evidence(lines, evidence):
    """(problems, warnings) for the output files behind PASS/FAIL rows, and the environment record that any of them rests
    on, whose check ran on other content than the working tree's: a problem when files other than tests changed since, a
    warning when only tests did, or when this repository doesn't have the tree to compare (a check run in a throwaway
    clone)."""
    outputs = [name for name in evidence_files(evidence) if name.endswith(".txt")]
    ran = [row for row in result_rows(lines, evidence) if row[1] in ("PASS", "FAIL")]
    judged = {name for _, _, cell, backed, *_ in ran if backed for name in outputs if names_a_file(cell, [name])}
    if ran and ENVIRONMENT_RECORD in outputs:
        judged.add(ENVIRONMENT_RECORD)
    trees = {}  # each tree the checks ran on, with the files that recorded it
    for name in sorted(judged):
        tree = evidence_tree(os.path.join(evidence, name))
        if tree:
            trees.setdefault(tree, []).append(name)
    current = working_tree() if trees else None
    problems, warnings = [], []
    for tree, names in trees.items():
        if tree == current:
            continue
        named = ", ".join(names)
        lookup = run_git(["cat-file", "-e", tree])
        if lookup.returncode > 1:
            problems.append(f"{named}: couldn't look up the content it ran on: {lookup.stderr.strip()}")
            continue
        if lookup.returncode == 1:
            warnings.append(f"{named} ran on a tree this repository doesn't have (pruned by `git gc`, or a throwaway "
                            "clone's), so it can't be compared with the working tree; the stamp doesn't know whether it "
                            "is out of date.")
            continue
        try:
            changed = [p for p in git_paths("diff-tree", "-r", "--no-renames", "--name-only", tree, current)
                       if not NOT_STAMPED.search(p)]
        except RuntimeError as error:
            problems.append(f"{named}: couldn't compare the content it ran on with the working tree: {error}")
            continue
        if not changed:
            continue
        code = [p for p in changed if not TEST_FILE.search(p)]
        listed = code or changed
        shown = ", ".join(listed[:10]) + (f" and {len(listed) - 10} more" if len(listed) > 10 else "")
        if code:
            checks = [n for n in names if n != ENVIRONMENT_RECORD]
            again = ([f"Run {'those checks' if len(checks) > 1 else 'that check'} again through bin/evidence and update "
                      "their rows."] if checks else []) + ([f"Record the environment again: {RECORD_ENVIRONMENT}"]
                                                           if ENVIRONMENT_RECORD in names else [])
            problems.append(f"{named} ran on content the working tree no longer has: {shown} changed since, and aren't "
                            "tests. " + " ".join(again))
        else:
            warnings.append(f"{named} ran before changes to tests only ({shown}): run it again if those tests bear on "
                            "its check.")
    return problems, warnings


def unbacked_rows(lines, evidence):
    """The PASS/FAIL rows whose Evidence cell names no saved file, as "<first cell>: <evidence cell>"."""
    return [f"{first}: {cell or '(empty)'}" for first, result, cell, backed, *_ in result_rows(lines, evidence)
            if result in ("PASS", "FAIL") and not backed]


def rows_without_expected(lines, evidence):
    """The PASS/FAIL rows of the validation report whose table has no Expected column or whose Expected cell holds nothing,
    as "<first cell>: <why>". A checks guide's results tables have no such column: only the validation report is asked."""
    return [f"{first}: " + ("its table has no Expected column" if expected is None else "its Expected cell is blank")
            for first, result, *_, expected in result_rows(lines, evidence)
            if result in ("PASS", "FAIL") and (expected is None or not has_reason(expected))]


def environment_notes(lines, evidence):
    """(problems, warnings) for the environment record behind the PASS/FAIL rows: a problem when it is missing or empty, a
    warning naming the probes it ends by calling incomplete. A service being down can be what the change is about, so an
    incomplete record isn't refused."""
    if not any(row[1] in ("PASS", "FAIL") for row in result_rows(lines, evidence)):
        return [], []
    if ENVIRONMENT_RECORD not in evidence_files(evidence):
        return [f"{evidence} has no {ENVIRONMENT_RECORD}, or it is empty: the checks ran in an environment nobody wrote down. "
                f"Record it, once the stack answers: {RECORD_ENVIRONMENT}"], []
    with open(os.path.join(evidence, ENVIRONMENT_RECORD), errors="replace") as fh:
        last = next((l for l in reversed(fh.read().splitlines()) if l.strip()), "")
    if last.startswith("incomplete: "):
        return [], [f"{ENVIRONMENT_RECORD} is incomplete ({last[len('incomplete: '):]}): the stamp doesn't refuse it, but the "
                    "report should say what that probe's failure means for the checks."]
    return [], []


def empty_recordings(evidence):
    """The browser-check videos of the evidence directory that hold nothing: agent-browser leaves a 0-byte file when its
    browser closes before `record stop`."""
    if not evidence or not os.path.isdir(evidence):
        return []
    return sorted(name for name in os.listdir(evidence)
                  if name.endswith(".webm") and os.path.getsize(os.path.join(evidence, name)) == 0)


def spec_criteria():
    """(spec path, its numbered criteria, None), (path, None, None) when path.sh names no file, or (None, None, why the
    spec can't be read)."""
    kit_spec = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "kit-spec")
    try:
        found = subprocess.run([os.path.join(kit_spec, "path.sh")], capture_output=True, text=True, timeout=SPEC_LOOKUP_SECS)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:  # ValueError: output that isn't UTF-8
        return None, None, f"The spec lookup failed: {error}."
    path = found.stdout.strip()
    if found.returncode or not path:
        why = (found.stderr.strip().splitlines() or [f"exit {found.returncode}, no path printed"])[-1]
        return None, None, f"The spec lookup (skills/kit-spec/path.sh) failed: {why}."
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        return path, None, None
    except OSError as error:
        return None, None, f"Can't read the spec {path}: {error.strerror}."
    except ValueError as error:
        return None, None, f"Can't read the spec {path}: not UTF-8 ({error.reason} at byte {error.start})."
    sys.dont_write_bytecode = True  # as in bin/triage: no __pycache__ left in the skill
    loader = importlib.machinery.SourceFileLoader("spec_lint", os.path.join(kit_spec, "lint.py"))
    lint = importlib.util.module_from_spec(importlib.util.spec_from_loader("spec_lint", loader))
    loader.exec_module(lint)
    return path, lint.criteria(text), None


def criteria_problems(items, rows):
    """Why the results rows don't cover the spec's criteria: each needs a PASS row and a negative row that ran, both naming
    it in the AC column, or, when its `verify:` can't be automated, a NOT RUN row with a reason."""
    numbers = [int(re.match(r"\d+", item).group()) for item in items]
    if not numbers:
        return ["The spec has no numbered acceptance criteria, so the rows have none to cover: number them `1.`, `2.`... under "
                "`## Acceptance criteria` (kit-spec skill)."]
    repeated = sorted({n for n in numbers if numbers.count(n) > 1})
    if repeated:
        return [f"The spec numbers criterion {', '.join(map(str, repeated))} more than once, so `AC{repeated[0]}` is ambiguous: renumber its criteria."]
    problems = [f"{row[0]} names AC{n}, which is no criterion of the spec (it has {', '.join(f'AC{k}' for k in numbers)})."
                for row in rows for n in row[4] or [] if n not in numbers]
    for number, item in zip(numbers, items):
        mine = [row for row in rows if number in (row[4] or [])]
        # The last `verify:`, as kit-spec ends a criterion with it, and none inside a code span (`verify: x` quoted);
        # the text after it keeps its code spans: `pytest x` by hand is a command, not a manual check.
        quoted = re.sub(r"`[^`]*`", lambda span: "\0" * len(span.group()), item)  # same length, and no \s for verify: to skip
        verify = [item[found.end():] for found in re.finditer(r"(?<!\w)verify:\s*", quoted, re.I)][-1:]
        if verify and MANUAL_VERIFY.match(verify[0]) and any(r[1] == "NOT RUN" and has_reason(r[2]) for r in mine):
            continue
        manual = " (or, as its check is manual, a NOT RUN row naming it with the reason in Evidence)" if verify and MANUAL_VERIFY.match(verify[0]) else ""
        if not any(r[1] == "PASS" for r in mine):
            problems.append(f"AC{number} has no PASS row: add a row with AC{number} in its AC cell and Result PASS{manual}.")
        if not any(r[1] in ("PASS", "FAIL") and NEGATIVE_CASE.search(r[5]) for r in mine):
            problems.append(f"AC{number} has no negative (\u2212) row that ran: add a row with AC{number} in its AC cell, a Case with \u2212 "
                            f"(or \u00b1) and Result PASS or FAIL{manual}.")
    unnamed = [row[0] for row in rows if row[4] is None]
    if problems and unnamed:
        problems.append(f"These rows are in a table with no AC column, so they count toward no criterion: add an `AC` column "
                        f"(`# | AC | Check | Case (+/\u2212) | Expected | Result | Evidence`): {', '.join(unnamed[:8])}.")
    return problems


def selected_test_types():
    """([test type labels bin/triage selects for the change], None), or (None, why triage can't say)."""
    triage = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bin", "triage")
    try:
        done = subprocess.run([triage, "--json"], capture_output=True, text=True, timeout=TRIAGE_SECS)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return None, f"bin/triage --json didn't finish: {error}."
    if done.returncode:
        return None, f"bin/triage --json failed (exit {done.returncode}): {' '.join(done.stderr.split()) or 'no message'}"
    try:
        tests = json.loads(done.stdout)["tests"]
    except (ValueError, KeyError, TypeError):
        tests = None
    if not isinstance(tests, dict):
        return None, "bin/triage --json printed no `tests` object."
    return list(tests), None


def test_type_problems(text, evidence):
    """Why the report's `## Test types` table (`| Type | Ran | Evidence |`) leaves out a type triage selects: each needs a
    row whose Type holds the first words of its label, run with a saved file as evidence or not run with a reason."""
    selected, why = selected_test_types()
    if why:
        return [why]
    section = re.search(r"^## Test types[ \t]*$(.*?)(?=^## |\Z)", text, re.M | re.S | re.I)
    files, rows, columns, table = evidence_files(evidence), [], None, False
    for line in (section.group(1) if section else "").splitlines():
        cells = table_cells(line)
        if not cells:
            columns = None
        elif {"Type", "Ran", "Evidence"} <= {c.strip("*_") for c in cells}:
            columns, table = [[c.strip("*_") for c in cells].index(name) for name in ("Type", "Ran", "Evidence")], True
        elif columns and len(cells) > max(columns):
            kind, ran, cell = (cells[i] for i in columns)
            ran = re.sub(r"[\s*_`]", "", ran).lower()
            why = (None if (ran == "yes" and names_a_file(cell, files)) or (ran == "no" and has_reason(cell))
                   else "Ran yes but an Evidence naming no saved file" if ran == "yes"
                   else "Ran no but no reason in Evidence" if ran == "no" else "a Ran that is neither yes nor no")
            rows.append((re.findall(r"[a-z0-9]+", kind.lower()), kind, why))
    if not table:
        return [f"The report has no `## Test types` table (`| Type | Ran | Evidence |`): bin/triage selects {'; '.join(selected)} "
                "for this change; add a row `| <type> | yes | <link to a saved file> |` or `| <type> | no | <why not> |` for each."
                ] if selected else []
    problems = []
    for label in selected:
        mine = [(kind, why) for words, kind, why in rows if words and re.findall(r"[a-z0-9]+", label.lower())[:len(words)] == words]
        if not any(why is None for _, why in mine):
            problems.append(f"The `## Test types` table doesn't account for {label}, which bin/triage selects for this change: "
                            + ("; ".join(f"its row `{kind}` has {why}" for kind, why in mine) if mine else
                               "add a row `| <type> | yes | <link to a saved file> |` or `| <type> | no | <why not> |`") + ".")
    return problems


def suite_command(command):
    """A full-suite command's arguments as the suite overlay and bin/evidence's `$ ` line compare: shell quoting aside,
    `./x`, `bash x` and `x` alike. None for a line that isn't one plain command."""
    try:
        words = shlex.split(command)
        # Unquoted operators only: posix=False keeps a quoted `'|'` in its quotes, so it stays an argument.
        raw = list(shlex.shlex(command, posix=False, punctuation_chars=";&|<>"))
    except ValueError:  # an unclosed quote
        return None
    words = words[1:] if words[:1] == ["bash"] else words
    if not words or "=" in words[0] or any(set(token) <= set(";&|<>") for token in raw):
        return None
    return (words[0][len("./"):] if words[0].startswith("./") else words[0], *words[1:])


def evidence_header(path):
    """(command, directory, exit code, fingerprint or None) from the header bin/evidence saves, or None for any other file."""
    try:
        with open(path, "rb") as fh:
            head = [fh.readline(EVIDENCE_LINE_BYTES).decode(errors="replace").rstrip("\n") for _ in range(3)]
    except OSError:
        return None
    # `# in`, then `# fingerprint` when there is one, then `$ `; before the commit and tree, `$ ` came first.
    if head[0].startswith("$ "):
        command, ran, recorded = head[0], head[1], head[2]
    else:
        ran, recorded, command = head[0], head[1], head[2] if head[1].startswith("# fingerprint ") else head[1]
    ran = re.fullmatch(r"# in (.+), \d{4}-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}, exit (\d{1,3})"
                       r"(?:, commit none|, commit [0-9a-f]+(?:, uncommitted changes)?, tree \S+)?", ran)
    if not command.startswith("$ ") or not ran:
        return None
    recorded = re.fullmatch(r"# fingerprint ([0-9a-f]{64})", recorded)
    return command[2:], ran.group(1), int(ran.group(2)), recorded.group(1) if recorded else None


def suite_problems(evidence):
    """Why no file of the evidence directory shows a green run, from a checkout's root, of a command the repo's suite
    overlay lists on the code as it is now (the fingerprint verify's stamp holds, which bin/evidence records before the
    run). Empty without an overlay."""
    shown = f"~/.agents/repos/{repo_name()}/suite"
    overlay = os.path.expanduser(shown)
    try:
        with open(overlay, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except FileNotFoundError:
        if not os.path.islink(overlay):
            return []
        return [f"The suite overlay {shown} is a link to {os.readlink(overlay)}, which doesn't exist: restore it or remove "
                "the link."]
    except (OSError, ValueError) as error:  # ValueError: not UTF-8
        return [f"Can't read the suite overlay {shown}: {getattr(error, 'strerror', None) or error}."]
    commands = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not commands:
        return [f"The suite overlay {shown} holds no command: list the repo's full-suite commands in it, one per line, "
                "or remove it."]
    unusable = [c for c in commands if suite_command(c) is None]
    if unusable:
        return [f"The suite overlay {shown} has lines that aren't one plain command (no pipes, `&&`, `;`, redirects or "
                "`VAR=` prefixes; a script can hold those):"] + [f"  {c}" for c in unusable]
    names = sorted(os.listdir(evidence))
    try:
        current = fingerprint()
    except RuntimeError as error:
        return [f"Can't compute the code's fingerprint to match the suite runs against: {error}"]
    wanted, reasons = {suite_command(c): c for c in commands}, []
    for name in names:
        header = evidence_header(os.path.join(evidence, name))
        if not header:
            continue
        ran, where, status, recorded = header
        words = suite_command(ran)
        if words not in wanted:
            line = next((c for w, c in wanted.items() if words and words[:len(w)] == w), None)
            if line:
                reasons.append(f"{name} ran `{ran}`, which doesn't count as the suite line `{line}`: run the line as it is.")
            continue
        if status != 0:
            reasons.append(f"{name} ran `{ran}`, but it exited {status}: fix what failed and run it again.")
        elif not os.path.exists(os.path.join(where, ".git")):
            reasons.append(f"{name} ran `{ran}` in {where}, which isn't a checkout's root: run it from the repo root.")
        elif not recorded:
            reasons.append(f"{name} ran `{ran}` with no recorded fingerprint (a bin/evidence from before it recorded one, "
                           "or run outside a checkout): run it again.")
        elif recorded != current:
            reasons.append(f"{name} ran `{ran}` on different code: a file changed during or after the run (an untracked "
                           "file the suite left counts), or the branch moved onto a newer base. Run it again.")
        else:
            return []
    return ([f"The suite overlay {shown} asks for a green run of the repo's full suite on the code as it is now, and "
             f"{evidence} holds none. Run one of its commands from the repo root:"]
            + [f"  ~/.agents/bin/evidence suite {c}" for c in commands] + reasons)


def validation_report_problems():
    """Why the validation report can't back a validate stamp: it's missing, PASS/FAIL rows whose Evidence cell names no
    non-empty file of the evidence directory, an empty recording, output recorded on content that changed since in
    files other than tests (stale_evidence), a criterion of the spec without its PASS and negative rows, a test type
    triage selects that its Test types table leaves out, or, where the repo has a suite overlay, no green run of the
    full suite on the code as it is now. Empty when it can."""
    report, evidence = report_paths("validation", "evidence")
    try:
        with open(report) as fh:
            text = fh.read()
    except OSError:
        return [f"No validation report at {report}: save it first (kit-validate skill, step 5)."]
    if not evidence:  # reports path evidence refuses on a detached HEAD
        return ["No evidence directory: a detached HEAD has none. Validate from the branch's checkout."]
    try:
        os.listdir(evidence)
    except OSError as error:  # every check below lists it
        return [f"Can't list the evidence directory {evidence}: {error.strerror}."]
    lines, problems = text.splitlines(), []
    missing = unbacked_rows(lines, evidence)
    if missing:
        problems += [f"These PASS/FAIL rows name no saved evidence in {evidence} (`bin/evidence <id> <command>` for "
                     "output, a screenshot for a UI state):"] + missing
    unexpected = rows_without_expected(lines, evidence)
    if unexpected:
        problems += ["These PASS/FAIL rows say nothing of the result expected. Add an Expected column "
                     "(`# | AC | Check | Case (+/\u2212) | Expected | Result | Evidence`) holding the output, status, UI text "
                     "or count the check asserts on:"] + unexpected
    stale, warnings = stale_evidence(lines, evidence)
    problems += stale
    missing_environment, incomplete = environment_notes(lines, evidence)
    problems += missing_environment
    for warning in warnings + incomplete:
        print(warning)
    empty = empty_recordings(evidence)
    if empty:
        problems += [f"These recordings in {evidence} are empty: the browser closed before `agent-browser record stop` "
                     "saved them. Record those checks again, stopping the recording before the next check or `close`:"] + empty
    path, items, why = spec_criteria()
    if why:
        problems.append(why)
    elif items is not None:
        problems += criteria_problems(items, result_rows(lines, evidence))
    problems += test_type_problems(text, evidence)
    # Last, after triage's run: the closer to the stamp's own fingerprint, the less an edit can slip between them.
    problems += suite_problems(evidence)
    if items is None and not why and not problems:
        print(f"No spec at {path}: the per-criterion check was skipped.")
    return problems


def record_verify(kind, checked_fingerprint):
    """Leave only this verify run's stamp: kind on the fingerprint it checked, or none when kind is None (it failed).
    Raises OSError when a stamp can't be written or removed, after trying every removal."""
    errors = []
    for other in VERIFY_KINDS:
        for path in (stamp_path(other), legacy_stamp_path(other)):
            if other != kind or path != stamp_path(kind):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError as error:
                    errors.append(error)
    if errors:
        raise errors[0]
    if kind:
        os.makedirs(os.path.dirname(stamp_path(kind)), exist_ok=True)
        with open(stamp_path(kind), "w") as fh:
            fh.write(checked_fingerprint)


def option(args, name):
    """The value of `--name value` or `--name=value`: None when the option is absent, "" when it has no value."""
    for at, arg in enumerate(args):
        if arg == name:
            return args[at + 1] if at + 1 < len(args) else ""
        if arg.startswith(name + "="):
            return arg[len(name) + 1:]
    return None


def main():
    args = sys.argv[1:]
    kind = option(args, "--kind")
    if kind == "":
        print(f"--kind needs a value: one of {', '.join(STAMP_KINDS)}.", file=sys.stderr)
        return 2
    kind = kind or "review"
    command = args[0] if args else "check"
    if command not in COMMANDS or kind not in STAMP_KINDS:
        print(f"Unknown {'command' if command not in COMMANDS else 'stamp kind'} "
              f"`{command if command not in COMMANDS else kind}`: the commands are {', '.join(COMMANDS)}, "
              f"and the kinds {', '.join(STAMP_KINDS)}.", file=sys.stderr)
        return 2
    if command == "write" and kind in VERIFY_KINDS:
        print("A verify stamp comes only from running verify: run `python3 ~/.agents/hooks/stop_checks.py verify`, "
              "which stamps the change when it passes.", file=sys.stderr)
        return 2
    git_dir = run_git(["rev-parse", "--absolute-git-dir"], env={**os.environ, "LC_ALL": "C"})  # the check reads English
    if git_dir.returncode != 0:
        if "not a git repository" in git_dir.stderr:
            return 0
        raise RuntimeError(f"git rev-parse failed: {' '.join(git_dir.stderr.split())}")
    os.chdir(git("rev-parse", "--show-toplevel"))
    if command == "branch-key":
        print(branch_key(git("branch", "--show-current")))
        return 0
    if command == "base":
        base = branch_base()[0]
        target = option(args, "--matches")
        if target is None:
            print(base)
            return 0
        # Without --base, gh targets the branch's gh-merge-base, else the repository's default branch.
        branch = git("branch", "--show-current")
        target = target or git("config", "--get", f"branch.{branch}.gh-merge-base") or branch_name(base_ref())
        if target in (base, branch_name(base)):
            return 0
        print(f"This PR targets `{target}`, but its stamps measure the change against `{base}`, so the review would "
              f"cover a different change than the PR shows. If `{target}` is the right target, record it with "
              f"`git config branch.{branch}.agentsBase {target}`, then re-run the "
              f"kit-review and kit-validate skills on the change it shows; otherwise pass `--base {branch_name(base)}`.")
        return 1
    if command == "follow-renames":
        follow_branch_renames()
        return 0
    if command == "needs-validate":
        return 0 if any(not NOT_BEHAVIOR.search(p) for p in changed_paths()[1]) else 1
    if command == "fingerprint":
        print(fingerprint())
        return 0
    problems = validation_report_problems() if command == "write" and kind == "validate" else []
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 2
    if command == "write":
        os.makedirs(os.path.dirname(stamp_path(kind)), exist_ok=True)
        with open(stamp_path(kind), "w") as fh:
            fh.write(fingerprint_for(kind))
        print(f"{kind} stamp written")
        return 0
    return 0 if stamp_matches(kind, fingerprint_for(kind)) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as error:  # a git query the stamp needs failed: no stamp can be written or matched
        print(f"review_stamp: {error}", file=sys.stderr)
        sys.exit(2)
