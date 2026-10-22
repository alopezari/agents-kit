#!/usr/bin/env python3
"""bin/triage: a shop's checkout raises the risk and a git checkout doesn't; a new dependency is named for review."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

TRIAGE = os.path.expanduser("~/.agents/bin/triage")
# Committed in a test repo's base, it turns the wordpress pack on: most samples here are WordPress code.
WORDPRESS_MARKER = ".wp-env.json"
GIT_SENSE = ["cwd = pr_checkout(command, cwd)", "git checkout -b fix", "- uses: actions/checkout@v4",
             "$msg = 'the main checkout serves the stack';", "for path in known_checkouts():", "git_checkout(command, cwd)",
             "def pr_checkout_unknown(command, payload):", "repo = phase_switches.checkout(os.getcwd())[0]",
             "def checkout(cwd, timeout=5):", "in_checkout(root, review_stamp.fingerprint)",
             "check_checkout(root, session, payload)", "const head = await checkout($, cwd);"]
# "// Redirect to the checkout page" and "// Render the checkout-page" are comments since B3, which must not fire: their
# code versions stay here, so the checkout-page exception to GIT_CHECKOUT keeps a test.
SHOP_SENSE = ["return wc_get_checkout_url();", "'wc-blocks-checkout',", "if ( is_checkout() ) {",
              "git checkout-free refund( $order );", "echo 'Redirect to the checkout page';",
              "echo 'Render the checkout-page';",
              "function remember_checkout_details() { return get_checkout_url(); }", "process_checkout(cart, cwd)",
              "renderCheckout(root);", "goToCheckout(path)", "$this->checkout(path)", "router.push(checkout(path))",
              "new Checkout(root, opts)", "cart.checkout(root)", "self.checkout(cwd, order)", "return checkout(root, cart);",
              "Cart::checkout(cwd)", "checkout(cwd.order)"]


def payments_signal(line):
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        open(os.path.join(repo, WORDPRESS_MARKER), "w").write("{}\n")
        git("add", WORDPRESS_MARKER)
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
        open(os.path.join(repo, "code.php"), "w").write(line + "\n")
        git("add", "code.php")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
        out = subprocess.run([TRIAGE, "--json", "--range", "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True)
        return any("payments" in reason for reason in json.loads(out.stdout)["reasons"])


def triage_change(before, after):
    """triage --json for one commit that turns the files in `before` into those in `after` ({path: text or None}), in a
    WordPress repo."""
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)

        def write(files):
            for path, text in files.items():
                full = os.path.join(repo, path)
                if text is None:
                    os.remove(full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    open(full, "w").write(text)
        git("init", "-q", "-b", "main")
        write({WORDPRESS_MARKER: "{}\n"})
        write(before)
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "base")
        write(after)
        git("add", "-A")
        git("commit", "-q", "-m", "change")
        out = subprocess.run([TRIAGE, "--json", "--range", "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True,
                             timeout=60)
        return json.loads(out.stdout)


def maintainability(result):
    return " | ".join(result["lenses"].get("maintainability", []))


PACKAGE = {"name": "shop", "version": "1.0.0", "scripts": {"build": "vite"}, "dependencies": {"react": "^18.0.0"}}
MOVED = '{"dependencies": {"react": "18", "vue": "3", "svelte": "4", "preact": "10", "solid-js": "1"}}\n'
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
    ("single-line go.mod require", {"go.mod": "module shop\n"},
     {"go.mod": "module shop\n\nrequire github.com/e/f v1.0.0\nrequire (github.com/g/h v1.0.0)\nrequire\tgithub.com/i/j v1\n"
                "replace github.com/e/f => ../f\n"},
     ["github.com/e/f", "github.com/g/h", "github.com/i/j"], ["replace", "=>"]),
    ("requirements extras, markers, URLs and includes", {"requirements.txt": "foo_bar==1\n"},
     {"requirements.txt": "Foo-Bar==2\nuvicorn[standard]>=0.30; python_version > '3.8'\n-r base.txt\n"
                          "git+https://github.com/x/y.git#egg=y\n-e git+https://github.com/x/useful.git#egg=useful\n./local-pkg\n"},
     ["uvicorn", "y", "useful"], ["foo-bar", "git", "https", ".", "base"]),
    ("requirements in a folder", {}, {"requirements/dev.txt": "pytest==8\n"}, ["pytest"], []),
    ("composer platform packages", {"composer.json": '{"require": {"php": ">=8.1"}}'},
     {"composer.json": '{"require": {"php": ">=8.1", "ext-json": "*", "lib-curl": "*", "composer-plugin-api": "^2"}}'},
     [], ["ext-json", "lib-curl", "composer-plugin-api"]),
    ("Cargo.toml quoted and dotted keys", {"Cargo.toml": "[dependencies]\n"},
     {"Cargo.toml": '[dependencies]\n"rand" = "0.8"\nserde.workspace = true\n'}, ["rand", "serde"], []),
    ("deps.txt line", {"deps.txt": "required  jq  brew:jq  the scripts\n"},
     {"deps.txt": "required  jq  brew:jq  the scripts\noptional  ffmpeg  brew:ffmpeg  video checks\n"}, ["ffmpeg"], ["jq"]),
    ("a manifest moved, nothing else", {"old/package.json": MOVED},
     {"old/package.json": None, "new/package.json": MOVED}, [], ["react"]),
    ("a manifest renamed to another format", {"composer.json": '{"require": {"php": ">=8.1"}, "dependencies": {"react": "18"}}'},
     {"composer.json": None, "package.json": '{"require": {"php": ">=8.1"}, "dependencies": {"react": "18"}}'}, ["react"], []),
    ("a manifest moved and edited", {"old/package.json": MOVED},
     {"old/package.json": None, "new/package.json": MOVED.replace('"1"}', '"1", "lodash": "4"}')}, ["lodash"], ["react"]),
]

fail = 0
for case, before, after, named, not_named in DEPENDENCY_CASES:
    text = maintainability(triage_change(before, after))
    ok = all(f"new dependency: {n} " in text for n in named) and not any(f"new dependency: {n}" in text for n in not_named)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} dependencies, {case}: {text or 'no maintainability lens'}")
for case, before in (("new", json.dumps(PACKAGE)), ("base", '{"dependencies": {')):
    after = '{"dependencies": {' if case == "new" else json.dumps(PACKAGE)
    for broken in (after, '{"dependencies": ["left-pad"]}') if case == "new" else (after,):
        text = maintainability(triage_change({"package.json": before}, {"package.json": broken}))
        ok = "package.json can't be read" in text and "check its dependencies by hand" in text
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} dependencies, a malformed manifest ({case}) is named, not taken as none added: {text}")


def triage_worktree(subdir):
    """triage with no --range, as the review runs it: an uncommitted dependency, from the root or a subdirectory."""
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        os.makedirs(os.path.join(repo, "src"))
        open(os.path.join(repo, "package.json"), "w").write(json.dumps(PACKAGE))
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        open(os.path.join(repo, "package.json"), "w").write(json.dumps({**PACKAGE, "dependencies": {"left-pad": "1"}}))
        out = subprocess.run([TRIAGE, "--json"], cwd=os.path.join(repo, subdir), capture_output=True, text=True)
        return maintainability(json.loads(out.stdout))


for subdir in (".", "src"):
    text = triage_worktree(subdir)
    ok = "new dependency: left-pad (package.json)" in text
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} dependencies, uncommitted, triage run from {subdir}: {text or 'no maintainability lens'}")
for rng in ("HEAD~1", "HEAD~1...HEAD"):
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        for message, deps in (("base", {}), ("change", {"left-pad": "1"})):
            open(os.path.join(repo, "package.json"), "w").write(json.dumps({"dependencies": deps}))
            subprocess.run("git add -A && git -c user.name=t -c user.email=t@t commit -qm " + message, shell=True, cwd=repo,
                           check=True)
        out = subprocess.run([TRIAGE, "--json", "--range", rng], cwd=repo, capture_output=True, text=True)
        ok = out.returncode == 0 and "new dependency: left-pad" in maintainability(json.loads(out.stdout))
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} dependencies, --range {rng}: {out.stderr.strip()[-120:] or 'named'}")

# A group that repeats and repeats inside backtracks exponentially on a long near-miss (#31's guard took 31 s).
for line, expected in [(r'PREFIXES = r"(?:\w+=\S*\s+|env\s+(?:-\S*\s+)*)*gh"', True), ("const re = /(a+)+$/;", True),
                       (r"PATTERN = re.compile(r'\d+(?:\.\d+)?')", False), ("$ok = preg_match('/^[a-z]+$/', $s);", False),
                       ("const re = /(a{1,})+$/;", True), ("const re = /([+])+/;", False), ("const re = /(x{2}a+)*$/;", True),
                       ("total = (i+1)*2", False), ('patterns = [re.compile(r"(a+)+$")]', True),
                       ('re.compile(r"safe"); total = (i+1)*2', False), ("const re = /(a+)+$/", True), ("total = a/(i+1)*2/g;", False),
                       ('re.compile(r"^(?:a?b?)+$")', True), ('re.compile(r"^(a+){20}$")', True), ('re.compile(r"(\\.\\d+)?$")', False)]:
    result = triage_change({}, {"code.py": line + "\n"})
    got = "regex worst-case timing" in result["tests"]
    ok = got == expected and (not expected or "nested quantifier" in " ".join(result["lenses"].get("performance", [])))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'regex timing' if expected else 'no regex timing'}: {line}")
# CPU time of the git and triage processes, not wall time: run.sh runs every section at once, and a loaded CI runner
# took 3.7 s of wall time for well under a second of work.
started = os.times()
triage_change({}, {"code.py": "x = '(" + "+" * 30000 + "'\n", "b.py": 'P = re.compile(r"(a+' + "b{1}" * 30 + ')$")\n',
                   "c.py": "".join('D = "' + "=/[" * 650 + '"\n' for _ in range(300))})
ended = os.times()
cpu = ended.children_user + ended.children_system - started.children_user - started.children_system
ok = cpu < 3
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} the regex scan stays fast on a long line ({cpu:.1f}s of CPU)")
long_line = 'L = re.compile(r"' + "a" * 180 + '(b+)+$")'
result = triage_change({}, {"code.py": "".join(f'P{c} = re.compile(r"^({c}+)+$")\n' for c in "wxyz") + long_line + "\n"})
timing = result["tests"].get("regex worst-case timing", [])
ok = len(timing) == 5 and all(t.startswith("code.py: `P") and t.endswith('+)+$")`') for t in timing[:4]) \
    and timing[4] == f"code.py: `{long_line}`"
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} every flagged regex is named for timing, whole: {timing}")

for label, after, ui in [("a component", {"src/Card.tsx": "export const Card = () => <div className='card' />\n"}, "src/Card.tsx"),
                         ("a style-only change", {"site.css": "body { color: #222 }\n"}, "site.css"),
                         ("a Svelte page", {"src/routes/page.svelte": "<h1>Hi</h1>\n"}, "src/routes/page.svelte"),
                         ("four components", {f"src/C{i}.tsx": "export const C = 1\n" for i in range(4)},
                          [f"src/C{i}.tsx" for i in range(4)]),
                         ("a script", {"tool.py": "print(1)\n"}, None)]:
    result = triage_change({}, after)
    lens, test = result["lenses"].get("design"), result["tests"].get("design (impeccable detect)")
    ui = [ui] if isinstance(ui, str) else ui
    ok = (lens == ui and test == ui) if ui else (lens is None and test is None)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'design checks' if ui else 'no design checks'} for {label} ({result['tier']}): {lens}")

page = "---\ntitle: Pricing\n---\n<h1>Pricing</h1>\n"
for label, old, new in [("a rename alone is a change to the renamed file", "src/pages/old.astro", "src/pages/pricing.astro"),
                        ("a renamed file keeps its non-ASCII name", "src/pages/old.astro", "src/pages/précios.astro"),
                        ("a renamed file keeps its name when git quotes it", "src/pages/old.astro", 'src/pages/page"name.astro'),
                        ("a UI file moved out of a UI folder still gets design checks", "src/components/Card.php", "src/helpers/Card.php")]:
    renamed = triage_change({old: page}, {old: None, new: page})
    ok = (new if new.endswith(".astro") else old) in renamed["lenses"].get("design", [])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {renamed['lenses'].get('design')}")
crashed = []
for label, before, after in [("a deleted line that looks like a quoted header", {"a.sql": '-- "unterminated\n', "b.py": "x = 1\n"},
                              {"a.sql": None, "b.py": "x = 2\n"}),
                             ("a renamed path with a line separator in it", {"src/pages/old.astro": page},
                              {"src/pages/old.astro": None, "src/pages/new\u2028page.astro": page})]:
    try:
        result = triage_change(before, after)
    except json.JSONDecodeError:
        result = None
    ok = result is not None and (not label.startswith("a renamed") or
                                 "src/pages/new\u2028page.astro" in result["lenses"].get("design", []))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {result and result['lenses'].get('design')}")
styles = "".join(f".rule-{i} {{ color: red; }}\n" for i in range(20))
edited = triage_change({"src/old page.css": styles}, {"src/old page.css": None, "src/new page.css": styles + ".x { }\n"})
ok = edited["files"] == 2
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a renamed file with a space in its name counts once per path: {edited['files']} files")
removed = triage_change({"a.py": "x = 1\n", "src/Old.tsx": "export const Old = 1\n"}, {"a.py": "x = 2\n", "src/Old.tsx": None})
ok = "src/Old.tsx" in removed["lenses"].get("design", []) and "design" in removed["lenses"]
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a file deleted after another change is still counted: {removed['lenses'].get('design')}")

# --lens-briefs: the kit:review-<key> agents are briefed from it, so every lens heading needs a key and keeps its text.
LENSES = os.path.expanduser("~/.agents/skills/kit-review/lenses.md")
sys.dont_write_bytecode = True  # a bin/__pycache__ would be listed by bin/docs as a tool
loader = importlib.machinery.SourceFileLoader("triage", TRIAGE)
module = importlib.util.module_from_spec(importlib.util.spec_from_loader("triage", loader))
loader.exec_module(module)
# Every key triage() can put in its lenses, read from its source rather than restated here.
source = open(TRIAGE).read()
selectable = {*re.findall(r'lenses(?:\[|\.setdefault\()"([\w-]+)"', source), *re.findall(r'"([\w-]+)": \["always"\]', source), *module.LENS_SIGNALS}
out = subprocess.run([TRIAGE, "--lens-briefs"], capture_output=True, text=True)
try:
    briefs = json.loads(out.stdout)
except json.JSONDecodeError:
    briefs = [{"key": "", "title": "not JSON: " + out.stdout[:60], "brief": ""}]
headings = re.findall(r"^## (.+)$", open(LENSES).read(), re.M)
bundled = {t: subprocess.run(["awk", "-v", f"lens=## {t}", "$0==lens{on=1;next} /^## /{on=0} on", LENSES],
                             capture_output=True, text=True).stdout.strip() for t in headings}
ok = ([b["title"] for b in briefs] == headings and selectable <= {b["key"] for b in briefs}
      and all(b["brief"] == bundled[b["title"]] and b["brief"] for b in briefs))
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --lens-briefs: every heading, every key triage selects, each brief as bundle.sh cuts it: "
      f"{out.stderr.strip() or [b['key'] for b in briefs]}")
skill = open(os.path.expanduser("~/.agents/skills/kit-review/SKILL.md")).read()
named = set(re.findall(r"kit:review-([\w-]+)", skill)) - {"<lens>"} if "kit:review-" in skill else set()
ok = bool(named) and named <= {b["key"] for b in briefs}
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} every kit:review-<key> the kit-review skill names is a lens key: {sorted(named)}")
lenses_text = open(LENSES).read()
for case, text, expected in [
    ("a lens heading with no key", lenses_text + "\n## Haptics\n\nYou are reviewing vibrations.\n", "no key in LENS_TITLES for Haptics"),
    ("a key with no lens heading", lenses_text.replace("## Operability\n", "## Operating\n"), "no lens headed Operability"),
    ("a lens headed twice", lenses_text + "\n## Security\n\nAgain.\n", "Security twice"),
]:
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as kit:
        os.makedirs(os.path.join(kit, "bin")); os.makedirs(os.path.join(kit, "skills", "kit-review"))
        shutil.copy(TRIAGE, os.path.join(kit, "bin", "triage"))
        with open(os.path.join(kit, "skills", "kit-review", "lenses.md"), "w") as f:
            f.write(text)
        out = subprocess.run([os.path.join(kit, "bin", "triage"), "--lens-briefs"], capture_output=True, text=True)
        ok = out.returncode != 0 and expected in out.stderr and not out.stdout
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} --lens-briefs fails on {case}: exit {out.returncode}, {out.stderr.strip()}")

# The user's second-model switch takes the cross-model pass out of triage's answer; the rest of the review stays.
with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as home, \
        tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    open(os.path.join(repo, "app.py"), "w").write("x = 1\n" * 120)
    subprocess.run("git add -A && git -c user.name=t -c user.email=t@t commit -qm base", shell=True, cwd=repo, check=True)
    open(os.path.join(repo, "app.py"), "w").write("x = 2\n" * 120)
    env = {**os.environ, "HOME": home}

    def switch(*args):
        subprocess.run(["python3", os.path.expanduser("~/.agents/hooks/phase_switches.py"), "set", *args], cwd=repo,
                       check=True, capture_output=True, env=env)

    def cross_model(harness=None):
        run_env = env
        if harness is not None:  # "" leaves triage nothing to tell the harness by; a dict sets the variables it names
            run_env = {k: v for k, v in env.items() if k not in ("AGENTS_HARNESS", "CODEX_THREAD_ID", "CLAUDECODE")}
            run_env.update(harness if isinstance(harness, dict) else {"AGENTS_HARNESS": harness} if harness else {})
        out = subprocess.run([TRIAGE, "--json"], cwd=repo, capture_output=True, text=True, env=run_env)
        return json.loads(out.stdout)["cross_model"] if out.returncode == 0 else out.stderr.strip()[-200:]
    usual = cross_model()
    for args, expected, case in ((("off", "second-model", "repo"), "none (second-model switched off)", "off for the repo"),
                                 (("on", "second-model", "branch"), usual, "on for the branch, over the repo's off"),
                                 (("off", "review", "global"), usual, "another phase off")):
        switch(*args)
        got = cross_model()
        ok = got == expected and usual in ("none", "correctness", "per lens")
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} cross-model, second-model {case}: {got} (usual: {usual})")
    switch("off", "second-model", "branch")
    text = subprocess.run([TRIAGE], cwd=repo, capture_output=True, text=True, env=env).stdout
    ok = "; cross-model: none (second-model switched off))" in text.splitlines()[0]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} the text output names it too: {text.splitlines()[0]}")
    # A reviewer's own switch: Claude reviews a Codex session, Codex a Claude Code one.
    switch("on", "second-model", "branch")
    switch("off", "claude-review", "global")
    for harness, expected in (("codex", "none (claude-review switched off)"), ("claude-code", usual),
                              ("", f"{usual} (unless the reviewer is off: claude-review)")):
        got = cross_model(harness)
        ok = got == expected
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} cross-model, claude-review off, harness {harness or 'unknown'}: {got}")
    for variables, expected, case in (({"CODEX_THREAD_ID": "t", "CLAUDECODE": "1"}, "none (claude-review switched off)",
                                       "a Codex run started from Claude Code"),
                                      ({"CLAUDECODE": "1"}, usual, "Claude Code by CLAUDECODE"),
                                      ({"CODEX_THREAD_ID": "t"}, "none (claude-review switched off)", "Codex by CODEX_THREAD_ID"),
                                      ({"AGENTS_HARNESS": "claude-code", "CODEX_THREAD_ID": "t"}, usual,
                                       "AGENTS_HARNESS over CODEX_THREAD_ID"),
                                      ({"AGENTS_HARNESS": "cursor"}, f"{usual} (unless the reviewer is off: claude-review)",
                                       "a harness triage doesn't know")):
        got = cross_model(variables)
        ok = got == expected
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} cross-model, claude-review off, {case}: {got}")
    switch("off", "codex-review", "global")
    got = cross_model("")
    ok = got == "none (claude-review, codex-review switched off)"
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} both reviewers off, harness unknown: {got}")
    switch("on", "codex-review", "global")
    switch("off", "second-model", "branch")
    got = cross_model("codex")
    ok = got == "none (second-model switched off)"
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} second-model off names itself over the reviewer's own off: {got}")
    switch("on", "claude-review", "global")
    switch("on", "second-model", "branch")
    switch("off", "codex-review", "repo")
    for harness, expected in (("claude-code", "none (codex-review switched off)"), ("codex", usual)):
        got = cross_model(harness)
        ok = got == expected
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} cross-model, codex-review off, harness {harness}: {got}")
    switch("on", "codex-review", "repo")
    with open(os.path.join(home, ".agents", "approvals", "phases", "global.json"), "w") as f:
        f.write("not json")
    got = cross_model()
    ok = got.startswith(f"{usual} (Couldn't read the phase switches") and got.endswith("tell the user)")
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} an unreadable switch store keeps the usual cross-model pass and says why: {got}")

# bundle.sh: the cross-model prompt carries the severities, the bug-class rule, the repo's review guide and the findings
# an earlier round decided. Its HOME holds a kit whose repos/ is the test's own, so the guide is the test's.
with tempfile.TemporaryDirectory(prefix="agents-test-bundle-") as home, \
        tempfile.TemporaryDirectory(prefix="agents-test-bundle-") as repo:
    kit = os.path.realpath(os.path.expanduser("~/.agents"))
    os.makedirs(os.path.join(home, ".agents", "repos"))
    for entry in os.listdir(kit):
        if entry != "repos":
            os.symlink(os.path.join(kit, entry), os.path.join(home, ".agents", entry))
    subprocess.run("git init -q -b main && echo 'x = 1' > app.py && git add -A && "
                   "git -c user.name=t -c user.email=t@t commit -qm base && echo 'x = 2' > app.py",
                   shell=True, cwd=repo, check=True)
    guide_dir = os.path.join(home, ".agents", "repos", os.path.basename(repo))
    decided = os.path.join(home, "decided.txt")
    env = {**os.environ, "HOME": home}

    def bundle(*extra):
        return subprocess.run([os.path.join(home, ".agents", "skills", "kit-review", "bundle.sh"), "main", "Correctness",
                               "Double x", *extra], cwd=repo, capture_output=True, text=True, env=env)
    plain = bundle()
    rules = ("Important", "Nit", "Pre-existing", "at most three", "every other place")
    ok = plain.returncode == 0 and all(r in plain.stdout for r in rules) and "+x = 2" in plain.stdout
    ok = ok and "review guide" not in plain.stdout and "already decided" not in plain.stdout
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} bundle.sh asks for the three severities, the nit cap and every instance, "
          f"and with no guide or decided file mentions neither: exit {plain.returncode} {plain.stderr.strip()[-200:]}")
    with open(os.path.join(kit, "skills", "kit-review", "SKILL.md")) as f:
        skill = f.read()
    shared = re.search(r"Severity is one of: .*? under that one finding\.", skill)
    ok = shared is not None and shared.group(0) in plain.stdout
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} SKILL.md gives same-model reviewers the severities, nit cap and bug-class rule "
          f"bundle.sh gives cross-model ones, word for word: {shared and shared.group(0)[:80]}")
    os.makedirs(guide_dir)
    with open(os.path.join(guide_dir, "review.md"), "w") as f:
        f.write("A wrong total on an invoice is the failure that matters here. Skip vendor/.\n")
    with open(decided, "w") as f:
        f.write("Rejected:  x can be None — evidence: app.py:1 sets it\n")
    first = bundle()
    ok = (first.returncode == 0 and "wrong total on an invoice" in first.stdout and "the guide wins" in first.stdout
          and "already decided" not in first.stdout)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} a first pass gets the review guide, which wins over the defaults, and no decided "
          f"findings: exit {first.returncode} {first.stderr.strip()[-200:]}")
    full = bundle(decided)
    ok = (full.returncode == 0 and "wrong total on an invoice" in full.stdout and "x can be None" in full.stdout
          and "Report only a new instance at another location" in full.stdout and full.stdout.index("```diff") > full.stdout.index("x can be None"))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} bundle.sh includes the repo's review guide and the decided findings, before the diff: "
          f"exit {full.returncode} {full.stderr.strip()[-200:]}")
    missing = bundle(os.path.join(home, "nope.txt"))
    ok = missing.returncode != 0 and "bundle.sh: can't read the decided findings" in missing.stderr and "```diff" not in missing.stdout
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} bundle.sh fails on a decided file that isn't there: exit {missing.returncode} "
          f"{missing.stderr.strip()[-200:]}")
    empty = bundle("")
    ok = empty.returncode != 0 and "bundle.sh: no decided-findings file" in empty.stderr and "```diff" not in empty.stdout
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} bundle.sh fails on an empty decided-file argument, a variable lost between shells: "
          f"exit {empty.returncode} {empty.stderr.strip()[-200:]}")
    os.chmod(os.path.join(guide_dir, "review.md"), 0)
    unreadable = bundle()
    ok = unreadable.returncode != 0 and "bundle.sh: can't read the review guide" in unreadable.stderr and "```diff" not in unreadable.stdout
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} bundle.sh fails on a review guide it can't read: exit {unreadable.returncode} "
          f"{unreadable.stderr.strip()[-200:]}")

# The spec's Risk line is a prediction made before the code: triage takes the higher of it and the diff's tier.
SPEC_TEXT = "# Round prices\n\nGoal: totals match.\n\n{risk}\n## Acceptance criteria\n1. x — verify: tests/test_x.py\n{extra}"


def triage_spec(risk, diff, extra="", setup=None, plain=False, args=()):
    """triage, as the review runs it, on a repo whose spec has `risk` as its Risk line; `diff` is "docs", "code" or "auth"."""
    kit = os.path.realpath(os.path.expanduser("~/.agents"))
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as home, \
            tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        os.makedirs(os.path.join(home, ".agents"))
        for part in ("bin", "hooks", "skills"):  # not approvals/: the switches a case sets stay in this HOME
            os.symlink(os.path.join(kit, part), os.path.join(home, ".agents", part))
        env = {**os.environ, "HOME": home}

        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        open(os.path.join(repo, "README.md"), "w").write("# Shop\n")
        open(os.path.join(repo, "app.py"), "w").write("x = 1\n")
        open(os.path.join(repo, WORDPRESS_MARKER), "w").write("{}\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        git("checkout", "-q", "-b", "round-prices")
        changed = {"docs": ("README.md", "# Shop\n\nRounds prices.\n"), "code": ("app.py", "x = 2\n"),
                   "auth": ("app.py", "if current_user_can('edit'):\n    x = 2\n")}[diff]
        open(os.path.join(repo, changed[0]), "w").write(changed[1])
        spec = subprocess.run([os.path.join(kit, "skills", "kit-spec", "path.sh")], cwd=repo, capture_output=True,
                              text=True, env=env, check=True).stdout.strip()
        if risk is not None:
            open(spec, "w").write(SPEC_TEXT.format(risk=risk, extra=extra))
        if setup:
            setup(home, repo, spec, env)
        out = subprocess.run([TRIAGE, *args] + ([] if plain else ["--json"]), cwd=repo, capture_output=True, text=True, env=env,
                             timeout=60)
        if os.path.exists(spec):
            os.chmod(spec, 0o644)
        return out.stdout if plain or out.returncode else json.loads(out.stdout)


def tiers(result):
    return result if isinstance(result, str) else (result.get("tier"), result.get("spec_tier"), result.get("diff_tier"))


DECISIONS = "\n## Decisions\n- Round once over per line, because totals drift.\n"
for case, args, expected in (
        ("spec low, high-risk diff", ("Risk: low — one line", "auth"), ("high", "low", "high")),
        ("spec standard, docs-only diff", ("Risk: standard — totals", "docs"), ("standard", "standard", "low")),
        ("spec high, docs-only diff", ("Risk: high — schema", "docs"), ("high", "high", "low")),
        ("a low Risk line with Decisions", ("Risk: low — one line", "docs", DECISIONS), ("high", "high", "low")),
        ("spec equal to the diff", ("Risk: standard — totals", "code"), ("standard", "standard", "standard")),
        ("no spec", (None, "auth"), ("high", None, "high")),
        ("no Risk line", ("", "docs"), ("low", None, "low")),
        ("an invalid Risk line", ("Risk: medium — totals", "docs"), ("low", None, "low")),
        ("two Risk lines", ("Risk: high — schema\nRisk: low — docs", "docs"), ("low", None, "low")),
        ("no Risk line, with Decisions", ("", "docs", DECISIONS), ("high", "high", "low")),
        ("an invalid Risk line, with Decisions", ("Risk: medium — x", "docs", DECISIONS), ("high", "high", "low"))):
    result = triage_spec(*args)
    ok = tiers(result) == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} spec risk, {case}: (tier, spec, diff) {tiers(result)}, expected {expected}")

result = triage_spec("Risk: standard — totals", "docs")
got = (result.get("budget"), "maintainability" in result.get("lenses", {}), result.get("cross_model", "")) if isinstance(result, dict) else result
ok = isinstance(result, dict) and got[0] == "<= 20 min" and got[1] and got[2].startswith("correctness")
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk: a standard spec over a docs-only diff gets standard's budget, lenses and cross-model: {got}")
result = triage_spec("Risk: high — schema", "docs")
ok = isinstance(result, dict) and result.get("budget") == "<= 45 min" and result.get("cross_model", "").startswith("per lens")
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk: a high spec gets high's budget and a cross-model pass per lens: "
      f"{result.get('budget') if isinstance(result, dict) else result}, {result.get('cross_model') if isinstance(result, dict) else ''}")


def second_model_off(home, repo, spec, env):
    subprocess.run(["python3", os.path.join(home, ".agents", "hooks", "phase_switches.py"), "set", "off", "second-model", "branch"],
                   cwd=repo, check=True, capture_output=True, env=env)


result = triage_spec("Risk: high — schema", "docs", setup=second_model_off)
ok = isinstance(result, dict) and result.get("tier") == "high" and result.get("cross_model") == "none (second-model switched off)"
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk: the user's switches still apply to a high spec's cross-model: "
      f"{result.get('cross_model') if isinstance(result, dict) else result}")

for case, args, expected in (
        ("the spec decides", ("Risk: high — adds a schema", "docs"), ("Risk: high (", "the spec", "adds a schema")),
        ("the diff decides", ("Risk: low — one line", "auth"), ("Risk: high (", "the diff", "one line")),
        ("they agree", ("Risk: standard — totals", "code"), ("Risk: standard (", "they agree", "totals")),
        ("a low Risk line with Decisions", ("Risk: low — one line", "docs", DECISIONS),
         ("Risk: high (", "the spec", "a '## Decisions' section; its Risk line says low: one line")),
        ("an invalid Risk line", ("Risk: medium — totals", "docs"), ("Risk: low (", "no prediction", "'medium' is not a tier")),
        ("no Risk line", ("", "docs"), ("Risk: low (", "no prediction", "no 'Risk:' line")),
        ("two Risk lines", ("Risk: high — a\nRisk: low — b", "docs"), ("Risk: low (", "no prediction", "more than one")),
        ("no spec", (None, "docs"), ("Risk: low (", None))):
    out = triage_spec(*args, plain=True)
    head = "\n".join(out.splitlines()[:3])
    if expected[1] is None:  # as today: no Spec line at all
        ok = out.startswith(expected[0]) and "Spec:" not in out
    else:
        ok = out.startswith(expected[0]) and all(part in head for part in expected[1:])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} spec risk, plain output, {case}: {head!r}")


def spec_off(home, repo, spec, env):
    subprocess.run(["python3", os.path.join(home, ".agents", "hooks", "phase_switches.py"), "set", "off", "spec", "branch"],
                   cwd=repo, check=True, capture_output=True, env=env)


def unreadable(home, repo, spec, env):
    os.chmod(spec, 0)


def not_utf8(home, repo, spec, env):
    open(spec, "wb").write(SPEC_TEXT.format(risk="Risk: high — schema caf", extra="").encode().replace(b"caf", b"caf\xe9"))


def lookup_stalls(home, repo, spec, env):
    os.remove(os.path.join(home, ".agents", "bin"))
    os.makedirs(os.path.join(home, ".agents", "bin"))
    stub = os.path.join(home, ".agents", "bin", "repo-name")
    open(stub, "w").write("#!/bin/sh\nsleep 30\n")
    os.chmod(stub, 0o755)


def lookup_fails(home, repo, spec, env):
    os.remove(os.path.join(home, ".agents", "bin"))  # path.sh can't run repo-name
    os.makedirs(os.path.join(home, ".agents", "bin"))


for case, setup, expected, note in (
        ("spec switched off, a spec already written", spec_off, ("high", "high", "low"), None),
        ("an unreadable spec", unreadable, ("low", None, "low"), "no prediction"),
        ("a failing spec lookup", lookup_fails, ("low", None, "low"), "no prediction"),
        ("a spec that isn't UTF-8", not_utf8, ("low", None, "low"), "not UTF-8")):
    result = triage_spec("Risk: high — schema", "docs", setup=setup)
    out = triage_spec("Risk: high — schema", "docs", setup=setup, plain=True)
    ok = tiers(result) == expected and (note is None or note in "\n".join(out.splitlines()[:3]))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} spec risk, {case}: {tiers(result)}, expected {expected}; plain: {out.splitlines()[:3]}")

started = time.monotonic()
result = triage_spec("Risk: high — schema", "docs", setup=lookup_stalls)
took = time.monotonic() - started
ok = tiers(result) == ("low", None, "low") and "timed out" in str(result.get("spec_note")) and took < 25
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk, a stalled spec lookup gives up after 10 s: {tiers(result)}, "
      f"{result.get('spec_note')!r}, {took:.0f} s")


def commit_change(home, repo, spec, env):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change"], cwd=repo, check=True)


result = triage_spec("Risk: high — schema", "docs", setup=commit_change, args=("--range", "main..HEAD"))
ok = tiers(result) == ("low", None, "low")
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk: --range reads no spec, even with a high one on the branch: {tiers(result)}")

for line, expected in [(l, False) for l in GIT_SENSE] + [(l, True) for l in SHOP_SENSE]:
    ok = payments_signal(line) == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'payments' if expected else 'no payments'}: {line}")


def labels(result):
    return {reason.split(" (")[0] for reason in result["reasons"]}


# The lines that made B1 (#120) and B2 (#121) high: prose a script prints, and an object key.
for label, after in [("B1's prose line in a heredoc", {"review-prompt.sh": "cat <<'EOF'\n  permissions, payments, schema or migration, "
                                                       "public API or hooks, caching, scheduled jobs) or more than 400 changed\nEOF\n"}),
                     ("B2's capabilities key", {"features.js": 'export const F = {\n    "capabilities": [\n      "read"]};\n'})]:
    result = triage_change({}, after)
    ok = result["diff_tier"] == "standard" and not result["reasons"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} not high on {label}: {result['diff_tier']} {result['reasons']}")

# A whole-line comment, by the file's language, raises nothing; the same trigger as code does. (path, comment, code, label)
COMMENTS = [("auth.js", "// guards with current_user_can( first", "if (current_user_can('edit')) {}", "auth or permissions"),
            ("auth.ts", "/* current_user_can( is checked upstream */", "if (current_user_can('edit')) {}", "auth or permissions"),
            ("auth.php", " * Uses current_user_can() for the check.", "if ( current_user_can( 'edit' ) ) {}", "auth or permissions"),
            ("auth.php", "# current_user_can( was here", "if ( current_user_can( 'edit' ) ) {}", "auth or permissions"),
            ("shop.php", "// Redirect to the checkout page", "wp_safe_redirect( wc_get_checkout_url() );", "payments or orders"),
            ("shop.php", "// Render the checkout-page", "echo wc_get_checkout_url();", "payments or orders"),
            ("job.py", "# wp_schedule_event( runs it later", "wp_schedule_event(1, 'hourly', 'x')", "scheduled jobs"),
            ("job.sh", "# wp_schedule_event( nightly", "wp eval 'wp_schedule_event(1);'", "scheduled jobs"),
            ("q.sql", "-- ALTER TABLE orders later", "ALTER TABLE orders ADD x int;", "schema or data migration"),
            ("page.html", "<!-- current_user_can( -->", "<script>if (current_user_can(1)) {}</script>", "auth or permissions"),
            ("ci.yml", "# set_transient( old", "run: set_transient(1)", "caching"),
            ("ci.yml", "#[x] wp_cron nightly", "run: wp_cron", "scheduled jobs"),
            ("auth.py", "#[auth] current_user_can( here", "current_user_can(u)", "auth or permissions"),
            ("AUTH.PHP", "// current_user_can( later", "current_user_can( 'x' );", "auth or permissions"),
            ("Makefile", "# wp_cron nightly", "\twp eval 'wp_cron();'", "scheduled jobs"),
            ("q.sql", "--ALTER TABLE orders later", "ALTER TABLE orders ADD x int;", "schema or data migration"),
            ("job.lua", "--wp_schedule_event(1)", "wp_schedule_event(1)", "scheduled jobs"),
            ("job.lua", "--[[ current_user_can( ]]", "current_user_can(1)", "auth or permissions"),
            ("shop.css", "/* checkout styles */", ".checkout { color: red; }", "payments or orders"),
            ("auth.go", "// current_user_can( later", "if current_user_can(u) {}", "auth or permissions"),
            ("pay.rs", "/// process_payment( docs", "process_payment(order);", "payments or orders"),
            ("Pay.java", " * Calls process_payment( first", "process_payment(order);", "payments or orders"),
            ("pay.c", " * @param payment_id the order", "int payment_id = 0;", "payments or orders"),
            ("db.c", "/* ALTER TABLE x */", "run(\"ALTER TABLE x ADD y int\");", "schema or data migration"),
            ("pay.rb", "# process_payment( later", "process_payment(order)", "payments or orders"),
            ("wp.toml", "# wp_cron = true", "wp_cron = true", "scheduled jobs"),
            ("feed.xml", "<!-- current_user_can( -->", "<x>current_user_can(1)</x>", "auth or permissions"),
            ("cart.twig", "<!-- checkout -->", "<a href=\"{{ checkout_url }}\">", "payments or orders"),
            ("App.vue", "<!-- current_user_can( -->", "if (current_user_can(1)) {}", "auth or permissions"),
            ("cart.js", "// Render {{ checkout_url }} in the template.", "renderCheckout(root);", "payments or orders"),
            ("db.py", "# ALTER TABLE x */ then check it", "run('ALTER TABLE x ADD y int')", "schema or data migration"),
            ("pay.js", "/* process_payment(x); */ // must be idempotent", "process_payment(x);", "payments or orders")]
for path, comment, code, label in COMMENTS:
    quiet, loud = triage_change({}, {path: comment + "\n"}), triage_change({}, {path: code + "\n"})
    signalled = sorted(set(quiet["lenses"]) & {"security", "performance", "compatibility", "operability", "data-privacy"})
    ok = not quiet["reasons"] and not signalled and label in labels(loud)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {path}: a comment raises nothing ({quiet['reasons']}, lenses {signalled}), code raises {label} ({loud['reasons']})")
# A comment prefix that is code in that language stays code.
for path, code, label in [("pay.c", "#define CHARGE stripe_charge(x)", "payments or orders"),
                          ("pay.c", "*payment_total = 0;", "payments or orders"), ("pay.c", "--payment_count;", "payments or orders"),
                          ("attr.php", "#[Route('/x')] function f() { process_payment(1); }", "payments or orders"),
                          ("tool", "#!/usr/local/bin/stripe-cli", "payments or orders"),
                          ("pay.c", "*payment = value;", "payments or orders"), ("pay.c", "*payment = charge(order,", "payments or orders"),
                          ("pay.c", "* result = process_payment(order);", "payments or orders"),
                          ("gen.js", "* process_payment(order) { return charge(order); }", "payments or orders"),
                          ("pay.c", "*/ int login_count;", "auth or permissions"),
                          ("t.php", "/* translators: %s: name */ if ( current_user_can( 'x' ) ) {", "auth or permissions"),
                          ("t.php", "<!-- debug: <?php current_user_can( 'x' ); ?> -->", "auth or permissions"),
                          ("page.html", "<!-- wp:woocommerce/checkout -->", "payments or orders"),
                          ("cart.php", '<!-- wp:woocommerce/checkout {"align":"wide"} /-->', "payments or orders"),
                          ("mail.rb", '  #{process_payment(order)} was charged"', "payments or orders"),
                          ("cart.php", "<!-- <?php echo wc_get_checkout_url(); ?> -->", "payments or orders")]:
    result = triage_change({}, {path: code + "\n"})
    ok = label in labels(result)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {path}: `{code}` is code, raises {label}: {result['reasons']}")

# A bare word fires only in a code shape, not as a word in prose, a string or a key. (label, code, prose)
SHAPES = [("auth or permissions", ("auth.php", "user_has_capability( $u );"), ("msg.py", 'msg = "Needs the capability to edit"')),
          ("auth or permissions", ("auth.php", "add_action( 'wp_login', 'f' );"), ("msg.py", 'print("Lets you log in, or login later")')),
          ("payments or orders", ("pay.php", "process_payment( $order );"), ("say.sh", 'echo "payments, schema or migration"')),
          ("schema or data migration", ("db.py", "run_migration(db)"), ("cli.py", 'help = "schema or migration"')),
          ("scheduled jobs", ("job.php", "wp_cron();"), ("log.js", 'log("Cron runs nightly, see the acronym")')),
          ("auth or permissions", ("auth.py", "login(user)"), ("auth.py", "after a login (or a refresh) the token rotates")),
          ("auth or permissions", ("auth.php", "echo $capability;"), ("msg.py", 'msg = "Note:login flows"')),
          ("payments or orders", ("pay.js", "return createPayment;"), ("pay.py", "payments are retried (see below)")),
          ("schema or data migration", ("db.py", "x = [MigrationRunner]"), ("db.py", '"""Runs the migration (if any) first."""')),
          ("public API or hooks", ("api.js", "app.post('/orders', create)"), ("api.js", "const port = app.get('port');")),
          ("public API or hooks", ("api.js", r"router.get(/^\/users\/\d+$/, list);"), ("api.js", "app.get(name);")),
          ("scheduled jobs", ("nightly.yml", "  - cron: '@daily'"), ("help.js", 'const help = {"cron": "5 fields separated by spaces"};'))]
# Every identifier shape of criterion 3 fires. (label, path, code)
for label, path, code in [("payments or orders", "pay.js", "payment.status = 'paid';"),
                          ("scheduled jobs", "job.js", "cron.schedule('* * * * *', run);"),
                          ("schema or data migration", "0002.py", "migrations.RemoveField(model_name='user', name='email'),"),
                          ("auth or permissions", "form.js", "const x = loginForm;"),
                          ("payments or orders", "pay.js", "x = [PaymentIntent];"),
                          ("auth or permissions", "auth.py", "login = authenticate(user)"),
                          ("auth or permissions", "cpt.php", "'capability' => 'manage_options',"),
                          ("auth or permissions", "cpt.php", "'capabilities' => array( 'edit_post' => 'edit_book' ),"),
                          ("payments or orders", "Payment.php", "class Payment extends Model {"),
                          ("schema or data migration", "m.php", "return new class extends Migration"),
                          ("scheduled jobs", "nightly.yml", "    - cron: '0 3 * * *'"),
                          ("scheduled jobs", "Job.java", '@Scheduled(cron = "0 0 * * * *")'),
                          ("public API or hooks", "api.js", 'app.get(["/a", "/b"], list);')]:
    result = triage_change({}, {path: code + "\n"})
    ok = label in labels(result)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} `{code}` raises {label}: {result['reasons']}")
for label, (code_path, code), (prose_path, prose) in SHAPES:
    loud, quiet = triage_change({}, {code_path: code + "\n"}), triage_change({}, {prose_path: prose + "\n"})
    ok = label in labels(loud) and not quiet["reasons"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {label}: `{code}` raises it ({loud['reasons']}), `{prose}` raises nothing ({quiet['reasons']})")

# Each label still fires on its own, in WordPress and elsewhere.
for label, samples in [("auth or permissions", [("a.php", "current_user_can('x');"), ("views.py", "@login_required")]),
                       ("payments or orders", [("o.php", "$o = wc_get_order( $id );"), ("pay.py", "stripe.charges.create(amount=1)")]),
                       ("schema or data migration", [("db.php", "dbDelta( $sql );"), ("m.sql", "ALTER TABLE users ADD age int;")]),
                       ("public API or hooks", [("api.php", "register_rest_route( 'a', 'b' );"), ("app.py", '@app.route("/users")'),
                                                ("api.js", "router.get('/users', list)")]),
                       ("caching", [("c.php", "set_transient( 'k', 1 );"), ("c.py", 'cache_key = f"user:{uid}"')]),
                       ("scheduled jobs", [("j.php", "wp_schedule_event( time(), 'hourly', 'h' );"), ("j.sh", "crontab -l")])]:
    for path, code in samples:
        result = triage_change({}, {path: code + "\n"})
        ok = labels(result) == {label}
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} `{code}` raises {label} alone: {result['reasons']}")

# The other selections read the same lines: a comment selects none of them, the same trigger as code does.
for path, trigger, where, key in [("q.php", "$wpdb->get_results( $q );", "lenses", "security"),
                                  ("q.php", "$wpdb->get_results( $q );", "tests", "query budget"),
                                  ("db.php", "dbDelta( $sql );", "tests", "migration round-trip"),
                                  ("re.py", 'P = re.compile(r"(a+)+$")', "tests", "regex worst-case timing"),
                                  ("f.js", "function parsePrice(text) {}", "tests", "property-based")]:
    prefix = "# " if path.endswith(".py") else "// "
    quiet, loud = triage_change({}, {path: prefix + trigger + "\n"}), triage_change({}, {path: trigger + "\n"})
    ok = key not in quiet[where] and key in loud[where]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {key}: not selected by a comment, selected by `{trigger}`")
for prose, code in [('help = "schema or migration"', "run_migration(db)")]:
    quiet, loud = triage_change({}, {"cli.py": prose + "\n"}), triage_change({}, {"cli.py": code + "\n"})
    ok = "migration round-trip" not in quiet["tests"] and "migration round-trip" in loud["tests"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} migration round-trip: not selected by `{prose}`, selected by `{code}`")

# A long line that almost matches a code shape is scanned in linear time: quality-log gives triage 60 seconds.
for filler in ["a" * 50000, "a_" * 25000]:
    started = time.monotonic()
    result = triage_change({}, {"bundle.js": f'const payload = "{filler}";\n'})
    took = time.monotonic() - started
    ok = took < 5 and not result["reasons"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} a {len(filler)}-character line of {filler[:2]!r}… is triaged in {took:.1f}s (< 5s)")


def triage_untracked(untracked):
    """triage with no --range on a committed docs change plus untracked files ({path: text})."""
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        open(os.path.join(repo, "README.md"), "w").write("# Shop\n")
        open(os.path.join(repo, WORDPRESS_MARKER), "w").write("{}\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        open(os.path.join(repo, "README.md"), "w").write("# Shop\n\nSells things.\n")
        for path, text in untracked.items():
            open(os.path.join(repo, path), "w").write(text)
        out = subprocess.run([TRIAGE, "--json"], cwd=repo, capture_output=True, text=True, timeout=60)
        return json.loads(out.stdout)


stream = "".join('{"type": "assistant", "text": "current_user_can( and wp_cron capabilities"}\n' for _ in range(500))
result = triage_untracked({"stream.jsonl": stream, "err.log": "wp_cron failed\n"})
ok = (result["diff_tier"], result["reasons"], result["files"]) == ("low", [], 1) and result["changed_lines"] < 50
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} an untracked log leaves the triage: {result['diff_tier']}, {result['files']} file(s), "
      f"{result['changed_lines']} lines, {result['reasons']}")
result = triage_untracked({"stream.jsonl": stream, "auth.php": "if ( current_user_can( 'edit' ) ) {}\n"})
ok = result["diff_tier"] == "high" and labels(result) == {"auth or permissions"} and "auth.php" in " ".join(result["reasons"])
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} an untracked source file beside it still counts: {result['diff_tier']} {result['reasons']}")
result = triage_untracked({f"run.{ext}": "current_user_can( and wp_cron\n" for ext in ("jsonl", "ndjson", "log", "out", "err", "LOG")})
ok = (result["diff_tier"], result["reasons"], result["files"]) == ("low", [], 1)
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} every untracked data extension leaves the triage: {result['diff_tier']} {result['files']} {result['reasons']}")
result = triage_untracked({"notes.log.php": "current_user_can( 'x' );\n"})
ok = labels(result) == {"auth or permissions"}
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} an untracked source file named like a log still counts: {result['reasons']}")
result = triage_change({}, {"debug.log": "current_user_can( 'x' );\n"})
ok = labels(result) == {"auth or permissions"}
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a tracked log still counts: {result['reasons']}")

# Removing or commenting out a risk check is a risky change too: removed code lines raise the high-risk labels.
AUTH = "<?php\nif ( current_user_can( 'edit' ) ) {\n\tsave();\n}\n"
for case, before, after, label, path in [
        ("a deleted auth check", {"auth.php": AUTH}, {"auth.php": "<?php\nsave();\n"}, "auth or permissions", "auth.php"),
        ("an auth check commented out", {"auth.php": AUTH},
         {"auth.php": "<?php\n// if ( current_user_can( 'edit' ) ) {\n\tsave();\n// }\n"}, "auth or permissions", "auth.php"),
        ("a deleted file holding a scheduled job", {"cron.php": "<?php\nwp_schedule_event( time(), 'hourly', 'h' );\n", "a.php": "<?php\n"},
         {"cron.php": None}, "scheduled jobs", "cron.php"),
        ("a removed ALTER TABLE", {"m.sql": "ALTER TABLE users ADD age int;\nSELECT 1;\n"}, {"m.sql": "SELECT 1;\n"},
         "schema or data migration", "m.sql")]:
    result = triage_change(before, after)
    ok = result["diff_tier"] == "high" and labels(result) == {label} and f"{path}, removed" in " ".join(result["reasons"])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {case} raises {label}: {result['diff_tier']} {result['reasons']}")

# A deletion not committed yet counts the same, and a removed line is judged by the path it was removed from.
with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
    def git(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q", "-b", "main")
    open(os.path.join(repo, "auth.php"), "w").write(AUTH)
    open(os.path.join(repo, WORDPRESS_MARKER), "w").write("{}\n")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    open(os.path.join(repo, "auth.php"), "w").write("<?php\nsave();\n")
    result = json.loads(subprocess.run([TRIAGE, "--json"], cwd=repo, capture_output=True, text=True, timeout=60).stdout)
    os.remove(os.path.join(repo, "auth.php"))
    git("config", "diff.noprefix", "true")
    deleted = json.loads(subprocess.run([TRIAGE, "--json"], cwd=repo, capture_output=True, text=True, timeout=60).stdout)
ok = labels(result) == {"auth or permissions"}
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} an uncommitted deletion of an auth check raises auth or permissions: {result['reasons']}")
ok = labels(deleted) == {"auth or permissions"} and deleted["files"] == 1
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} an uncommitted file deletion counts, whatever diff.noprefix says: {deleted['files']} {deleted['reasons']}")
body = "".join(f"\tstep_{n}();\n" for n in range(20))
result = triage_change({"auth.php": "<?php\nif ( current_user_can( 'edit' ) ) {\n" + body + "}\n"},
                       {"auth.php": None, "docs/auth.md": "<?php\n" + body + "}\n"})
ok = labels(result) == {"auth or permissions"} and "auth.php, removed" in " ".join(result["reasons"])
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a check removed while its file moves into docs/ still counts: {result['reasons']}")

# Removed lines go through the same filters: a comment, a test or doc file, a word of prose raise nothing.
for case, before, after in [
        ("a removed comment", {"auth.php": "<?php\n// current_user_can( is checked upstream\nsave();\n"}, {"auth.php": "<?php\nsave();\n"}),
        ("a removed line in a test", {"tests/test_auth.php": "<?php\ncurrent_user_can( 'x' );\n"}, {"tests/test_auth.php": "<?php\n"}),
        ("a removed line in a doc", {"docs/auth.md": "Call current_user_can( first.\n"}, {"docs/auth.md": "Done.\n"}),
        ("a removed word of prose", {"cli.py": 'help = "schema or migration"\n'}, {"cli.py": 'help = "schema"\n'})]:
    result = triage_change(before, after)
    ok = not result["reasons"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {case} raises nothing: {result['reasons']}")

# Only the high-risk labels read removed lines: removing a query or a migration selects no lens or test of its own.
for path, trigger in [("q.php", "$wpdb->get_results( $q );"), ("q.php", "dbDelta( $sql );"), ("re.py", 'P = re.compile(r"(a+)+$")'),
                      ("f.js", "function parsePrice(text) {}")]:
    result = triage_change({path: "x = 1\n" + trigger + "\nsave();\n"}, {path: "x = 1\nsave();\n"})
    selected = sorted(set(result["lenses"]) & {"security", "performance", "compatibility", "operability", "data-privacy"})
    selected += sorted(set(result["tests"]) & {"query budget", "migration round-trip", "regex worst-case timing", "property-based"})
    ok = not selected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} removing `{trigger}` selects no lens or test: {selected}")

# An added hit comes before a removed one, so a changed line keeps the reason it had.
result = triage_change({"a.php": "<?php\ncurrent_user_can('a');\n"}, {"a.php": "<?php\ncurrent_user_can('b');\n"})
ok = result["reasons"] == ["auth or permissions (a.php: `current_user_can`)"]
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a changed line keeps its added reason: {result['reasons']}")

# What the filters left out is reported, so a tier lower than expected can be explained.
result = triage_untracked({"stream.jsonl": stream, "err.log": "x\n", "auth.php": "<?php\n// current_user_can( later\nsave();\n"})
skipped = result.get("skipped", {})
ok = (skipped.get("comment_lines"), skipped.get("untracked_data_files"), sorted(skipped.get("untracked_data_sample", []))) == (
    1, 2, ["err.log", "stream.jsonl"])
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --json reports what was skipped: {skipped}")
result = triage_untracked({f"run{n}.log": "x\n" for n in range(7)})
ok = (result["skipped"]["untracked_data_files"], len(result["skipped"]["untracked_data_sample"])) == (7, 5)
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --json names at most 5 of 7 skipped logs: {result['skipped']}")
# A removed comment counts as skipped; a comment in a test or doc file isn't one triage would have read.
result = triage_change({"auth.php": "<?php\n// old note\nsave();\n", "tests/test_a.py": "# old\nx = 1\n"},
                       {"auth.php": "<?php\nsave();\n", "tests/test_a.py": "x = 1\n", "docs/a.py": "# new\n"})
ok = result["skipped"]["comment_lines"] == 1
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a removed comment is counted, test and doc comments aren't: {result['skipped']}")


def triage_text(untracked):
    """triage's text output on a committed docs change plus untracked files ({path: text})."""
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        open(os.path.join(repo, "README.md"), "w").write("# Shop\n")
        open(os.path.join(repo, WORDPRESS_MARKER), "w").write("{}\n")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        for path, text in untracked.items():
            open(os.path.join(repo, path), "w").write(text)
        return subprocess.run([TRIAGE], cwd=repo, capture_output=True, text=True, timeout=60).stdout


many = {f"run{n}.log": "x\n" for n in range(7)}
for case, untracked, expected in [
        ("logs and a comment", {**many, "a.py": "# wp_cron later\nx = 1\n"},
         "Skipped: 1 comment line; 7 untracked data files (run0.log, run1.log, run2.log, run3.log, run4.log and 2 more)"),
        ("comments only", {"a.py": "# one\n# two\nx = 1\n"}, "Skipped: 2 comment lines"),
        ("one log only", {"run\n2026.log": "x\n"}, "Skipped: 1 untracked data file (run\\n2026.log)"),
        ("nothing skipped", {"a.py": "x = 1\n"}, None)]:
    out = triage_text(untracked)
    lines = [l for l in out.splitlines() if l.startswith("Skipped:")]
    ok = lines == ([] if expected is None else [expected])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} text output with {case}: {lines!r}")
# B3b: the WordPress signals are a pack, applied in a WordPress repo or where the repo's overlay turns it on.
def triage_packs(base, change=None, overlay=None, uncommitted=None, args=("--json", "--range", "HEAD~1..HEAD"), subdir="",
                 worktree=False, setup=None):
    """triage in a repo committed as `base`, then `change`, then `uncommitted` left in the tree ({path: text or None}),
    under a HOME whose overlay for the repo holds `overlay` as its packs file. Returns the finished process."""
    kit = os.path.realpath(os.path.expanduser("~/.agents"))
    with tempfile.TemporaryDirectory(prefix="agents-test-packs-") as home, \
            tempfile.TemporaryDirectory(prefix="agents-test-packs-") as parent:
        repo = os.path.join(parent, "plugin")
        os.makedirs(os.path.join(home, ".agents"))
        for part in ("bin", "hooks", "skills", "packs"):
            os.symlink(os.path.join(kit, part), os.path.join(home, ".agents", part))
        if overlay is not None:
            os.makedirs(os.path.join(home, ".agents", "repos", "plugin"))
            open(os.path.join(home, ".agents", "repos", "plugin", "packs"), "w").write(overlay)
        env = {**os.environ, "HOME": home}

        def git(*a, cwd=repo):
            subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)

        def write(files):
            for path, text in (files or {}).items():
                full = os.path.join(repo, path)
                if text is None:
                    os.remove(full)
                elif isinstance(text, tuple):
                    os.symlink(text[0], full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    open(full, "w").write(text)
        os.makedirs(repo)
        git("init", "-q", "-b", "main")
        write(base)
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "base")
        write(change if change is not None else {"app.php": "<?php\nsave();\n"})
        git("add", "-A")
        git("commit", "-q", "--allow-empty", "-m", "change")
        if "--range" not in args:
            git("checkout", "-q", "-b", "feature", "HEAD")
            git("branch", "-q", "-f", "main", "HEAD~1")
        write(uncommitted)
        where = repo
        if worktree:
            where = os.path.join(parent, "second")
            git("worktree", "add", "-q", "-b", "other", where, "HEAD")
        if setup:
            setup(repo)
        os.makedirs(os.path.join(where, subdir), exist_ok=True)
        out = subprocess.run([os.path.join(home, ".agents", "bin", "triage"), *args], cwd=os.path.join(where, subdir),
                             capture_output=True, text=True, env=env, timeout=60)
        if setup:
            for root, dirs, files in os.walk(repo):
                for name in dirs + files:
                    os.chmod(os.path.join(root, name), 0o755)
        return out


def packs_of(out):
    if out.returncode:
        return f"exit {out.returncode}: {out.stderr.strip()[-200:]}"
    return json.loads(out.stdout).get("packs")


# Criterion 1: no WordPress name is left in the core tables; the pack holds every one.
loader = importlib.machinery.SourceFileLoader("triage_core", TRIAGE)
core = importlib.util.module_from_spec(importlib.util.spec_from_loader("triage_core", loader))
sys.dont_write_bytecode = True
loader.exec_module(core)
core_text = " ".join([*core.HIGH_RISK.values(), *core.LENS_SIGNALS.values(), core.MIGRATION, core.HTML_COMMENT_HOLDS_CODE.pattern])
WORDPRESS_NAMES = ["current_user_can", "permission_callback", "wp_verify_nonce", "check_ajax_referer", "wc_get_order",
                   "get_total", "register_rest_route", "apply_filters", "do_action", "WP_CLI", "set_transient", "wp_cache",
                   "as_schedule", "as_enqueue", "wp_schedule_event", "wp_ajax_", "wpdb", "WP_Query", "get_posts",
                   "get_post_meta", "template_redirect", "plugins_loaded", "add_option", "update_option", "register_setting",
                   "add_filter", "add_action", "wp_delete_", "dbDelta", "wc_get_(orders|products)", "wp_remote_",
                   "posts_per_page", "get_json_params", "wp:"]
left = [name for name in WORDPRESS_NAMES if name in core_text]
source = open(TRIAGE).read()
left += [name for name in ("wpdb", "WP_Query", "get_posts", "wc_get_") if name in source]
pack_path = os.path.join(os.path.dirname(os.path.realpath(TRIAGE)), "..", "packs", "wordpress.json")
pack_text = open(pack_path).read() if os.path.exists(pack_path) else ""
missing = [name for name in WORDPRESS_NAMES if name.replace("\\", "") not in pack_text.replace("\\\\", "\\").replace("\\", "")]
ok = not left and not missing
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} the core tables hold no WordPress name ({left}) and the pack holds every one ({missing})")

# Criterion 2: a WordPress line is sized as today with the pack on, by the generic signals alone with it off; a generic line
# the same in every state. (path, line, where its WordPress signal shows: "label", "lens", "test", or "code" for markup that stops being a comment)
WORDPRESS_LINES = [("auth.php", "if ( current_user_can( 'edit' ) ) {}", "label", "auth or permissions"),
                   ("auth.php", "check_ajax_referer( 'save' );", "label", "auth or permissions"),
                   ("order.php", "$o = wc_get_order( $id );", "label", "payments or orders"),
                   ("total.php", "$t = $o->get_total();", "label", "payments or orders"),
                   ("db.php", "dbDelta( $sql );", "test", "migration round-trip"),
                   ("rest.php", "register_rest_route( 'a', 'b' );", "label", "public API or hooks"),
                   ("hook.php", "$v = apply_filters( 'x', $y );", "label", "public API or hooks"),
                   ("cli.php", "WP_CLI::add_command( 'x', 'X' );", "label", "public API or hooks"),
                   ("cache.php", "wp_cache_set( 'k', 1 );", "label", "caching"),
                   ("job.php", "as_enqueue_async_action( 'h' );", "label", "scheduled jobs"),
                   ("job.php", "wp_schedule_event( time(), 'hourly', 'h' );", "label", "scheduled jobs"),
                   ("ajax.php", "add_action( 'wp_ajax_save', 'f' );", "lens", "security"),
                   ("q.php", "$rows = $wpdb->get_results( $q );", "test", "query budget"),
                   ("meta.php", "$m = get_post_meta( 1, 'k', true );", "lens", "performance"),
                   ("init.php", "add_action( 'init', 'f' );", "lens", "performance"),
                   ("posts.php", "$p = get_posts( array() );", "test", "query budget"),
                   ("opt.php", "update_option( 'k', 1 );", "lens", "compatibility"),
                   ("remote.php", "$r = wp_remote_get( $u );", "lens", "operability"),
                   ("del.php", "wp_delete_post( 1 );", "lens", "data-privacy"),
                   ("block.html", "<!-- wp:paragraph -->", "code", "block markup")]
GENERIC_LINES = [("pay.py", "stripe.charges.create(amount=1)"), ("views.py", "@login_required"),
                 ("m.sql", "ALTER TABLE users ADD age int;"), ("api.js", "router.get('/users', list)"),
                 ("c.py", 'cache_key = f"user:{uid}"'), ("j.sh", "crontab -l"), ("x.js", "el.innerHTML = html;"),
                 ("app.py", "x = 1")]
SIZING = ("tier", "diff_tier", "reasons", "lenses", "tests", "skipped")


def sized(base, path, line, overlay=None):
    out = triage_packs(base, {path: line + "\n"}, overlay=overlay)
    return {k: json.loads(out.stdout)[k] for k in SIZING} if out.returncode == 0 else f"exit {out.returncode}: {out.stderr[-200:]}"


def shows(result, where, name):
    if isinstance(result, str):
        return False
    return {"label": name in labels(result), "lens": name in result["lenses"], "test": name in result["tests"],
            "code": not result["skipped"]["comment_lines"]}[where]


MARKED = {WORDPRESS_MARKER: "{}\n"}
for path, line, where, name in WORDPRESS_LINES:
    detected, forced_on = sized(MARKED, path, line), sized({}, path, line, overlay="wordpress\n")
    forced_off, absent = sized(MARKED, path, line, overlay="-wordpress\n"), sized({}, path, line)
    ok = (detected == forced_on and forced_off == absent and shows(detected, where, name) and not shows(absent, where, name))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} `{line}`: {where} {name} with the pack on (detected and forced), not with it off or absent"
          + ("" if ok else f": {detected} | {forced_on} | {forced_off} | {absent}"))
for path, line in GENERIC_LINES:
    states = [sized(MARKED, path, line), sized({}, path, line, overlay="wordpress\n"),
              sized(MARKED, path, line, overlay="-wordpress\n"), sized({}, path, line)]
    ok = all(state == states[0] for state in states) and not isinstance(states[0], str)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} `{line}` is sized the same in every pack state" + ("" if ok else f": {states}"))

# The query-budget test keeps today's evidence, the performance lens's first two signals; a query pattern that no lens
# names gives its own, and the patterns match WordPress's casing only.
for case, line, expected in [
        ("with the performance lens", "update_option( 'x', 1, 'autoload' ); $q = new WP_Query( $a );",
         ["q.php: `autoload`"]),
        ("without it", "$q = WP_Query::instance();", ["q.php: `WP_Query`"]),
        ("in another casing", "$q = wp_query();", None)]:
    result = sized(MARKED, "q.php", line)
    got = result["tests"].get("query budget") if isinstance(result, dict) else result
    ok = got == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} the query-budget evidence {case}: {got}")

# Criterion 3: what makes a repo WordPress, at either end of the change, read within bounds. (case, base, change, why part)
PLUGINS = {f"p{n:03}.php": "<?php\necho 1;\n" for n in range(200)}
HEADERED = {**PLUGINS, "p149.php": "<?php\n/**\n * Plugin Name: Late Starter\n */\n"}
for case, base, change, why in [
        ("wp-config.php", {"wp-config.php": "<?php\n"}, None, "wp-config.php"),
        ("wp-content/", {"wp-content/index.php": "<?php\n"}, None, "wp-content/"),
        (".wp-env.json", {".wp-env.json": "{}\n"}, None, ".wp-env.json"),
        ("a plugin header in the 150th root PHP file", HEADERED, None, "p149.php"),
        ("a plugin header, # style", {"main.php": "<?php\n# Plugin Name: Hash\n"}, None, "main.php"),
        ("a theme header", {"style.css": "/*\nTheme Name: Twenty\n*/\n"}, None, "style.css"),
        ("composer type wordpress-plugin", {"composer.json": '{"type": "wordpress-plugin"}'}, None, "composer.json"),
        ("composer type wordpress-theme", {"composer.json": '{"type": "wordpress-theme"}'}, None, "composer.json"),
        ("composer type wordpress-muplugin", {"composer.json": '{"type": "wordpress-muplugin"}'}, None, "composer.json"),
        ("composer requires johnpbloch/wordpress", {"composer.json": '{"require": {"johnpbloch/wordpress": "6"}}'}, None, "johnpbloch"),
        ("composer requires roots/wordpress", {"composer.json": '{"require": {"roots/wordpress": "6"}}'}, None, "roots/wordpress"),
        ("composer requires a wpackagist package", {"composer.json": '{"require-dev": {"wpackagist-plugin/akismet": "*"}}'}, None,
         "wpackagist-plugin/akismet"),
        ("a marker only in the start tree", {"wp-config.php": "<?php\n"}, {"wp-config.php": None, "app.php": "<?php\n"},
         "wp-config.php"),
        ("a marker the change adds", {}, {"wp-config.php": "<?php\n"}, "wp-config.php")]:
    got = packs_of(triage_packs(base, change))
    ok = isinstance(got, list) and [p["name"] for p in got] == ["wordpress"] and why in got[0]["why"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} WordPress from {case}: {got}")
for case, base in [("a PHP repo with no marker", {"index.php": "<?php echo 1;\n", "style.css": "body { color: red; }\n",
                                                  "composer.json": '{"type": "library", "require": {"monolog/monolog": "3"}}'}),
                   ("a plugin header past the first 8 KB", {"late.php": "<?php\n//" + "x" * 8200 + "\n/* Plugin Name: Late */\n"}),
                   ("a header in a PHP file below the root", {"src/plugin.php": "<?php\n/* Plugin Name: Deep */\n"}),
                   ("a theme header in a stylesheet below the root", {"css/style.css": "/* Theme Name: Deep */\n"})]:
    got = packs_of(triage_packs(base))
    ok = got == []
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} not WordPress: {case}: {got}")
# Without --range the end is the working tree, untracked files included, and the root is the top level from anywhere.
for case, kwargs in [("an untracked wp-config.php", {"base": {}, "uncommitted": {"wp-config.php": "<?php\n"}}),
                     ("a run from a subdirectory", {"base": MARKED, "uncommitted": {"app.php": "<?php\nx();\n"}, "subdir": "src/deep"})]:
    got = packs_of(triage_packs(args=("--json",), **kwargs))
    ok = isinstance(got, list) and [p["name"] for p in got] == ["wordpress"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} WordPress from {case}, no --range: {got}")
# With --range both ends are commits: a marker the end commit has and the working tree has lost still counts.
got = packs_of(triage_packs({}, {"wp-config.php": "<?php\n"}, uncommitted={"wp-config.php": None}))
ok = isinstance(got, list) and [p["name"] for p in got] == ["wordpress"]
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --range reads its end commit, not the working tree: {got}")
got = packs_of(triage_packs({}, {"app.php": "<?php\n"}, uncommitted={"wp-config.php": "<?php\n"}))
ok = got == []
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --range ignores a marker only the working tree has: {got}")
# Commits are read from the top level whatever directory triage runs in, and a marker counts whatever git stores it as.
for case, kwargs, expected in [
        ("--range from a subdirectory", {"base": MARKED, "subdir": "src"}, ["wordpress"]),
        ("a marker only in the start tree, from a subdirectory", {"base": {"wp-config.php": "<?php\n"},
         "change": {"wp-config.php": None, "src/a.php": "<?php\n"}, "args": ("--json",), "subdir": "src"}, ["wordpress"]),
        ("a PHP repo with no marker, from the subdirectory it changes", {"base": {"app/a.php": "<?php\n", "index.php": "<?php\n"},
         "change": {"app/a.php": "<?php\nx();\n"}, "subdir": "app"}, []),
        ("a wp-content symlink in the commit", {"base": {"wp-content": ("../shared/wp-content",)}}, ["wordpress"]),
        ("a dangling wp-config.php symlink, no --range", {"base": {"wp-config.php": ("../wp-config.php",)}, "args": ("--json",)},
         ["wordpress"]),
        ("a plugin header with CR line ends", {"base": {"main.php": "<?php\r/*\rPlugin Name: Example\r*/\r"}}, ["wordpress"]),
        ("a theme header with CR line ends", {"base": {"style.css": "/*\rTheme Name: Example\r*/\r"}}, ["wordpress"]),
        ("a root PHP file whose name holds a newline", {"base": {"line\nbreak.php": "<?php\n", "index.php": "<?php\n"}}, [])]:
    got = packs_of(triage_packs(**kwargs))
    ok = isinstance(got, list) and [p["name"] for p in got] == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {case}: {got}")
# A repo with no commit yet is read from its working tree alone.
with tempfile.TemporaryDirectory(prefix="agents-test-packs-") as repo:
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    open(os.path.join(repo, "a.php"), "w").write("<?php\nx();\n")
    results = []
    for marker in (None, "wp-config.php"):
        if marker:
            open(os.path.join(repo, marker), "w").write("<?php\n")
        out = subprocess.run([TRIAGE, "--json"], cwd=repo, capture_output=True, text=True, timeout=60)
        results.append(packs_of(out))
    ok = results[0] == [] and isinstance(results[1], list) and [p["name"] for p in results[1]] == ["wordpress"]
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} a repo with no commit yet, without and with a marker: {results}")
# Past 200 root PHP files triage stops reading headers and says so.
many_php = {f"p{n:03}.php": "<?php\necho 1;\n" for n in range(300)}
out = triage_packs({**many_php, "p250.php": "<?php\n/* Plugin Name: Too Late */\n"}, {"p000.php": "<?php\necho 2;\n"})
result = json.loads(out.stdout) if out.returncode == 0 else {}
ok = result.get("packs") == [] and result.get("skipped", {}).get("php_headers_unread") == 100
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} a 300-file root reads 200 headers and says it stopped: {result.get('packs')} {result.get('skipped')}")
text = triage_packs(many_php, {"p000.php": "<?php\necho 2;\n"}, args=("--range", "HEAD~1..HEAD")).stdout
ok = any(l.startswith("Skipped:") and "100 root PHP files past the first 200" in l for l in text.splitlines())
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} the text output says it too: {[l for l in text.splitlines() if l.startswith('Skipped:')]}")

# Criterion 4: the repo's overlay turns the pack on or off over detection, from any worktree; a bad line fails loudly.
for case, base, overlay, worktree, expected in [
        ("on, in a repo with no marker", {}, "wordpress\n", False, ["wordpress"]),
        ("on, from a second worktree", {}, "# WordPress, though it doesn't look it\n\nwordpress\n", True, ["wordpress"]),
        ("off, in a WordPress repo", MARKED, "-wordpress\n", False, []),
        ("off, from a second worktree", MARKED, "-wordpress\n", True, []),
        ("empty, so detection decides", MARKED, "\n", False, ["wordpress"])]:
    got = packs_of(triage_packs(base, overlay=overlay, worktree=worktree))
    ok = isinstance(got, list) and [p["name"] for p in got] == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} the overlay {case}: {got}")
for case, overlay, named in [("an unknown pack", "laravel\n", ["~/.agents/repos/plugin/packs:1", "laravel"]),
                             ("an unknown pack on a later line", "# packs\nwordpress\n-django\n", ["~/.agents/repos/plugin/packs:3", "django"]),
                             ("on and off at once", "wordpress\n-wordpress\n", ["~/.agents/repos/plugin/packs:1", ":2", "wordpress"])]:
    out = triage_packs(MARKED, overlay=overlay)
    ok = out.returncode != 0 and all(n in out.stderr for n in named)
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} the overlay with {case} fails naming it: exit {out.returncode}, {out.stderr.strip()[-160:]}")

# Criterion 5: which packs applied, and why, in both outputs.
for case, base, overlay, expected in [("detected", MARKED, None, "Packs: wordpress (.wp-env.json)"),
                                      ("forced on", {}, "wordpress\n", "Packs: wordpress (repos/plugin/packs)"),
                                      ("forced off", MARKED, "-wordpress\n", "Packs: none (wordpress turned off by repos/plugin/packs)"),
                                      ("absent", {}, None, None)]:
    text = triage_packs(base, overlay=overlay, args=("--range", "HEAD~1..HEAD")).stdout
    lines = [l for l in text.splitlines() if l.startswith("Packs:")]
    ok = lines == ([] if expected is None else [expected])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} text output, pack {case}: {lines}")
result = json.loads(triage_packs(MARKED, overlay="-wordpress\n").stdout)
ok = result.get("packs") == [] and result.get("packs_off") == [{"name": "wordpress", "why": "repos/plugin/packs"}]
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} --json names a pack turned off: {result.get('packs')} {result.get('packs_off')}")


# A pack can only add to the core's labels and lenses: one naming another fails, naming the pack and the label.
with tempfile.TemporaryDirectory(prefix="agents-test-packs-") as kit:
    os.makedirs(os.path.join(kit, "bin"))
    os.makedirs(os.path.join(kit, "packs"))
    shutil.copy(TRIAGE, os.path.join(kit, "bin", "triage"))
    os.symlink(os.path.join(os.path.dirname(os.path.realpath(TRIAGE)), "..", "hooks"), os.path.join(kit, "hooks"))
    pack = json.load(open(pack_path))
    for case, broken, named in [("an unknown label", {**pack, "high_risk": {"billing": "x"}}, "'billing'"),
                                ("an unknown lens", {**pack, "lens_signals": {"speed": "x"}}, "'speed'"),
                                ("a missing key", {k: v for k, v in pack.items() if k != "migration"}, "keys must be"),
                                ("a bad regex", {**pack, "migration": "dbDelta("}, "missing )")]:
        open(os.path.join(kit, "packs", "wordpress.json"), "w").write(json.dumps(broken))
        with tempfile.TemporaryDirectory(prefix="agents-test-packs-") as repo:
            for a in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "a"]):
                subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=repo, check=True)
            open(os.path.join(repo, "wp-config.php"), "w").write("<?php\n")
            out = subprocess.run([os.path.join(kit, "bin", "triage"), "--json"], cwd=repo, capture_output=True, text=True)
        ok = (out.returncode != 0 and named in out.stderr and "packs/wordpress.json" in out.stderr and kit not in out.stderr
              and "Traceback" not in out.stderr)
        fail |= not ok
        print(f"{'ok  ' if ok else 'FAIL'} a pack file with {case} stops triage, naming it: {out.stderr.strip()[-200:]}")

# Criterion 6: detection that can't conclude stops triage, naming why.
def drop_blob(path):
    def setup(repo):
        sha = subprocess.run(["git", "rev-parse", f"HEAD:{path}"], cwd=repo, capture_output=True, text=True).stdout.strip()
        os.remove(os.path.join(repo, ".git", "objects", sha[:2], sha[2:]))
    return setup


def lock(path):
    def setup(repo):
        os.chmod(os.path.join(repo, path), 0)
    return setup


for case, kwargs, named in [
        ("an invalid composer.json", {"base": {"composer.json": "{not json"}}, "composer.json"),
        ("a composer.json that isn't an object", {"base": {"composer.json": "[]"}}, "composer.json"),
        ("a missing range end", {"base": {}, "args": ("--json", "--range", "HEAD~1..no-such-branch")}, "no-such-branch"),
        ("a missing range start", {"base": {}, "args": ("--json", "--range", "no-such-start..HEAD")}, "no-such-start"),
        ("a missing range end, the overlay turning it on", {"base": {}, "overlay": "wordpress\n",
                                                            "args": ("--json", "--range", "HEAD~1..no-such-branch")}, "no-such-branch"),
        ("a missing range end, the overlay turning it off", {"base": {}, "overlay": "-wordpress\n",
                                                             "args": ("--json", "--range", "HEAD~1..no-such-branch")}, "no-such-branch"),
        ("a composer.json over 1 MB", {"base": {"composer.json": '{"description": "' + "x" * 1100000 + '"}'}}, "over 1 MB"),
        ("an unreadable marker file", {"base": {"style.css": "body {}\n"}, "args": ("--json",), "setup": lock("style.css")}, "style.css"),
        ("a git command that fails", {"base": {"main.php": "<?php\n"}, "setup": drop_blob("main.php")},
         "the change's end HEAD")]:
    out = triage_packs(**kwargs)
    # A tree that can't be read points to the overlay, which skips detection.
    hint = "range" in case or "repos/plugin/packs" in out.stderr
    ok = out.returncode != 0 and named in out.stderr and hint and "Traceback" not in out.stderr
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {case} stops triage, naming {named}: exit {out.returncode}, {out.stderr.strip()[-160:]}")
sys.exit(fail)
