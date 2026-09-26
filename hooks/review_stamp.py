#!/usr/bin/env python3
"""Record or check that a skill ran on the exact current change.

  review_stamp.py write [--kind review|validate|verify]   # end of self-review / validate; the stop hook after a green verify
  review_stamp.py check [--kind review|validate|verify]   # exit 1 if the change differs from the stamped one
  review_stamp.py needs-validate                   # exit 0 if the change touches behavior, not just tests/docs
  review_stamp.py follow-renames                   # move spec, reports and stamps from this branch's earlier names
  review_stamp.py branch-key                       # the current branch as it appears in those file names

The fingerprint covers every file that differs from the merge-base with the
default branch, by content, so committing stamped changes keeps it valid and
any later edit invalidates it. Stamps live in the repository's shared git dir, per branch
(.git/agents/stamps/<branch key>/), never in the tree: they survive removing the worktree they were
written in, so the PR can be opened from the main checkout after a staging hand-off. They also
follow `git branch -m`: agents often write the spec on a session branch and rename it afterwards.
"""
import hashlib
import os
import re
import subprocess
import sys

NOT_BEHAVIOR = re.compile(
    r"(^|/)(tests?|__tests__|spec|docs?)/|[._-](test|spec)\.[a-z]+$|Test\.php$|\.(md|txt|rst)$|(^|/)(CHANGELOG|README)",
    re.I,
)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd).stdout.strip()


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
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd)
    name = os.path.basename(common)
    return os.path.basename(os.path.dirname(common)) if name == ".git" else name[:-len(".git")] if name.endswith(".git") else name


def merge_base(cwd=None):
    """Where this branch left the default branch; HEAD when on the default branch itself."""
    return git("merge-base", "HEAD", base_ref(cwd), cwd=cwd) or "HEAD"


def changed_paths():
    merge_base_sha = merge_base()
    paths = set(git("diff", "--name-only", merge_base_sha).splitlines())
    paths |= set(git("ls-files", "--others", "--exclude-standard").splitlines())
    return merge_base_sha, sorted(p for p in paths if p)


def fingerprint():
    base, paths = changed_paths()
    digest = hashlib.sha256(base.encode())
    for path in paths:
        digest.update(path.encode())
        digest.update(git("hash-object", path).encode() if os.path.isfile(path) else b"<deleted>")
    return digest.hexdigest()


# Files skills/spec/path.sh and bin/reports name <kind>-<repo>-<branch key>.md, next to the spec.
REPORT_KINDS = ("spec", "verify", "review", "validation", "staging-guide", "follow-pr")


def renamed_from(branch):
    """Earlier names of this branch, newest first: git carries a branch's reflog across renames."""
    entries = git("reflog", "show", "--format=%gs", f"refs/heads/{branch}")
    return re.findall(r"^Branch: renamed refs/heads/(.+) to refs/heads/", entries, re.M)


def branch_key(branch):
    """The branch in file names. `~` can't appear in a branch name, so `a/b` and `a-b` never share files."""
    return branch.replace("/", "~")


def follow_branch_renames():
    """Move the spec, reports and stamps kept under an earlier name or key of this branch to its current key."""
    branch = git("branch", "--show-current")
    if not branch:
        return
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    repo, new = repo_name(), branch_key(branch)
    dirs = (os.path.join(common, "agents"), os.path.join(os.environ.get("TMPDIR", "/tmp"), "agents-specs"))
    earlier = renamed_from(branch)
    # Keys used to turn / into -: such a key is this branch's, unless a branch with that name really exists.
    dashed = [name.replace("/", "-") for name in [branch, *earlier] if "/" in name]
    for old in [branch_key(name) for name in earlier] + dashed:
        moves = [(os.path.join(d, f"{kind}-{repo}-{old}.md"), os.path.join(d, f"{kind}-{repo}-{new}.md"))
                 for d in dirs for kind in REPORT_KINDS]
        moves += [(os.path.join(d, f"browser-ab-spec-{repo}-{old}.json"), os.path.join(d, f"browser-ab-spec-{repo}-{new}.json"))
                  for d in dirs]
        moves.append((os.path.join(common, "agents", "stamps", old), os.path.join(common, "agents", "stamps", new)))
        for src, dst in moves:
            if os.path.exists(src) and not os.path.exists(dst) and not (old in dashed and branch_exists(old)):
                os.rename(src, dst)
                print(f"moved {os.path.basename(src)} to {os.path.basename(dst)}", file=sys.stderr)


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


def main():
    if not git("rev-parse", "--absolute-git-dir"):
        return 0
    os.chdir(git("rev-parse", "--show-toplevel"))
    args = sys.argv[1:]
    kind = args[args.index("--kind") + 1] if "--kind" in args else "review"
    command = args[0] if args else "check"
    if command == "branch-key":
        print(branch_key(git("branch", "--show-current")))
        return 0
    if command == "follow-renames":
        follow_branch_renames()
        return 0
    if command == "needs-validate":
        return 0 if any(not NOT_BEHAVIOR.search(p) for p in changed_paths()[1]) else 1
    if command == "write":
        os.makedirs(os.path.dirname(stamp_path(kind)), exist_ok=True)
        with open(stamp_path(kind), "w") as fh:
            fh.write(fingerprint())
        print(f"{kind} stamp written")
        return 0
    current = fingerprint()
    for path in (stamp_path(kind), legacy_stamp_path(kind)):
        try:
            with open(path) as fh:
                if fh.read().strip() == current:
                    return 0
        except OSError:
            continue
    return 1


if __name__ == "__main__":
    sys.exit(main())
