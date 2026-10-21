#!/usr/bin/env python3
"""Record or check that a skill ran on the exact current change.

  review_stamp.py write [--kind review|validate]   # end of review / validate; verify stamps come only from
                                                   # its runner (stop_checks.py), through record_verify(); validate
                                                   # needs a report whose PASS/FAIL rows name their evidence
  review_stamp.py check [--kind review|validate|verify|verify-empty]   # exit 1 if the change differs from the stamped one
  review_stamp.py needs-validate                   # exit 0 if the change touches behavior, not just tests/docs
  review_stamp.py follow-renames                   # move spec, reports and stamps from this branch's earlier names, and out of $TMPDIR
  review_stamp.py branch-key                       # the current branch as it appears in those file names
  review_stamp.py base [--matches <branch>]        # the ref the change is measured against (see branch_base), or
                                                   # exit 1, saying why, when a PR targeting <branch> (empty: the
                                                   # default branch) would show a different change

A stamp covers every file that differs from the merge-base with the branch's base (the default branch, or the
parent of a stacked branch: branch_base), except the changelog, so committing stamped changes keeps it valid
and any later edit invalidates it. Review and validate stamps hold the branch's diff, so merging the default branch
keeps them unless it moves the changed lines or their context; verify's holds the files' content, so the tests run again. Stamps live in the repository's shared git dir, per branch
(.git/agents/stamps/<branch key>/), never in the tree: they survive removing the worktree they were
written in, so they are still read from the main checkout once kit-ship frees the branch. They also follow `git branch -m`: agents often write the spec on a session branch and rename it afterwards.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

# Where path.sh keeps a spec and its reports while a sandbox makes .git read-only.
TMP_SPECS = os.path.join(os.environ.get("TMPDIR") or "/tmp", "agents-specs")
NOT_BEHAVIOR = re.compile(
    r"(^|/)(tests?|__tests__|spec|docs?)/|[._-](test|spec)\.[a-z]+$|Test\.php$|\.(md|txt|rst)$|(^|/)(CHANGELOG|README)",
    re.I,
)
# CI checks the changelog entry, and it is often written last: adding it re-runs nothing.
NOT_STAMPED = re.compile(r"(^|/)CHANGELOG(\.(md|txt|rst))?\Z|(^|/)changelog\.d/[^/]+\.md\Z", re.I)
VERIFY_KINDS = ("verify", "verify-empty")
STAMP_KINDS = ("review", "validate", *VERIFY_KINDS)
COMMANDS = ("check", "write", "base", "branch-key", "follow-renames", "needs-validate")


def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd).stdout.strip()


def git_checked(*args, env=None):
    """git's output as bytes; a failure raises, since an empty answer would make every change look stamped."""
    result = subprocess.run(["git", *args], capture_output=True, env=env)
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
    """Where this branch left its base (branch_base); HEAD when on the default branch itself."""
    return git("merge-base", "HEAD", branch_base(cwd)[0], cwd=cwd) or "HEAD"


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
    fd, index = tempfile.mkstemp(prefix="agents-stamp-index-")
    os.close(fd)
    try:
        real_index = os.fsdecode(git_checked("rev-parse", "--path-format=absolute", "--git-path", "index").rstrip(b"\n"))
        if os.path.exists(real_index):
            shutil.copyfile(real_index, index)
        else:
            os.remove(index)
        env = {**os.environ, "GIT_INDEX_FILE": index}
        untracked = git_paths("ls-files", "--others", "--exclude-standard", "--", *stamped)
        commands = [["add", "--intent-to-add", "--", *(f":(literal){p}" for p in untracked)]] if untracked else []
        commands.append(["diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "--binary", "-U3", base,
                         "--", *stamped])
        for args in commands:
            # Bytes: decoding could make two different changes hash alike.
            patch = git_checked(*args, env=env)
    finally:
        if os.path.exists(index):
            os.remove(index)
    for line in patch.split(b"\n"):
        if not line.startswith(b"index "):
            digest.update(re.sub(rb"^@@ -\d+(,\d+)? \+\d+(,\d+)? @@", b"@@", line) + b"\n")
    return digest.hexdigest()


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
    listed = subprocess.run(["git", "-C", kit, "worktree", "list", "--porcelain", "-z"], capture_output=True, text=True,
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
    return subprocess.run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{name}"]).returncode == 0


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


def result_rows(lines, evidence):
    """The PASS, FAIL and NOT RUN rows of the tables in lines that have Result and Evidence columns, as (first cell,
    result, evidence cell, whether that cell names a non-empty file of the evidence directory)."""
    files = [name for name in os.listdir(evidence) if os.path.isfile(os.path.join(evidence, name))
             and os.path.getsize(os.path.join(evidence, name)) > 0] if evidence and os.path.isdir(evidence) else []
    rows, columns = [], None
    for line in lines:
        # Markdown tables: outer pipes optional, \| is a pipe inside a cell.
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", re.sub(r"^\s*\||(?<!\\)\|\s*$", "", line))] if re.search(r"(?<!\\)\|", line) else None
        if not cells:
            columns = None  # a table per block, each with its own header
        elif {"Result", "Evidence"} <= {c.strip("*_") for c in cells}:
            names = [c.strip("*_") for c in cells]
            columns = (names.index("Result"), names.index("Evidence"))
        elif columns and len(cells) > columns[0]:
            result = re.match(r"[*_]*(?:(PASS|FAIL|NOT RUN)\b)?", cells[columns[0]]).group(1)
            cell = cells[columns[1]] if len(cells) > columns[1] else ""
            backed = any(re.search(rf"(?<![\w.-]){re.escape(name)}(?![\w.-])", cell) for name in files)
            if result:
                rows.append((cells[0], result, cell, backed))
    return rows


def unbacked_rows(lines, evidence):
    """The PASS/FAIL rows whose Evidence cell names no saved file, as "<first cell>: <evidence cell>"."""
    return [f"{first}: {cell or '(empty)'}" for first, result, cell, backed in result_rows(lines, evidence)
            if result in ("PASS", "FAIL") and not backed]


def rows_without_evidence():
    """Why the validation report can't back a validate stamp: it's missing, or PASS/FAIL rows whose Evidence cell names
    no non-empty file of the evidence directory. Empty when it can."""
    report, evidence = report_paths("validation", "evidence")
    try:
        with open(report) as fh:
            lines = fh.read().splitlines()
    except OSError:
        return [f"No validation report at {report}: save it first (kit-validate skill, step 5)."]
    missing = unbacked_rows(lines, evidence)
    if missing:
        return [f"These PASS/FAIL rows name no saved evidence in {evidence} (`bin/evidence <id> <command>` for output, "
                "a screenshot for a UI state):"] + missing
    return []


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


def main():
    args = sys.argv[1:]
    kind = args[args.index("--kind") + 1] if "--kind" in args else "review"
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
    if not git("rev-parse", "--absolute-git-dir"):
        return 0
    os.chdir(git("rev-parse", "--show-toplevel"))
    if command == "branch-key":
        print(branch_key(git("branch", "--show-current")))
        return 0
    if command == "base":
        base = branch_base()[0]
        if "--matches" not in args:
            print(base)
            return 0
        target = args[args.index("--matches") + 1] if len(args) > args.index("--matches") + 1 else ""
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
    unbacked = rows_without_evidence() if command == "write" and kind == "validate" else []
    if unbacked:
        print("\n".join(unbacked), file=sys.stderr)
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
