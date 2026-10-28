#!/usr/bin/env python3
"""bin/structure-facts: the complexity and duplication facts the Maintainability reviewer gets. Each test builds a
throwaway repo; the ones that need the real lizard or jscpd skip without them, except under CI, which installs both.
Run: python3 ~/.agents/tests/test_structure_facts.py   (exit 1 on any failure)
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
FACTS = os.path.join(KIT, "bin", "structure-facts")
RESULTS = []


class Skip(Exception):
    pass


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True)


def write(repo, rel, text, executable=False):
    path = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if executable:
        os.chmod(path, 0o755)
    return path


def new_repo(base, files):
    """Base commit on main with `files`, then a feature branch."""
    repo = os.path.join(base, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feature")
    return repo


def bare_path(base):
    """A PATH with git and python3 but neither lizard nor jscpd, wherever those are installed."""
    tools = os.path.join(base, "bare-bin")
    os.makedirs(tools, exist_ok=True)
    for program in ("git", "python3", "bash", "sleep"):
        if not os.path.exists(os.path.join(tools, program)):
            os.symlink(shutil.which(program), os.path.join(tools, program))
    return tools + ":/usr/bin:/bin"


def fake(base, name, script):
    """A PATH whose `name` is this bash script, ahead of a PATH without the real tools."""
    tools = os.path.join(base, "fake-" + name)
    write(tools, name, "#!/bin/bash\n" + script, executable=True)
    return tools + ":" + bare_path(base)


def facts(repo, base="main", path=None, **env):
    env = {**os.environ, **({"PATH": path} if path else {}), **env}
    done = subprocess.run([FACTS, base], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
    return done.returncode, done.stdout + done.stderr


def need(*programs):
    missing = [p for p in programs if not shutil.which(p)]
    if missing and os.environ.get("CI"):
        raise AssertionError(f"{', '.join(missing)} not installed: CI installs them pinned, so this test must run there")
    if missing:
        raise Skip(f"{', '.join(missing)} not installed (pipx install lizard==1.24.3; npm install -g jscpd@5.4.2)")


def test(fn):
    with tempfile.TemporaryDirectory(prefix="agents-test-facts-") as base:
        try:
            fn(os.path.realpath(base))
            RESULTS.append((fn.__name__, None, None))
        except Skip as skipped:
            RESULTS.append((fn.__name__, None, str(skipped)))
        except Exception:
            RESULTS.append((fn.__name__, traceback.format_exc(), None))


def block(i, prefix="item"):
    """Five lines and well over 50 tokens, unlike every other block: a clone only of its own copy. jscpd misses a short
    clone that runs to the end of both files, so each use has a line of its own after it, which jscpd's reported range
    runs onto."""
    return (f"def {prefix}_{i}(a, b):\n"
            f"    first_{i} = alpha(a, b, {i}) + beta(a, {i}) + gamma(b, {i})\n"
            f"    second_{i} = delta(first_{i}, b) * epsilon({i}, a) - zeta(first_{i})\n"
            f"    third_{i} = eta(second_{i}, first_{i}) - theta(b, {i}) + iota(a)\n"
            f"    return kappa(third_{i}, second_{i}, first_{i}, {i}, a, b)\n")


# --- AC1: functions the change touches, at both ends ----------------------------------------------------------------

APP_BASE = """def grow(items):
    total = 0
    for item in items:
        total += item
    return total


def untouched(a):
    if a:
        return 1
    return 0


def gone(x):
    return x * 2


def split_me(rows):
    out = []
    for r in rows:
        if r:
            out.append(r)
    for r in rows:
        if not r:
            out.append(0)
    return out
"""
APP_HEAD = """def grow(items):
    total = 0
    for item in items:
        if item > 0:
            if item % 2:
                total += item
    return total


def untouched(a):
    if a:
        return 1
    return 0


def split_me(rows):
    return keep_truthy(rows) + pad_falsy(rows)


def keep_truthy(rows):
    out = []
    for r in rows:
        if r:
            out.append(r)
    return out


def pad_falsy(rows):
    return [0 for r in rows if not r]
"""
CART_BASE = """<?php
function cart_total($items) {
    $total = 0;
    foreach ($items as $item) {
        $total += $item;
    }
    return $total;
}
"""
CART_HEAD = """<?php
function cart_total($items) {
    $total = 0;
    foreach ($items as $item) {
        if ($item > 0) {
            if ($item % 2) {
                $total += $item;
            }
        }
    }
    return $total;
}
"""
UI_JS = """export function label(user) {
  if (user) {
    if (user.name) {
      return user.name;
    }
  }
  return 'guest';
}
"""
OLD_TS = """export function legacy(n: number): number {
  if (n > 1) {
    return n;
  }
  return 0;
}
"""
TOOL_BASE = "#!/usr/bin/env python3\ndef main(argv):\n    return 0\n"
TOOL_HEAD = "#!/usr/bin/env python3\ndef main(argv):\n    if argv:\n        if argv[0]:\n            return 1\n    return 0\n"


def the_change_repo(base):
    """One change of each kind: app.py, cart.php and old.ts's deletion committed, lib.py staged, tool (an extensionless
    Python script) unstaged, ui.js untracked, and a README edit no tool reads."""
    repo = new_repo(base, {"app.py": APP_BASE, "web/cart.php": CART_BASE, "web/old.ts": OLD_TS, "tool": TOOL_BASE,
                           "lib.py": "def scale(v):\n    return v * 2\n", "README.md": "# Shop\n"})
    write(repo, "app.py", APP_HEAD)
    write(repo, "web/cart.php", CART_HEAD)
    os.remove(os.path.join(repo, "web/old.ts"))
    write(repo, "README.md", "# Shop\n\nTotals.\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "change")
    write(repo, "lib.py", "def scale(v, factor):\n    return v * factor\n")
    git(repo, "add", "lib.py")
    write(repo, "tool", TOOL_HEAD, executable=True)
    write(repo, "web/ui.js", UI_JS)
    return repo


def section(out, path):
    """The lines under a file's heading in the complexity part, up to the next heading."""
    lines = out.splitlines()
    start = lines.index(path)
    rest = []
    for line in lines[start + 1:]:
        if not line.startswith("  "):
            break
        rest.append(line)
    return rest


def touched_functions_at_both_ends(base):
    need("lizard")
    repo = the_change_repo(base)
    code, out = facts(repo)
    assert code == 0, out
    # Cognitive complexity tells nesting apart where CCN doesn't: grow's CCN is 4, keep_truthy's 3, pad_falsy's 3.
    assert section(out, "app.py") == [
        "  base: 4 functions, cognitive 8 in total, 20 NLOC; largest split_me (cognitive 6, NLOC 9)",
        "  head: 5 functions, cognitive 10 in total, 21 NLOC; largest grow (cognitive 6, NLOC 7)",
        "  changed grow: cognitive 1 → 6, NLOC 5 → 7",
        "  changed split_me: cognitive 6 → 0, NLOC 9 → 2",
        "  new keep_truthy: cognitive 3, NLOC 6",
        "  new pad_falsy: cognitive 0, NLOC 2",
        "  removed gone: cognitive 0, NLOC 2",
    ], out
    assert "untouched" not in out.split("app.py", 1)[1], out
    assert section(out, "web/cart.php") == [
        "  base: 1 function, cognitive 1 in total, 7 NLOC; largest cart_total (cognitive 1, NLOC 7)",
        "  head: 1 function, cognitive 6 in total, 11 NLOC; largest cart_total (cognitive 6, NLOC 11)",
        "  changed cart_total: cognitive 1 → 6, NLOC 7 → 11",
    ], out
    assert section(out, "web/old.ts (deleted)") == [
        "  base: 1 function, cognitive 1 in total, 6 NLOC; largest legacy (cognitive 1, NLOC 6)",
        "  head: no file",
        "  removed legacy: cognitive 1, NLOC 6",
    ], out
    assert section(out, "web/ui.js (new)") == [
        "  base: no file",
        "  head: 1 function, cognitive 3 in total, 8 NLOC; largest label (cognitive 3, NLOC 8)",
        "  new label: cognitive 3, NLOC 8",
    ], out
    assert section(out, "tool") == [
        "  base: 1 function, cognitive 0 in total, 2 NLOC; largest main (cognitive 0, NLOC 2)",
        "  head: 1 function, cognitive 3 in total, 5 NLOC; largest main (cognitive 3, NLOC 5)",
        "  changed main: cognitive 0 → 3, NLOC 2 → 5",
    ], out
    # A new parameter is the same function, not one removed and one new.
    assert section(out, "lib.py") == [
        "  base: 1 function, cognitive 0 in total, 2 NLOC; largest scale (cognitive 0, NLOC 2)",
        "  head: 1 function, cognitive 0 in total, 2 NLOC; largest scale (cognitive 0, NLOC 2)",
        "  changed scale: cognitive 0 → 0, NLOC 2 → 2",
    ], out
    assert "not measured: a language neither tool reads: README.md" in out, out
    assert re.search(r"^structure-facts: 7 changed files against main \(merge-base [0-9a-f]{7,}\)", out, re.M), out


def a_renamed_file_is_compared_with_its_old_path(base):
    need("lizard")
    repo = new_repo(base, {"old_name.py": APP_BASE})
    git(repo, "mv", "old_name.py", "new_name.py")
    write(repo, "new_name.py", APP_BASE.replace("        total += item\n", "        if item:\n            total += item\n"))
    code, out = facts(repo)
    assert code == 0, out
    assert section(out, "new_name.py (was old_name.py)")[2:] == ["  changed grow: cognitive 1 → 3, NLOC 5 → 6"], out


# --- AC2: new clones ------------------------------------------------------------------------------------------------

def a_block_copied_into_a_new_file_is_listed(base):
    need("jscpd")
    repo = new_repo(base, {"src/orig.py": "import os\n\n\n" + block(1) + "ORIG = 1\n"})
    write(repo, "src/copied.py", "x = 0\n" + block(1) + "COPIED = 2\n")
    code, out = facts(repo)
    assert code == 0, out
    clones = out.split("New clones", 1)[1].splitlines()
    assert "  src/copied.py:2-7 ↔ src/orig.py:4-9 (6 lines)" in clones, out
    assert "token mode, at least 5 lines and 50 tokens" in out and "jscpd config" in out, out


def a_clone_already_there_is_not_listed(base):
    need("jscpd")
    repo = new_repo(base, {"a.py": block(1) + "\n\ndef keep():\n    return 1\n", "b.py": "y = 2\n" + block(1) + "B = 1\n"})
    write(repo, "a.py", block(1) + "\n\ndef keep():\n    return 2\n\n\ndef more():\n    return 3\n")
    code, out = facts(repo)
    assert code == 0, out
    assert out.split("New clones", 1)[1].splitlines()[1:] == ["  none"], out


def at_most_fifty_clones_are_listed(base):
    need("jscpd")
    source = "".join(f"marker_src_{i} = {i}\n" + block(i) for i in range(60)) + "SRC = 1\n"
    repo = new_repo(base, {"src.py": source})
    # Reversed and kept apart, so jscpd can't merge neighbours into one long clone.
    write(repo, "copy.py", "".join(f"marker_copy_{i} = '{i}'\n" + block(i) for i in reversed(range(60))) + "COPY = 2\n")
    code, out = facts(repo)
    assert code == 0, out
    clones = [line for line in out.split("New clones", 1)[1].splitlines() if line.startswith("  copy.py:")]
    assert len(clones) == 50 and "  and 10 more" in out, out


def jscpd_config_in_the_repo_changes_nothing(base):
    need("jscpd")
    repo = new_repo(base, {"src/orig.py": block(1) + "ORIG = 1\n", ".jscpd.json": '{"ignore": ["**/*"], "minLines": 1000}\n',
                           "package.json": '{"jscpd": {"ignore": ["**/*"], "minLines": 1000}}\n'})
    write(repo, "src/copied.py", block(1) + "COPIED = 2\n")
    code, out = facts(repo)
    assert code == 0, out
    assert "  src/copied.py:1-6 ↔ src/orig.py:1-6 (6 lines)" in out, out


# --- AC3: it never fails the review ---------------------------------------------------------------------------------

def missing_tools_are_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    code, out = facts(repo, path=bare_path(base))
    assert code == 0, out
    assert "not measured: complexity: lizard is not installed" in out, out
    assert "not measured: new clones: jscpd is not installed" in out, out


def lizard_failing_without_rows_is_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    code, out = facts(repo, path=fake(base, "lizard", "echo 'boom' >&2\nexit 1\n"))
    assert code == 0, out
    assert "not measured: complexity: lizard exited 1 and printed no rows" in out and "boom" in out, out


def lizard_failing_with_rows_is_measured(base):
    repo = new_repo(base, {"keep.txt": "x\n"})
    write(repo, "a.py", "def f():\n    return 1\n")
    rows = 'for f in "$@"; do case "$f" in -*) ;; *) echo "3,2,20,1,3,\\"f@1-3@$f\\",\\"$f\\",\\"f\\",\\"f( )\\",1,3,7";; esac; done\nexit 1\n'
    code, out = facts(repo, path=fake(base, "lizard", rows))
    assert code == 0, out
    assert "  new f: cognitive 7, NLOC 3" in section(out, "a.py (new)"), out


def lizard_printing_garbage_is_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    code, out = facts(repo, path=fake(base, "lizard", "echo 'not,a,row'\n"))
    assert code == 0, out
    assert "not measured: complexity: lizard printed nothing parsable" in out, out


def a_function_over_lizards_warning_limit_is_measured(base):
    need("lizard")
    branches = "".join(f"    if x == {i}:\n        return {i}\n" for i in range(20))
    repo = new_repo(base, {"keep.txt": "x\n"})
    write(repo, "busy.py", "def busy(x):\n" + branches + "    return -1\n")
    code, out = facts(repo)
    assert code == 0, out
    assert "  new busy: cognitive 20, NLOC 42" in section(out, "busy.py (new)"), out


def jscpd_timing_out_is_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    started = time.monotonic()
    code, out = facts(repo, path=fake(base, "jscpd", "sleep 30\n"), STRUCTURE_FACTS_JSCPD_TIMEOUT="1")
    assert code == 0 and time.monotonic() - started < 20, out
    assert "not measured: new clones: jscpd timed out after 1 s" in out, out


def lizard_timing_out_is_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    code, out = facts(repo, path=fake(base, "lizard", "sleep 30\n"), STRUCTURE_FACTS_LIZARD_TIMEOUT="1")
    assert code == 0, out
    assert "not measured: complexity: lizard timed out after 1 s" in out, out


def jscpd_without_a_report_is_not_measured(base):
    repo = new_repo(base, {"a.py": "def f():\n    return 1\n"})
    write(repo, "a.py", "def f():\n    return 2\n")
    code, out = facts(repo, path=fake(base, "jscpd", "echo 'jscpd broke' >&2\nexit 3\n"))
    assert code == 0, out
    assert "not measured: new clones: jscpd wrote no readable report" in out and "jscpd broke" in out, out


def a_file_it_cannot_read_is_not_measured(base):
    repo = new_repo(base, {"keep.txt": "x\n"})
    secret = write(repo, "secret.py", "def f():\n    return 1\n")
    os.chmod(secret, 0)
    try:
        code, out = facts(repo, path=bare_path(base))
    finally:
        os.chmod(secret, 0o644)
    assert code == 0, out
    assert "not measured: secret.py: can't read it at the head" in out, out


def a_change_no_tool_reads_is_not_measured(base):
    repo = new_repo(base, {"README.md": "# x\n"})
    write(repo, "README.md", "# y\n")
    code, out = facts(repo, path=bare_path(base))
    assert code == 0, out
    assert "not measured: a language neither tool reads: README.md" in out, out


def a_bad_base_exits_2(base):
    repo = new_repo(base, {"a.py": "x = 1\n"})
    code, out = facts(repo, base="no-such-branch")
    assert code == 2 and "no-such-branch" in out, out


def outside_a_checkout_exits_2(base):
    plain = os.path.join(base, "plain")
    os.makedirs(plain)
    code, out = facts(plain)
    assert code == 2 and "git" in out, out


# --- AC4: bounded work ----------------------------------------------------------------------------------------------

def changed_files_past_the_cap_are_named(base):
    repo = new_repo(base, {"keep.txt": "x\n"})
    for name in ("a.py", "b.py", "c.py"):
        write(repo, name, "def f():\n    return 1\n")
    code, out = facts(repo, path=bare_path(base), STRUCTURE_FACTS_MAX_FILES="2")
    assert code == 0, out
    assert "not measured: 1 more changed file, past the cap of 2: c.py" in out, out


def a_file_over_the_size_cap_is_skipped(base):
    repo = new_repo(base, {"keep.txt": "x\n"})
    write(repo, "big.py", "x = 1\n" * 50)
    code, out = facts(repo, path=bare_path(base), STRUCTURE_FACTS_MAX_BYTES="100")
    assert code == 0, out
    assert "not measured: big.py: larger than 100 bytes" in out, out


def caps_are_read_only_under_the_test_flag(base):
    repo = new_repo(base, {"keep.txt": "x\n"})
    for name in ("a.py", "b.py", "c.py"):
        write(repo, name, "def f():\n    return 1\n")
    env = {k: v for k, v in os.environ.items() if k != "AGENTS_KIT_UNDER_TEST"}
    done = subprocess.run([FACTS, "main"], cwd=repo, capture_output=True, text=True,
                          env={**env, "PATH": bare_path(base), "STRUCTURE_FACTS_MAX_FILES": "2"})
    assert done.returncode == 0 and "past the cap" not in done.stdout, done.stdout + done.stderr


def a_clone_past_the_comparison_cut_is_not_listed(base):
    need("jscpd")
    repo = new_repo(base, {"a1.py": "x = 1\n", "a2.py": "x = 2\n", "zz_orig.py": block(1) + "ORIG = 1\n"})
    write(repo, "new.py", block(1) + "NEW = 2\n")
    code, out = facts(repo, STRUCTURE_FACTS_MAX_COMPARED="3")
    assert code == 0, out
    assert "zz_orig.py" not in out, out
    assert "compared against 3 of 4 files" in out, out


# --- AC5: the review uses it ----------------------------------------------------------------------------------------

def bundle(home, repo, lens):
    return subprocess.run([os.path.join(home, ".agents", "skills", "kit-review", "bundle.sh"), "main", lens, "Double x"],
                          cwd=repo, capture_output=True, text=True, env={**os.environ, "HOME": home})


def only_the_maintainability_bundle_holds_the_facts(base):
    home = os.path.join(base, "home")
    os.makedirs(os.path.join(home, ".agents", "repos"))
    for entry in os.listdir(KIT):
        if entry != "repos":
            os.symlink(os.path.join(KIT, entry), os.path.join(home, ".agents", entry))
    repo = new_repo(base, {"app.py": "def f():\n    return 1\n"})
    write(repo, "app.py", "def f():\n    return 2\n")
    maintainability = bundle(home, repo, "Maintainability")
    correctness = bundle(home, repo, "Correctness")
    assert maintainability.returncode == 0 and correctness.returncode == 0, maintainability.stderr + correctness.stderr
    assert "structure-facts: 1 changed file against main" in maintainability.stdout, maintainability.stdout
    facts_at = maintainability.stdout.index("structure-facts: 1 changed file")
    assert facts_at < maintainability.stdout.index("```diff"), maintainability.stdout
    assert "structure-facts:" not in correctness.stdout, correctness.stdout


def the_skill_and_the_brief_use_the_facts(base):
    skill = open(os.path.join(KIT, "skills", "kit-review", "SKILL.md")).read()
    lenses = open(os.path.join(KIT, "skills", "kit-review", "lenses.md")).read()
    maintainability = re.search(r"^## Maintainability\n(.*?)(?=^## )", lenses, re.M | re.S).group(1)
    step2 = re.search(r"^## 2\. Run the lenses\n(.*?)(?=^## 3)", skill, re.M | re.S).group(1)
    assert "~/.agents/bin/structure-facts <base>" in step2 and "not measured" in step2, step2
    assert "structure-facts" in maintainability and "threshold" in maintainability, maintainability
    assert "function count" in maintainability and "reuse" in maintainability, maintainability


for t in [touched_functions_at_both_ends, a_renamed_file_is_compared_with_its_old_path,
          a_block_copied_into_a_new_file_is_listed, a_clone_already_there_is_not_listed, at_most_fifty_clones_are_listed,
          jscpd_config_in_the_repo_changes_nothing, missing_tools_are_not_measured,
          lizard_failing_without_rows_is_not_measured, lizard_failing_with_rows_is_measured,
          lizard_printing_garbage_is_not_measured, a_function_over_lizards_warning_limit_is_measured,
          jscpd_timing_out_is_not_measured, lizard_timing_out_is_not_measured, jscpd_without_a_report_is_not_measured,
          a_file_it_cannot_read_is_not_measured, a_change_no_tool_reads_is_not_measured, a_bad_base_exits_2,
          outside_a_checkout_exits_2, changed_files_past_the_cap_are_named, a_file_over_the_size_cap_is_skipped,
          caps_are_read_only_under_the_test_flag, a_clone_past_the_comparison_cut_is_not_listed,
          only_the_maintainability_bundle_holds_the_facts, the_skill_and_the_brief_use_the_facts]:
    test(t)

failed = [(n, e) for n, e, _ in RESULTS if e]
for name, err, skipped in RESULTS:
    if skipped:
        print(f"skip {name}: {skipped}")
        continue
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
