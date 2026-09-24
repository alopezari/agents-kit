#!/usr/bin/env python3
"""adapters/apply_baseline.py adds missing settings and never changes one the user set."""
import json
import os
import subprocess
import sys
import tempfile

SCRIPT = os.path.expanduser("~/.agents/adapters/apply_baseline.py")
failures = []


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


def check(name, ok, detail=""):
    print(f"{'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f": {detail}"))
    if not ok:
        failures.append(name)


with tempfile.TemporaryDirectory(prefix="agents-baseline-") as tmp:
    base = os.path.join(tmp, "base.json")
    json.dump({"effortLevel": "medium", "fastMode": False, "env": {"X": "1"}}, open(base, "w"))
    target = os.path.join(tmp, "settings.json")
    json.dump({"effortLevel": "high", "env": {"Y": "2"}}, open(target, "w"))
    check("json --check reports missing keys", run(target, base, "--check").returncode == 1)
    run(target, base)
    got = json.load(open(target))
    check("json keeps the user's value and adds the rest",
          got == {"effortLevel": "high", "fastMode": False, "env": {"Y": "2", "X": "1"}}, got)

    base = os.path.join(tmp, "base.toml")
    open(base, "w").write('service_tier = "default"\n\n[features]\nfast_mode = false\n\n[agents]\nmax_threads = 3\n')
    target = os.path.join(tmp, "config.toml")
    open(target, "w").write('model = "m"\nservice_tier = "flex"\n\n[features]\nhooks = true\n')
    run(target, base)
    text = open(target).read()
    check("toml keeps the user's value", 'service_tier = "flex"' in text and 'service_tier = "default"' not in text, text)
    check("toml adds keys inside their tables",
          text.index("[features]") < text.index("fast_mode = false") < text.index("[agents]") < text.index("max_threads = 3"), text)
    check("toml is complete after applying", run(target, base, "--check").returncode == 0, text)

sys.exit(1 if failures else 0)
