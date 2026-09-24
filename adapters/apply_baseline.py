#!/usr/bin/env python3
"""Add the kit's baseline harness settings to a settings file, only where a key is missing.

  apply_baseline.py <settings.json|config.toml> <baseline file> [--check]

A key that is already set is never changed, so choices made later (e.g. /config) survive re-running
install.sh. Prints one line per missing key; --check only reports. Exit 1 when --check finds any.
TOML support covers what the baselines use: `key = value` lines at the top level and in [tables].
"""
import json
import os
import re
import sys

TABLE = re.compile(r"^\s*\[([^\[\]]+)\]\s*$")
KEY = re.compile(r"^\s*([\w.-]+|\"[^\"]+\")\s*=")


def missing_json(target, baseline, prefix=""):
    """[(dotted key, value)] for baseline leaves absent from target."""
    missing = []
    for key, value in baseline.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            missing += missing_json(target[key], value, f"{prefix}{key}.")
        elif key not in target:
            missing.append((prefix + key, value))
    return missing


def toml_entries(lines):
    """{(table, key): line index} for `key = value` lines; the top level is table ''."""
    table, found = "", {}
    for i, line in enumerate(lines):
        header = TABLE.match(line)
        if header:
            table = header.group(1).strip()
        elif KEY.match(line) and not line.lstrip().startswith("#"):
            found[(table, KEY.match(line).group(1))] = i
    return found


def apply_toml(path, baseline_path, check):
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    present = toml_entries(lines)
    tables = {TABLE.match(l).group(1).strip(): i for i, l in enumerate(lines) if TABLE.match(l)}
    wanted = toml_entries(baseline := open(baseline_path).read().splitlines())
    missing = [(table, key, baseline[i]) for (table, key), i in wanted.items() if (table, key) not in present]
    for table, key, line in missing:
        print(f"{'missing' if check else 'added'} {table + '.' if table else ''}{key} ({line.split('=', 1)[1].strip()})")
    if check or not missing:
        return missing
    top = [line for table, _, line in missing if not table]
    # Top-level keys must come before the first table, or TOML reads them as part of it.
    lines = top + lines
    for table, _, line in (m for m in missing if m[0]):
        if table in tables:
            header = next(i for i, l in enumerate(lines) if TABLE.match(l) and TABLE.match(l).group(1).strip() == table)
            lines.insert(header + 1, line)
        else:
            lines += ["", f"[{table}]", line]
            tables[table] = len(lines)
    with open(path, "w") as fh:
        fh.write("\n".join(lines) + "\n")
    return missing


def apply_json(path, baseline_path, check):
    current = json.load(open(path)) if os.path.exists(path) else {}
    missing = missing_json(current, json.load(open(baseline_path)))
    for key, value in missing:
        print(f"{'missing' if check else 'added'} {key} ({json.dumps(value)})")
    if check or not missing:
        return missing
    for key, value in missing:
        node = current
        *parents, leaf = key.split(".")
        for parent in parents:
            node = node.setdefault(parent, {})
        node[leaf] = value
    with open(path, "w") as fh:
        json.dump(current, fh, indent=2)
        fh.write("\n")
    return missing


def main():
    path, baseline, check = sys.argv[1], sys.argv[2], "--check" in sys.argv
    missing = (apply_toml if path.endswith(".toml") else apply_json)(path, baseline, check)
    return 1 if check and missing else 0


if __name__ == "__main__":
    sys.exit(main())
