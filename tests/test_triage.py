#!/usr/bin/env python3
"""bin/triage: a shop's checkout raises the risk and a git checkout doesn't; a new dependency is named for review."""
import json
import os
import subprocess
import sys
import tempfile

TRIAGE = os.path.expanduser("~/.agents/bin/triage")
GIT_SENSE = ["cwd = pr_checkout(command, cwd)", "git checkout -b fix", "- uses: actions/checkout@v4",
             "# the main checkout serves the stack", "for path in known_checkouts():", "git_checkout(command, cwd)"]
SHOP_SENSE = ["return wc_get_checkout_url();", "'wc-blocks-checkout',", "if ( is_checkout() ) {",
              "// Redirect to the checkout page", "git checkout-free refund( $order );", "// Render the checkout-page"]


def payments_signal(line):
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        open(os.path.join(repo, "code.php"), "w").write(line + "\n")
        git("add", "code.php")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
        out = subprocess.run([TRIAGE, "--json", "--range", "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True)
        return any("payments" in reason for reason in json.loads(out.stdout)["reasons"])


def triage_change(before, after):
    """triage --json for one commit that turns the files in `before` into those in `after` ({path: text or None})."""
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)

        def write(files):
            for path, text in files.items():
                full = os.path.join(repo, path)
                if text is None:
                    os.remove(full)
                else:
                    open(full, "w").write(text)
        git("init", "-q", "-b", "main")
        write(before)
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "base")
        write(after)
        git("add", "-A")
        git("commit", "-q", "-m", "change")
        out = subprocess.run([TRIAGE, "--json", "--range", "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True)
        return json.loads(out.stdout)


def maintainability(result):
    return " | ".join(result["lenses"].get("maintainability", []))


PACKAGE = {"name": "shop", "version": "1.0.0", "scripts": {"build": "vite"}, "dependencies": {"react": "^18.0.0"}}
DEPENDENCY_CASES = [  # (case, before, after, names that must appear, names that must not)
    ("package.json dependency", {"package.json": json.dumps(PACKAGE)},
     {"package.json": json.dumps({**PACKAGE, "dependencies": {"react": "^18.0.0", "left-pad": "^1.3.0"}})}, ["left-pad"], ["react"]),
    ("package.json version bump and script", {"package.json": json.dumps(PACKAGE)},
     {"package.json": json.dumps({**PACKAGE, "scripts": {"build": "vite", "lint": "eslint ."},
                                  "dependencies": {"react": "^18.3.0"}})}, [], ["react", "lint", "eslint"]),
    ("new package.json", {}, {"package.json": json.dumps(PACKAGE)}, ["react"], []),
    ("composer require-dev", {"composer.json": '{"require": {"php": ">=8.1"}}'},
     {"composer.json": '{"require": {"php": ">=8.1"}, "require-dev": {"phpunit/phpunit": "^10"}}'}, ["phpunit/phpunit"], ["php "]),
    ("requirements line", {"requirements.txt": "requests==2.31\n"}, {"requirements.txt": "requests==2.32\nhttpx>=0.27\n"},
     ["httpx"], ["requests"]),
    ("go.mod require", {"go.mod": "module shop\n\nrequire (\n\tgithub.com/a/b v1.0.0\n)\n"},
     {"go.mod": "module shop\n\nrequire (\n\tgithub.com/a/b v1.1.0\n\tgithub.com/c/d v0.2.0\n)\n"}, ["github.com/c/d"], ["github.com/a/b"]),
    ("Cargo.toml dependency", {"Cargo.toml": '[package]\nname = "shop"\n\n[dependencies]\nserde = "1"\n'},
     {"Cargo.toml": '[package]\nname = "shop"\nversion = "0.2.0"\n\n[dependencies]\nserde = "1.0.200"\ntokio = { version = "1" }\n'},
     ["tokio"], ["serde", "version"]),
    ("deps.txt line", {"deps.txt": "required  jq  brew:jq  the scripts\n"},
     {"deps.txt": "required  jq  brew:jq  the scripts\noptional  ffmpeg  brew:ffmpeg  video checks\n"}, ["ffmpeg"], ["jq"]),
]

fail = 0
for case, before, after, named, not_named in DEPENDENCY_CASES:
    text = maintainability(triage_change(before, after))
    ok = all(f"new dependency: {n} " in text for n in named) and not any(f"new dependency: {n}" in text for n in not_named)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} dependencies, {case}: {text or 'no maintainability lens'}")
text = maintainability(triage_change({"package.json": json.dumps(PACKAGE)}, {"package.json": '{"dependencies": {'}))
ok = "package.json can't be read" in text and "check its dependencies by hand" in text
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} dependencies, a malformed manifest is named, not taken as none added: {text}")

for line, expected in [(l, False) for l in GIT_SENSE] + [(l, True) for l in SHOP_SENSE]:
    ok = payments_signal(line) == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'payments' if expected else 'no payments'}: {line}")
sys.exit(fail)
