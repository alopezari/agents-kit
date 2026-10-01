#!/usr/bin/env python3
"""Keep what profiles consider private (an employer's ticket prefixes, product names, hosts) out of the kit.

Profiles list case-insensitive regexes in profiles/<name>/private-terms.txt, one per line. The core never
names them: it only checks the kit's own commits and pull requests against every profile's list.

  private_terms.py staged           the lines a commit adds (pre-commit)
  private_terms.py message <file>   a commit message (commit-msg)
  private_terms.py pr <title> <body file>
                                    a pull request's title and description, before `gh pr create|edit` on the kit:
                                    names each term with where it is (the title, or the description's line)

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
    diff = subprocess.run(["git", "diff", "--cached", "--unified=0", "--no-color", "--text"],
                          capture_output=True, text=True, errors="replace", check=True).stdout
    names = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=AR"],
                           capture_output=True, text=True, check=True).stdout
    return names + "\n".join(l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++"))


def pr_text(title, body_file):
    """Each place in a pull request's title and description that names a private term, with the terms there."""
    places = [("title", title)] + [(f"description line {n}", line)
                                   for n, line in enumerate(open(body_file).read().splitlines(), 1)]
    return [f"{where}: {', '.join(terms)}" for where, text in places for terms in [found(text)] if terms]


def main(args):
    if len(args) == 3 and args[0] == "pr":
        try:
            places = pr_text(args[1], args[2])
        except (OSError, UnicodeDecodeError) as error:
            print(f"Can't read the description file {args[2]}: {error}", file=sys.stderr)
            return 2
        if places:
            print("Private terms from a profile in this pull request; the kit is public, so rewrite these:\n  "
                  + "\n  ".join(places), file=sys.stderr)
            return 1
        return 0
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
