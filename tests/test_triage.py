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

TRIAGE = os.path.expanduser("~/.agents/bin/triage")
GIT_SENSE = ["cwd = pr_checkout(command, cwd)", "git checkout -b fix", "- uses: actions/checkout@v4",
             "# the main checkout serves the stack", "for path in known_checkouts():", "git_checkout(command, cwd)",
             "def pr_checkout_unknown(command, payload):", "repo = phase_switches.checkout(os.getcwd())[0]",
             "def checkout(cwd, timeout=5):", "in_checkout(root, review_stamp.fingerprint)",
             "check_checkout(root, session, payload)", "const head = await checkout($, cwd);"]
SHOP_SENSE = ["return wc_get_checkout_url();", "'wc-blocks-checkout',", "if ( is_checkout() ) {",
              "// Redirect to the checkout page", "git checkout-free refund( $order );", "// Render the checkout-page",
              "function remember_checkout_details() { return get_checkout_url(); }", "process_checkout(cart, cwd)",
              "renderCheckout(root);", "goToCheckout(path)", "$this->checkout(path)", "router.push(checkout(path))",
              "new Checkout(root, opts)", "cart.checkout(root)", "self.checkout(cwd, order)", "return checkout(root, cart);",
              "Cart::checkout(cwd)", "checkout(cwd.order)"]


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
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    open(full, "w").write(text)
        git("init", "-q", "-b", "main")
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


def triage_spec(risk, diff, extra="", setup=None, plain=False):
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
        out = subprocess.run([TRIAGE] + ([] if plain else ["--json"]), cwd=repo, capture_output=True, text=True, env=env,
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
        ("two Risk lines", ("Risk: high — schema\nRisk: low — docs", "docs"), ("low", None, "low"))):
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
        ("an invalid Risk line", ("Risk: medium — totals", "docs"), ("Risk: low (", "no prediction", "'medium' is not a tier"))):
    out = triage_spec(*args, plain=True)
    head = "\n".join(out.splitlines()[:3])
    ok = out.startswith(expected[0]) and all(part in head for part in expected[1:])
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} spec risk, plain output, {case}: {head!r}")


def spec_off(home, repo, spec, env):
    subprocess.run(["python3", os.path.join(home, ".agents", "hooks", "phase_switches.py"), "set", "off", "spec", "branch"],
                   cwd=repo, check=True, capture_output=True, env=env)


def unreadable(home, repo, spec, env):
    os.chmod(spec, 0)


def lookup_fails(home, repo, spec, env):
    os.remove(os.path.join(home, ".agents", "bin"))  # path.sh can't run repo-name
    os.makedirs(os.path.join(home, ".agents", "bin"))


for case, setup, expected, note in (
        ("spec switched off, a spec already written", spec_off, ("high", "high", "low"), None),
        ("an unreadable spec", unreadable, ("low", None, "low"), "no prediction"),
        ("a failing spec lookup", lookup_fails, ("low", None, "low"), "no prediction")):
    result = triage_spec("Risk: high — schema", "docs", setup=setup)
    out = triage_spec("Risk: high — schema", "docs", setup=setup, plain=True)
    ok = tiers(result) == expected and (note is None or note in "\n".join(out.splitlines()[:3]))
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} spec risk, {case}: {tiers(result)}, expected {expected}; plain: {out.splitlines()[:3]}")

result = triage_change({"app.py": "x = 1\n"}, {"app.py": "if current_user_can('edit'):\n    x = 2\n"})
ok = (result.get("tier"), result.get("spec_tier"), result.get("diff_tier")) == ("high", None, "high")
fail |= not ok
print(f"{'ok  ' if ok else 'FAIL'} spec risk: --range reads no spec: {tiers(result)}")

for line, expected in [(l, False) for l in GIT_SENSE] + [(l, True) for l in SHOP_SENSE]:
    ok = payments_signal(line) == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'payments' if expected else 'no payments'}: {line}")
sys.exit(fail)
