#!/usr/bin/env python3
"""Check a spec has the shape the spec skill documents, so self-review and validate have something to check against.

  lint.py [spec.md]     the current branch's spec (path.sh) when no file is given

Fails, naming each problem, on a missing title, Goal line or section, and on a criterion whose `verify:` is
missing or only a placeholder. Warns, without failing, when there are fewer than 3 or more than 6 criteria.
"""
import os
import re
import subprocess
import sys

SECTIONS = ("Acceptance criteria", "Out of scope", "Assumptions", "Open questions")
# A verify that names no test, command or step: nobody could run it.
PLACEHOLDER_VERIFY = re.compile(r"(unit |manual |automated )?(tests?|testing|manual(ly)?|check|n/?a|tbd|todo|\?+|-+)\.?", re.I)


def criteria(text):
    """The numbered items under Acceptance criteria, each with its continuation lines joined."""
    section = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", text, re.M | re.S)
    items = []
    for line in (section.group(1) if section else "").splitlines():
        if re.match(r"\d+\.\s", line):
            items.append(line.strip())
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def problems(text):
    errors, warnings = [], []
    if not re.search(r"\A\s*# \S", text):
        errors.append("no title: the first line should be '# <issue id>: <title>'")
    if not re.search(r"^Goal:[ \t]*\S", text, re.M):
        errors.append("no 'Goal:' line")
    for name in SECTIONS:
        if not re.search(rf"^## {name}\s*$", text, re.M):
            errors.append(f"no '## {name}' section")
    items = criteria(text)
    for item in items:
        number = item.split(".")[0]
        verify = re.search(r"verify:\s*(.*)$", item, re.I)
        if not verify:
            errors.append(f"criterion {number} has no 'verify:' naming a test, command or manual step")
        elif PLACEHOLDER_VERIFY.fullmatch(verify.group(1).strip()):
            errors.append(f"criterion {number}: 'verify: {verify.group(1).strip()}' names no test, command or step")
    if items and not 3 <= len(items) <= 6:
        warnings.append(f"{len(items)} criteria: three to six is usual; more often means the issue should be split")
    if not items and re.search(r"^## Acceptance criteria\s*$", text, re.M):
        errors.append("no numbered criteria under '## Acceptance criteria'")
    return errors, warnings


def main():
    if len(sys.argv) > 1:
        path = sys.argv[1]
    else:
        path = subprocess.run([os.path.join(os.path.dirname(os.path.abspath(__file__)), "path.sh")],
                              capture_output=True, text=True, check=True).stdout.strip()
    try:
        text = open(path).read()
    except OSError as error:
        print(f"spec lint: cannot read the spec: {error}", file=sys.stderr)
        return 2
    errors, warnings = problems(text)
    for warning in warnings:
        print(f"warn {warning}")
    for error in errors:
        print(f"FAIL {error}")
    if errors:
        print(f"spec lint: {len(errors)} problem(s) in {path}")
        return 1
    print(f"ok   {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
