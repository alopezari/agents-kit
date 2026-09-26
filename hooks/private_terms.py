#!/usr/bin/env python3
"""Keep what profiles consider private (an employer's ticket prefixes, product names, hosts) out of the kit.

Profiles list case-insensitive regexes in profiles/<name>/private-terms.txt, one per line. The core never
names them: it only checks the kit's own commits and pull requests against every profile's list.

  private_terms.py staged           the lines a commit adds (pre-commit)
  private_terms.py message <file>   a commit message (commit-msg)

Exits 1 and names the matches when any are found. guard_bash.py uses found() for `gh pr create|edit`.
"""
import glob
import os
import re
import subprocess
import sys

PROFILES = os.environ.get("AGENTS_PROFILES_DIR") or os.path.expanduser("~/.agents/profiles")


def patterns():
    found = []
    for path in sorted(glob.glob(os.path.join(PROFILES, "*", "private-terms.txt"))):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#"):
                found.append(re.compile(line, re.I))
    return found


def found(text):
    """The private terms in text, as they appear there."""
    return sorted({m.group(0) for p in patterns() for m in p.finditer(text)})


def staged_additions():
    diff = subprocess.run(["git", "diff", "--cached", "--unified=0", "--no-color"],
                          capture_output=True, text=True, check=True).stdout
    return "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))


def main(args):
    if args == ["staged"]:
        where, text = "the staged changes", staged_additions()
    elif len(args) == 2 and args[0] == "message":
        where, text = "the commit message", open(args[1]).read()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    terms = found(text)
    if terms:
        print(f"Private terms from a profile in {where}: {', '.join(terms)}. The kit is public; keep them in the profile.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
