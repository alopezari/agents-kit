#!/usr/bin/env python3
"""Check a spec has the shape the kit-spec skill documents, so review and validate have something to check against.

  lint.py [spec.md]     the current branch's spec (path.sh) when no file is given

Fails, naming each problem, on a missing title, Goal line or section, a `Risk:` line missing, repeated, with an unknown
tier or without a reason (or with the template's placeholder for one), a `## Decisions` section on a spec whose tier
isn't high, no numbered criteria, a criterion whose `verify:` is missing, empty or only a placeholder, and an empty or
unfilled Change map or Decisions; exits 2 when the spec can't be read.
Warns, without failing, when there are fewer than 3 or more than 6 criteria.
"""
import os
import re
import subprocess
import sys

TIERS = ("low", "standard", "high")
RISK_LINE = re.compile(r"^Risk:[ \t]*([^\s—–-]*)(.*)$", re.M)
DECISIONS = re.compile(r"^## Decisions\s*$", re.M | re.I)
SECTIONS = ("Acceptance criteria", "Out of scope", "Assumptions", "Open questions")
# A verify that names no test, command or step: nobody could run it.
PLACEHOLDER_VERIFY = re.compile(r"((run )?(the )?(unit |manual |automated )?(tests?|testing)( pass(es)?)?|manual(ly)?|check"
                                r"|none|n/?a|tbd|todo|see above|\?+|-+|)\.?", re.I)


def criteria(text):
    """The numbered items under Acceptance criteria, each with its continuation lines joined."""
    section = re.search(r"^## Acceptance criteria\s*$(.*?)(?=^## |\Z)", text, re.M | re.S | re.I)
    items = []
    for line in (section.group(1) if section else "").splitlines():
        if re.match(r"\d+\.\s", line):
            items.append(line.strip())
        elif items and line.strip():
            items[-1] += " " + line.strip()
    return items


def risk(text):
    """The spec's Risk line as (tier, reason, None), or (None, None, the problem with it). bin/triage reads it too."""
    lines = RISK_LINE.findall(text)
    if not lines:
        return None, None, f"no 'Risk:' line: write 'Risk: <{'|'.join(TIERS)}> — <reason>' under the Goal"
    if len(lines) > 1:
        return None, None, f"more than one 'Risk:' line ({len(lines)}): keep the one that decides"
    tier, rest = lines[0]
    if tier.lower() not in TIERS:
        return None, None, f"'Risk:': '{tier}' is not a tier; use one of {', '.join(TIERS)}"
    reason = re.fullmatch(r"(?:\s*[—–]|\s+--?)\s*(\S.*)", rest)  # a hyphen needs a space before it: 'high-ish' isn't high
    if not reason or re.fullmatch(r"<.*>", reason.group(1).strip()):
        return None, None, f"'Risk: {tier}' gives no reason: add ' — <the signal or Change map item that decides it>'"
    return tier.lower(), reason.group(1).strip(), None


def problems(text):
    errors, warnings = [], []
    if not re.search(r"\A\s*# \S", text):
        errors.append("no title: the first line should be '# <title>', with the issue id first when there is one")
    if not re.search(r"^Goal:[ \t]*\S", text, re.M):
        errors.append("no 'Goal:' line")
    tier, _, problem = risk(text)
    if problem:
        errors.append(problem)
    elif tier != "high" and DECISIONS.search(text):
        errors.append(f"'## Decisions' means a one-way door: the tier is high, not {tier}")
    for name in SECTIONS:
        if not re.search(rf"^## {name}\s*$", text, re.M | re.I):
            errors.append(f"no '## {name}' section")
    items = criteria(text)
    for item in items:
        number = item.split(".")[0]
        verify = re.search(r"verify:\s*(.*)$", item, re.I)
        if not verify and re.search(r"\bverify\b", item, re.I):
            errors.append(f"criterion {number}: write its check as '— verify: <test, command or step>', with the colon")
        elif not verify:
            errors.append(f"criterion {number} has no 'verify:' naming a test, command or manual step")
        elif PLACEHOLDER_VERIFY.fullmatch(verify.group(1).strip()):
            errors.append(f"criterion {number}: 'verify: {verify.group(1).strip()}' names no test, command or step")
    if items and not 3 <= len(items) <= 6:
        warnings.append(f"{len(items)} criteria: three to six is usual; more often means the issue should be split")
    if not items and re.search(r"^## Acceptance criteria\s*$", text, re.M | re.I):
        errors.append("no numbered criteria under '## Acceptance criteria'")
    for name, what in (("Change map", "list the ways in, derived data and failures"),
                       ("Decisions", "write each as '<chosen> over <alternative>, because <reason>'")):
        section = re.search(rf"^## {name}\s*$(.*?)(?=^## |\Z)", text, re.M | re.S | re.I)
        if not section:
            continue
        # Bullets, numbered items or table rows; a table's header and separator rows aren't items.
        lines = [line.strip() for line in section.group(1).splitlines()
                 if re.match(r"\s*([-*]|\d+\.|\|)\s*\S", line) and not re.fullmatch(r"\s*\|[\s|:-]*\|?\s*", line)]
        items = [line for line in lines if not line.startswith("|")] or lines[1:]
        unfilled = [line for line in items if re.search(r"<(file:line|chosen|alternative|reason|[^<>]*\s[^<>]*)>", line) or re.fullmatch(r"[-*\d.\s]*[\w ]+:\s*", line)]
        if not items:
            errors.append(f"'## {name}' is empty: {what}, or drop the section")
        for line in unfilled:
            errors.append(f"'## {name}' has an unfilled item: {line}")
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
