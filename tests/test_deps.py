#!/usr/bin/env python3
"""deps.txt is the complete list of programs the kit runs: a program the code calls must be declared there."""
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
    names = set()
    for line in open(os.path.join(KIT, "deps.txt")):
        if line.strip() and not line.lstrip().startswith("#"):
            tier, program, *_ = line.split()
            assert tier in ("required", "recommended", "optional"), f"deps.txt: unknown tier {tier!r}"
            names.add(program)
    return names


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
    missing = {p: f for p, f in called().items() if p not in declared() | NOT_DEPENDENCIES}
    if missing:
        for program, rel in sorted(missing.items()):
            print(f"FAIL {rel} runs {program}, which deps.txt doesn't declare")
        return 1
    print("ok   every program the code runs is declared in deps.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
