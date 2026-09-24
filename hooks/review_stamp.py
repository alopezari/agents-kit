#!/usr/bin/env python3
"""Record or check that a skill ran on the exact current change.

  review_stamp.py write [--kind review|validate|verify]   # end of self-review / validate; the stop hook after a green verify
  review_stamp.py check [--kind review|validate|verify]   # exit 1 if the change differs from the stamped one
  review_stamp.py needs-validate                   # exit 0 if the change touches behavior, not just tests/docs

The fingerprint covers every file that differs from the merge-base with the
default branch, by content, so committing stamped changes keeps it valid and
any later edit invalidates it. Stamps live in .git/, never in the tree.
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


def stamp_path(kind):
    name = "self-review.stamp" if kind == "review" else f"{kind}.stamp"
    return os.path.join(git("rev-parse", "--absolute-git-dir"), name)


def main():
    if not git("rev-parse", "--absolute-git-dir"):
        return 0
    os.chdir(git("rev-parse", "--show-toplevel"))
    args = sys.argv[1:]
    kind = args[args.index("--kind") + 1] if "--kind" in args else "review"
    command = args[0] if args else "check"
    if command == "needs-validate":
        return 0 if any(not NOT_BEHAVIOR.search(p) for p in changed_paths()[1]) else 1
    if command == "write":
        with open(stamp_path(kind), "w") as fh:
            fh.write(fingerprint())
        print(f"{kind} stamp written")
        return 0
    try:
        with open(stamp_path(kind)) as fh:
            return 0 if fh.read().strip() == fingerprint() else 1
    except OSError:
        return 1


if __name__ == "__main__":
    sys.exit(main())
