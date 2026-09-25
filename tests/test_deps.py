#!/usr/bin/env python3
"""deps.txt is the complete list of programs the kit runs: a program the code calls must be declared there,
and the kit's Python must parse as the oldest Python it declares."""
import ast
import os
import re
import subprocess
import sys

KIT = os.path.expanduser("~/.agents")
# Calls whose program is a literal, the only ones a scan can read without running the code.
PYTHON_CALLS = [r'shutil\.which\(\s*"([\w.-]+)"', r'subprocess\.(?:run|Popen|check_output|check_call|call)\(\s*\[\s*"([\w.-]+)"',
                r'\b(?:run|execute)\(\s*\[?\s*"([\w.-]+)"', r'find_tool\(\s*\w+,\s*"([\w.-]+)"']
SHELL_CALLS = [r'command -v "?([\w.-]+)']
NOT_DEPENDENCIES = {
    "grep", "tar", "brew",  # macOS ships them; brew is how install.sh installs the rest
    "codex", "pi",  # harnesses: install.sh wires the ones it finds
    # verify_auto.py runs the project's own tools and reports them as skipped when the project has none
    "cargo", "eslint", "go", "gofmt", "php", "pytest", "ruff", "shellcheck",
}


def declared():
    """{program: minimum version or None}"""
    programs = {}
    for line in open(os.path.join(KIT, "deps.txt")):
        if line.strip() and not line.lstrip().startswith("#"):
            tier, spec, *_ = line.split()
            assert tier in ("required", "recommended", "optional"), f"deps.txt: unknown tier {tier!r}"
            program, _, minimum = spec.partition(">=")
            programs[program] = minimum or None
    return programs


def python_files():
    files = subprocess.run(["git", "ls-files"], cwd=KIT, capture_output=True, text=True, check=True).stdout.split()
    for rel in files:
        try:
            source = open(os.path.join(KIT, rel)).read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        if rel.endswith(".py") or "python" in source.split("\n", 1)[0]:
            yield rel, source


def called():
    files = subprocess.run(["git", "ls-files"], cwd=KIT, capture_output=True, text=True, check=True).stdout.split()
    programs = {}
    for rel in files:
        if rel.startswith(("tests/", "docs/", "site/src/")):
            continue
        path = os.path.join(KIT, rel)
        try:
            source = open(path).read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        first = source.split("\n", 1)[0]
        if rel.endswith(".py") or "python" in first:
            patterns = PYTHON_CALLS
        elif rel.endswith(".sh") or "bash" in first or first == "#!/bin/sh":
            patterns = SHELL_CALLS
        else:
            continue
        for pattern in patterns:
            for program in re.findall(pattern, source):
                programs.setdefault(program, rel)
    return programs


def main():
    fail = 0
    missing = {p: f for p, f in called().items() if p not in set(declared()) | NOT_DEPENDENCIES}
    for program, rel in sorted(missing.items()):
        print(f"FAIL {rel} runs {program}, which deps.txt doesn't declare")
        fail = 1
    if not missing:
        print("ok   every program the code runs is declared in deps.txt")
    oldest = tuple(int(part) for part in declared()["python3"].split("."))
    too_new = []
    for rel, source in python_files():
        try:
            ast.parse(source, feature_version=oldest)
        except SyntaxError as err:
            too_new.append(f"{rel}:{err.lineno}: {err.msg}")
    for problem in too_new:
        print(f"FAIL {problem} (deps.txt promises python3>={declared()['python3']})")
        fail = 1
    if not too_new:
        print(f"ok   the kit's Python parses as Python {declared()['python3']}")
    return fail


if __name__ == "__main__":
    sys.exit(main())
