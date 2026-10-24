#!/usr/bin/env python3
"""Tests for repos/_shared/verify_auto.py. Each test builds a throwaway git repo with fake project-local
tools (node_modules/.bin, .venv/bin, vendor/bin), so results don't depend on what is installed here.

Run: python3 ~/.agents/tests/test_verify_auto.py   (exit 1 on any failure)
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

SCRIPT = os.path.expanduser("~/.agents/repos/_shared/verify_auto.py")
MINIMAL_PATH = "/usr/bin:/bin"  # git, but none of the linters or test runners
RESULTS = []


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
    """Base commit on trunk with `files`, then a feature branch; the test's edits stay uncommitted."""
    repo = os.path.join(base, "repo")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "trunk")
    write(repo, ".gitignore", "node_modules/\nvendor/\n.venv/\n")
    for rel, text in files.items():
        write(repo, rel, text)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feature")
    return repo


def verify(repo, path=None, red_check_timeout=None, timeouts=None, **env):
    """`timeouts` lowers verify_auto's timeout constants by name, e.g. {"LINT_TIMEOUT": 1}."""
    env = {**os.environ, **({"PATH": path} if path else {}), **env}
    args = [sys.executable, SCRIPT]
    timeouts = {**(timeouts or {}), **({"RED_CHECK_TIMEOUT": red_check_timeout} if red_check_timeout else {})}
    if timeouts:
        lowered = ("import importlib.util, sys\n"
                   f"spec = importlib.util.spec_from_file_location('verify_auto', {SCRIPT!r})\n"
                   "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                   + "".join(f"module.{name} = {value}\n" for name, value in timeouts.items())
                   + "sys.exit(module.main())")
        args = [sys.executable, "-c", lowered]
    proc = subprocess.run(args, cwd=repo, capture_output=True, text=True, env=env, timeout=120)
    return proc.returncode, proc.stdout + proc.stderr


def lines(out, prefix):
    return [l for l in out.splitlines() if l.startswith(prefix)]


def test(fn):
    base = tempfile.mkdtemp(prefix="agents-verify-auto-")
    try:
        fn(base)
        RESULTS.append((fn.__name__, None))
    except Exception:  # noqa: BLE001 - report every failure, keep going
        RESULTS.append((fn.__name__, traceback.format_exc(limit=2)))
    finally:
        shutil.rmtree(base, ignore_errors=True)


# --- tests ------------------------------------------------------------------------------------
def no_changes_is_nothing_to_check(base):
    repo = new_repo(base, {"app.py": "x = 1\n"})
    code, out = verify(repo)
    assert code == 0 and "ran: nothing to check: no changed files" in out, out


def unsupported_files_only(base):
    repo = new_repo(base, {"README.md": "a\n"})
    write(repo, "README.md", "a\nb\n")
    code, out = verify(repo)
    assert code == 0 and "ran: nothing to check: no supported file types" in out, out


def eslint_reports_only_changed_lines(base):
    repo = new_repo(base, {"package.json": "{}\n", "src/app.js": "a;\nb;\nc;\n"})
    write(repo, "src/app.js", "a;\nchanged;\nc;\n")
    app = os.path.realpath(os.path.join(repo, "src/app.js"))
    write(repo, "node_modules/.bin/eslint", f"""#!/bin/sh
cat <<'EOF'
[{{"filePath": "{app}", "messages": [
  {{"line": 1, "severity": 2, "ruleId": "old-rule", "message": "old problem"}},
  {{"line": 2, "severity": 2, "ruleId": "no-undef", "message": "new problem"}},
  {{"line": 2, "severity": 1, "ruleId": "semi", "message": "new warning"}}]}}]
EOF
exit 1
""", executable=True)
    code, out = verify(repo)
    assert code == 1, out
    assert lines(out, "error: ") == ["error: src/app.js:2 [eslint no-undef] new problem"], out
    assert lines(out, "warning: ") == ["warning: src/app.js:2 [eslint semi] new warning"], out
    assert "ran: eslint on 1 changed files" in out, out
    assert "skipped: JS related tests (neither vitest nor jest" in out, out


def eslint_missing_is_skipped(base):
    repo = new_repo(base, {"package.json": "{}\n", "app.ts": "a;\n"})
    write(repo, "app.ts", "b;\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0, out
    assert "skipped: eslint (eslint not installed in node_modules/.bin or PATH)" in out, out
    assert not lines(out, "error: "), out


def vitest_failure_is_error(base):
    repo = new_repo(base, {"package.json": '{"devDependencies": {"vitest": "^2"}}\n', "src/sum.js": "a;\n"})
    write(repo, "src/sum.js", "b;\n")
    write(repo, "node_modules/.bin/vitest", """#!/bin/sh
echo "$@" > "$(dirname "$0")/args"
echo " FAIL  src/sum.test.js > sum > adds"
exit 1
""", executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1, out
    assert "error: [vitest] FAIL  src/sum.test.js > sum > adds" in out, out
    args = open(os.path.join(repo, "node_modules/.bin/args")).read().split()
    assert args[:2] == ["related", "--run"] and "src/sum.js" in args, args


def vitest_dependency_not_installed_is_skipped(base):
    repo = new_repo(base, {"package.json": '{"devDependencies": {"vitest": "^2"}}\n', "src/sum.js": "a;\n"})
    write(repo, "src/sum.js", "b;\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and "skipped: vitest related tests (vitest not installed in node_modules/.bin)" in out, out


def ruff_reports_only_changed_lines(base):
    repo = new_repo(base, {"pyproject.toml": "", "pkg/mod.py": "a = 1\nb = 2\n"})
    write(repo, "pkg/mod.py", "a = 1\nb = 3\n")
    mod = os.path.realpath(os.path.join(repo, "pkg/mod.py"))
    write(repo, ".venv/bin/ruff", f"""#!/bin/sh
cat <<'EOF'
[{{"filename": "{mod}", "location": {{"row": 1, "column": 1}}, "code": "F401", "message": "old"}},
 {{"filename": "{mod}", "location": {{"row": 2, "column": 1}}, "code": "F821", "message": "undefined name"}}]
EOF
exit 1
""", executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1, out
    assert lines(out, "error: ") == ["error: pkg/mod.py:2 [ruff F821] undefined name"], out
    assert "skipped: pytest (no related test_<module>.py" in out, out


def committed_branch_work_is_checked(base):
    repo = new_repo(base, {"pyproject.toml": "", "pkg/mod.py": "a = 1\n"})
    write(repo, "pkg/mod.py", "a = 1\nb = c\n")
    git(repo, "commit", "-qam", "work")
    mod = os.path.realpath(os.path.join(repo, "pkg/mod.py"))
    write(repo, ".venv/bin/ruff", f"""#!/bin/sh
echo '[{{"filename": "{mod}", "location": {{"row": 2, "column": 5}}, "code": "F821", "message": "undefined name"}}]'
exit 1
""", executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1 and "error: pkg/mod.py:2 [ruff F821] undefined name" in out, out


def pytest_runs_related_tests(base):
    repo = new_repo(base, {"pkg/mod.py": "a = 1\n", "tests/test_mod.py": "def test_a(): pass\n",
                           "tests/test_other.py": "def test_b(): pass\n"})
    write(repo, "pkg/mod.py", "a = 2\n")
    write(repo, ".venv/bin/pytest", """#!/bin/sh
echo "$@" > "$(dirname "$0")/args"
echo "FAILED tests/test_mod.py::test_a - assert 2 == 1"
exit 1
""", executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1 and "error: [pytest] FAILED tests/test_mod.py::test_a - assert 2 == 1" in out, out
    assert "skipped: ruff (ruff not installed in .venv/bin or venv/bin or PATH)" in out, out
    assert open(os.path.join(repo, ".venv/bin/args")).read().split() == ["-q", "tests/test_mod.py"]
    assert [l for l in lines(out, "skipped: diff coverage (") if "#!/bin/sh" in l], \
        "a shell launcher with no venv python beside it leaves the Python unknown:\n" + out


def php_lint_catches_syntax_error(base):
    if not shutil.which("php"):
        raise AssertionError("php not installed; this test needs a real php binary")
    repo = new_repo(base, {"composer.json": "{}\n", "src/Good.php": "<?php\n$a = 1;\n"})
    write(repo, "src/Good.php", "<?php\n$a = 2;\n")
    write(repo, "src/Bad.php", "<?php\nfunction (\n")
    code, out = verify(repo)
    assert code == 1, out
    errors = lines(out, "error: ")
    assert len(errors) == 1 and errors[0].startswith("error: src/Bad.php [php -l]") and "Parse error" in errors[0], out
    assert "skipped: phpcs (phpcs not installed in vendor/bin)" in out, out


def phpunit_runs_related_test_and_reports_failures(base):
    repo = new_repo(base, {"composer.json": "{}\n", "src/Foo.php": "<?php\n", "tests/unit/FooTest.php": "<?php\n",
                           "tests/unit/BarTest.php": "<?php\n"})
    write(repo, "src/Foo.php", "<?php\n$a = 1;\n")
    write(repo, "vendor/bin/phpunit", """#!/bin/sh
echo "$@" > "$(dirname "$0")/args"
while [ $# -gt 0 ]; do [ "$1" = "--log-junit" ] && junit="$2"; shift; done
cat > "$junit" <<'EOF'
<testsuites><testsuite><testcase class="FooTest" name="test_a"><failure>boom</failure></testcase>
<testcase class="FooTest" name="test_b"/></testsuite></testsuites>
EOF
exit 1
""", executable=True)
    code, out = verify(repo)
    assert code == 1 and "error: [phpunit] FooTest::test_a fails" in out, out
    assert "ran: phpunit --filter FooTest: 2 tests, 1 failing" in out, out
    assert open(os.path.join(repo, "vendor/bin/args")).read().split()[:2] == ["--filter", "/(FooTest)/"]


def phpunit_that_cannot_start_is_skipped(base):
    repo = new_repo(base, {"src/Foo.php": "<?php\n", "tests/FooTest.php": "<?php\n"})
    write(repo, "src/Foo.php", "<?php\n$a = 1;\n")
    write(repo, "vendor/bin/phpunit", "#!/bin/sh\necho 'Error establishing a database connection'\nexit 1\n",
          executable=True)
    code, out = verify(repo)
    assert code == 0, out
    assert "skipped: phpunit (no JUnit report, PHPUnit did not start" in out and "database connection" in out, out


def shell_and_go_without_tools_are_skipped(base):
    repo = new_repo(base, {"run.sh": "echo a\n", "main.go": "package main\n"})
    write(repo, "run.sh", "echo b\n")
    write(repo, "main.go", "package main\n\nfunc main() {}\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0, out
    assert "skipped: shellcheck (shellcheck not installed in PATH)" in out, out
    assert "skipped: go (no go.mod at the repo root)" in out, out


# --- red check: the changed tests run on a pristine merge-base copy, where at least one must fail ----------------
# The fakes tell the base from the change by reading the source file in their cwd: only the change says "fixed".
FAKE_NPM = """#!/bin/sh
echo "$(pwd -P) $* CI=$CI" >> "$(dirname "$0")/calls"
[ -f src/sum.test.js ] || {{ echo "src/sum.test.js: no such file"; exit 1; }}
if grep -q fixed src/sum.js; then echo "  1 passing"; exit 0; fi
{base}
"""
MOCHA_FAILING = 'echo "  1 failing"; exit 1'
JS_BASE = {"package.json": '{"scripts": {"test": "mocha"}}\n', "src/sum.js": "a;\n"}


def js_change(base, files=JS_BASE, on_base=MOCHA_FAILING):
    """A change to src/sum.js with a new src/sum.test.js; PATH has a fake npm and no vitest or jest."""
    repo = new_repo(base, files)
    write(repo, "src/sum.js", "fixed;\n")
    write(repo, "src/sum.test.js", "test;\n")
    write(base, "bin/npm", FAKE_NPM.format(base=on_base), executable=True)
    return repo, f"{base}/bin:{MINIMAL_PATH}"


def npm_calls(base):
    path = os.path.join(base, "bin/calls")
    return open(path).read().splitlines() if os.path.exists(path) else []


def npm_test_red_check_runs_changed_tests_on_the_base(base):
    repo, path = js_change(base)
    write(repo, "node_modules/lodash/index.js", "x;\n")  # an ordinary installed package is a directory, not a link
    code, out = verify(repo, path)
    assert code == 0, out
    assert "skipped: JS related tests (neither vitest nor jest" in out, out
    assert [l for l in lines(out, "ran: red check: ") if "npm test" in l and "src/sum.test.js" in l], out
    calls = npm_calls(base)
    assert len(calls) == 2, calls
    assert calls[0] == f"{os.path.realpath(repo)} test -- src/sum.test.js CI=true", calls
    copy = calls[1].split()[0]
    assert "agents-red-" in copy and calls[1].endswith(" test -- src/sum.test.js CI=true"), calls
    assert not os.path.exists(copy), "the pristine copy was left behind"


def npm_test_red_check_errors_when_tests_pass_on_the_base(base):
    repo, path = js_change(base, on_base='echo "  1 passing"; exit 0')
    code, out = verify(repo, path)
    assert code == 1, out
    errors = [l for l in lines(out, "error: ") if "[red check]" in l]
    assert len(errors) == 1 and "src/sum.test.js" in errors[0] and "pass without your change" in errors[0], out
    assert not lines(out, "ran: red check"), out


def npm_test_red_check_without_failure_summary_is_skipped(base):
    repo, path = js_change(base, on_base="echo 'Error: Cannot find module mocha'; exit 1")
    code, out = verify(repo, path)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "no test failure summary" in l], out
    assert not lines(out, "ran: red check") and not lines(out, "error: "), out


def npm_placeholder_or_missing_test_script_is_skipped(base):
    placeholder = '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}\n'
    repo, path = js_change(base, {**JS_BASE, "package.json": placeholder})
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "no test script" in l], out
    write(repo, "package.json", "{}\n")
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "no test script" in l], out
    assert npm_calls(base) == [], npm_calls(base)


def npm_test_red_check_hanging_runner_is_skipped(base):
    repo, path = js_change(base, on_base="exec sleep 30")
    code, out = verify(repo, path, red_check_timeout=1)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "timed out after 1s" in l], out
    calls = npm_calls(base)
    assert len(calls) == 2 and "agents-red-" in calls[1], calls
    assert not os.path.exists(calls[1].split()[0]), "the pristine copy was left behind after the timeout"


def npm_test_red_check_skips_workspace_links(base):
    repo, path = js_change(base, {**JS_BASE, "packages/lib/index.js": "lib;\n"})
    os.makedirs(os.path.join(repo, "node_modules/@acme"))
    os.symlink("../../packages/lib", os.path.join(repo, "node_modules/@acme/lib"))
    code, out = verify(repo, path)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "@acme/lib" in l], out
    assert not [c for c in npm_calls(base) if "agents-red-" in c], npm_calls(base)


def npm_test_red_check_skips_tests_failing_in_the_working_tree(base):
    repo, path = js_change(base)
    write(base, "bin/npm", '#!/bin/sh\necho "$*" >> "$(dirname "$0")/calls"\necho "  1 failing"; exit 1\n',
          executable=True)
    code, out = verify(repo, path)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "working tree" in l], out
    assert len(npm_calls(base)) == 1, npm_calls(base)


def npm_test_red_check_without_npm_is_skipped(base):
    repo, _ = js_change(base)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "npm not on PATH" in l], out


def npm_test_red_check_follows_links_that_stay_in_node_modules(base):
    repo, path = js_change(base, {**JS_BASE, "packages/app/index.js": "app;\n"})
    write(repo, "node_modules/.pnpm/lodash@4/node_modules/lodash/index.js", "x;\n")
    os.symlink(".pnpm/lodash@4/node_modules/lodash", os.path.join(repo, "node_modules/lodash"))
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "ran: red check: ") if "npm test" in l], out
    os.makedirs(os.path.join(repo, "packages/web/node_modules/@acme"))
    os.symlink("../../../app", os.path.join(repo, "packages/web/node_modules/@acme/app"))
    code, out = verify(repo, path)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "packages/web/node_modules/@acme/app" in l], \
        "a package's own node_modules can link a workspace package too:\n" + out
    write(repo, "packages/web/node_modules/.gitkeep", "")
    git(repo, "add", "-f", "packages/web/node_modules/.gitkeep")
    code, out = verify(repo, path)
    assert [l for l in lines(out, "skipped: red check (") if "packages/web/node_modules/@acme/app" in l], \
        "a node_modules with a tracked .gitkeep is listed by its children:\n" + out


def js_failure_summary_matches_the_runners_failure_lines(base):
    spec = importlib.util.spec_from_file_location("verify_auto", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    red = ["Tests:       1 failed, 2 passed, 3 total", "Test Suites: 1 failed, 1 total", " Test Files  1 failed (1)",
           "      Tests  1 failed | 4 passed (5)", "  2 failing", "# fail 1", "ℹ fail 3",
           "\x1b[1mTests:\x1b[22m       \x1b[1m\x1b[31m1 failed\x1b[39m\x1b[22m, 1 total"]
    green = ["Tests:       0 failed, 2 passed", "  0 failing", "# fail 0", "ℹ fail 0", "Error: 1 failed to load"]
    summary = lambda line: module.JS_FAILURE_SUMMARY.search(module.ANSI_COLOR.sub("", line))  # noqa: E731
    assert [l for l in red if not summary(l)] == [], "missed failure summaries"
    assert [l for l in green if summary(l)] == [], "lines that are not a failure summary"


# The related run (no --junitxml) passes; the red-check run records what it saw and writes a JUnit report.
FAKE_PYTEST = """#!/bin/sh
bin=$(dirname "$0")
junit=""
for a in "$@"; do case "$a" in --junitxml=*) junit="${{a#--junitxml=}}";; esac; done
[ -z "$junit" ] && exit {related_exit}
{{ pwd -P; echo "$PYTHONPATH"; echo "$*"; cat tests/conftest.py; echo "bytecode=$PYTHONDONTWRITEBYTECODE"; cat tests/test_mod.py; }} > "$bin/red"
if grep -q fixed src/pkg/mod.py; then case=''; else case='{on_base}'; fi
echo "<testsuites><testsuite>$case</testsuite></testsuites>" > "$junit"
[ -z "$case" ]
"""
FAILING_CASE = '<testcase classname="tests.test_mod" name="test_a"><failure message="assert 1 == 2"/></testcase>'
PY_BASE = {"src/pkg/__init__.py": "", "src/pkg/mod.py": "a = 1\n", "tests/conftest.py": "",
           "tests/test_mod.py": "def test_a(): pass\n"}


def py_change(base, on_base=FAILING_CASE, related_exit=0, with_pytest=True):
    """A fix in src/pkg/mod.py with an edited tests/test_mod.py, a new tests/test_extra.py and an edited conftest."""
    repo = new_repo(base, PY_BASE)
    write(repo, "src/pkg/mod.py", "a = 2  # fixed\n")
    write(repo, "tests/test_mod.py", "def test_a(): assert True\n")
    write(repo, "tests/test_extra.py", "def test_b(): pass\n")
    write(repo, "tests/conftest.py", "# new fixture\n")
    if with_pytest:
        write(repo, ".venv/bin/pytest", FAKE_PYTEST.format(on_base=on_base, related_exit=related_exit), executable=True)
    return repo


def red_run(repo):
    path = os.path.join(repo, ".venv/bin/red")
    return open(path).read().splitlines() if os.path.exists(path) else None


def pytest_red_check_runs_changed_tests_on_the_base(base):
    repo = py_change(base)
    code, out = verify(repo, MINIMAL_PATH, PYTHONPATH="/already/set")
    assert code == 0, out
    assert [l for l in lines(out, "ran: red check: ") if "pytest" in l], out
    copy, pythonpath, args, conftest, bytecode, test_mod = red_run(repo)
    assert "agents-red-" in copy and not os.path.exists(copy), copy
    entries = pythonpath.split(os.pathsep)
    assert [os.path.realpath(e) for e in entries[:2]] == [copy, os.path.join(copy, "src")], pythonpath
    assert entries[2:] == ["/already/set"], pythonpath
    assert args.split()[:3] == ["-q", "-o", "cache_dir=.agents-red-cache"] and args.split()[3].startswith("--junitxml="), args
    assert args.split()[4:] == ["tests/test_extra.py", "tests/test_mod.py"], args
    assert conftest == "# new fixture", "the changed conftest.py was not copied in"
    assert test_mod == "def test_a(): assert True", "the edited test ran in its base version"
    assert bytecode == "bytecode=1", "the run on the base may write __pycache__ through the linked dirs"


def pytest_red_check_errors_when_tests_pass_on_the_base(base):
    repo = py_change(base, on_base='<testcase classname="tests.test_mod" name="test_a"/>')
    write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
        "grep -q fixed src/pkg/mod.py", "false").replace('[ -z "$case" ]', "exit 0"), executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1, out
    errors = [l for l in lines(out, "error: ") if "[red check]" in l]
    assert len(errors) == 1 and "tests/test_mod.py" in errors[0] and "pass without your change" in errors[0], out
    assert not lines(out, "ran: red check"), out


def pytest_red_check_counts_a_collection_error(base):
    repo = py_change(base, on_base='<testcase classname="" name="tests.test_mod">'
                                   '<error message="collection failure">ImportError: cannot import name b</error></testcase>')
    write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
        '[ -z "$case" ]', '[ -z "$case" ] || exit 2'), executable=True)  # pytest's exit status on a collection error
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "ran: red check: ") if "pytest" in l], out


def pytest_red_check_with_empty_report_is_skipped(base):
    repo = py_change(base, on_base="")
    write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
        '[ -z "$case" ]', "exit 5"), executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "pytest" in l and "no test cases" in l], out
    assert not lines(out, "ran: red check") and not lines(out, "error: "), out


def pytest_red_check_without_pytest_is_skipped(base):
    repo = py_change(base, with_pytest=False)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "pytest not installed" in l], out


def pytest_red_check_skips_tests_failing_in_the_working_tree(base):
    repo = py_change(base, related_exit=1)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 1 and "error: [pytest] related tests failed" in out, out
    assert [l for l in lines(out, "skipped: red check (") if "working tree" in l], out
    assert red_run(repo) is None, "the red check ran although the changed tests fail with the change"


def pytest_red_check_copy_build_error_is_skipped(base):
    repo = py_change(base)
    unreadable = os.path.join(repo, "tests/test_extra.py")
    os.chmod(unreadable, 0)
    try:
        code, out = verify(repo, MINIMAL_PATH)
    finally:
        os.chmod(unreadable, 0o644)
    assert code == 0, out
    assert [l for l in lines(out, "skipped: red check (") if "pristine copy" in l], out
    assert red_run(repo) is None and not lines(out, "ran: red check"), out


def pytest_red_check_without_a_conclusive_case_is_skipped(base):
    skipped = '<testcase classname="tests.test_mod" name="test_a"><skipped message="needs a db"/></testcase>'
    passed = '<testcase classname="tests.test_mod" name="test_a"/>'
    for on_base, exit_code, why in [(skipped, "exit 0", "every case skipped"),
                                    (passed, "exit 3", "an internal error after a passing case")]:
        repo = py_change(base, on_base=on_base)
        write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
            "grep -q fixed src/pkg/mod.py", "false").replace('[ -z "$case" ]', exit_code), executable=True)
        code, out = verify(repo, MINIMAL_PATH)
        assert code == 0 and [l for l in lines(out, "skipped: red check (") if "pytest" in l], f"{why}:\n{out}"
        assert not lines(out, "error: "), f"{why} is not a test that can't fail:\n{out}"
        shutil.rmtree(repo)


def pytest_red_check_hanging_runner_is_skipped(base):
    repo = py_change(base)
    write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
        "if grep -q fixed", "pwd -P > \"$bin/red\"; sleep 30 & wait\nif grep -q fixed"),
        executable=True)
    start = time.time()
    code, out = verify(repo, MINIMAL_PATH, red_check_timeout=1)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "timed out after 1s" in l], out
    # Killing only the runner leaves its child holding the output pipe, so verify would wait the full 30 s for it.
    assert time.time() - start < 15, f"the runner's child outlived the timeout ({time.time() - start:.0f}s)"
    copy = red_run(repo)[0]
    assert "agents-red-" in copy and not os.path.exists(copy), "the pristine copy was left behind after the timeout"


def pytest_red_check_on_committed_work_and_ignored_files(base):
    repo = py_change(base)
    write(repo, ".gitignore", "node_modules/\nvendor/\n.venv/\n_version.py\n")
    write(repo, "src/pkg/_version.py", "v = 1\n")  # generated, ignored: the copy needs it to import the package
    write(repo, ".venv/bin/pytest", open(os.path.join(repo, ".venv/bin/pytest")).read().replace(
        "if grep -q fixed", "[ -f src/pkg/_version.py ] || exit 4\nif grep -q fixed"), executable=True)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "fix")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "ran: red check: ") if "pytest" in l], out


def pytest_red_check_ignores_test_support_without_code(base):
    repo = new_repo(base, PY_BASE)
    write(repo, ".venv/bin/pytest", FAKE_PYTEST.format(on_base=FAILING_CASE, related_exit=0), executable=True)
    write(repo, "tests/helpers.py", "def make(): pass\n")
    write(repo, "tests/test_mod.py", "def test_a(): assert True\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and "red check" not in out and red_run(repo) is None, out


def red_check_needs_tests_and_code_of_the_stack(base):
    repo = new_repo(base, {**PY_BASE, **JS_BASE, "src/sum.test.js": "test;\n"})
    write(repo, ".venv/bin/pytest", FAKE_PYTEST.format(on_base=FAILING_CASE, related_exit=0), executable=True)
    write(base, "bin/npm", FAKE_NPM.format(base=MOCHA_FAILING), executable=True)
    path = f"{base}/bin:{MINIMAL_PATH}"
    write(repo, "tests/test_mod.py", "def test_a(): assert True\n")
    write(repo, "src/sum.test.js", "test2;\n")
    code, out = verify(repo, path)
    assert code == 0 and "red check" not in out, "tests-only change:\n" + out
    git(repo, "checkout", "--", ".")
    write(repo, "src/pkg/mod.py", "a = 2  # fixed\n")
    write(repo, "src/sum.js", "fixed;\n")
    code, out = verify(repo, path)
    assert code == 0 and "red check" not in out, "code-only change:\n" + out
    assert red_run(repo) is None and npm_calls(base) == [], (red_run(repo), npm_calls(base))


# --- diff coverage: the related run under coverage.py or c8, a summary of the changed lines it executed ----------
# The fake interpreter runs the launcher with sh, so the related run's verdict is the fake pytest's own.
FAKE_COVERAGE_PYTHON = """#!/bin/sh
bin=$(dirname "$0")
echo "$*" >> "$bin/python-calls"
[ "$1" = -O ] && shift
[ "$1" = -c ] && {{ {version}; exit; }}
[ "$1 $2" = "-m coverage" ] || exit 9
cmd=$3; shift 3
case "$cmd" in
run) cp "${{1#--rcfile=}}" "$bin/rcfile"; echo "$COVERAGE_FILE" > "$bin/coverage-file"; shift
     {before_tests}
     sh "$@"; status=$?
     {after_tests}; exit $status;;
json) while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out=$2; shift; done
      {json};;
esac
"""
PY_COVERAGE_BASE = {".gitignore": "node_modules/\nvendor/\n.venv/\n.coverage\n", "pkg/__init__.py": "",
                    "pkg/mod.py": "a = 1\nb = 2\nc = 3\n", "pkg/gone.py": "g = 1\n", "tests/test_mod.py": "def test_a(): pass\n"}
# pkg/mod.py: lines 2-4 changed, 4 a comment; pkg/new.py untracked, every line new; pkg/gone.py never loaded.
PY_COVERAGE_REPORT = {"files": {"pkg/mod.py": {"executed_lines": [1, 2], "missing_lines": [3]},
                                "pkg/new.py": {"executed_lines": [1], "missing_lines": [2, 3]},
                                "pkg/__init__.py": {"executed_lines": [], "missing_lines": []}}}
FAKE_RELATED_PYTEST = """#!/bin/sh
echo "$@" >> "$(dirname "$0")/args"
{body}
"""


def py_coverage_change(base, report=PY_COVERAGE_REPORT, pytest_body="exit 0", python_dir=".venv/bin", **fake):
    """A change to pkg/mod.py, pkg/gone.py and a new pkg/new.py, with a fake pytest and coverage-capable python."""
    repo = new_repo(base, PY_COVERAGE_BASE)
    write(repo, "pkg/mod.py", "a = 1\nb = 20\nc = 30\n# why c is 30\n")
    write(repo, "pkg/gone.py", "g = 2\n")
    write(repo, "pkg/new.py", "x = 1\ny = 2\nz = 3\n")
    write(repo, "conftest.py", "import pkg\n")
    write(repo, ".venv/bin/pytest", FAKE_RELATED_PYTEST.format(body=pytest_body), executable=True)
    write(base, "report.json", report if isinstance(report, str) else json.dumps(report))
    defaults = {"version": ":", "before_tests": ":", "after_tests": 'echo data > "$COVERAGE_FILE"',
                "json": f'cp "{base}/report.json" "$out"'}
    write(repo if python_dir == ".venv/bin" else base, f"{python_dir}/python",
          FAKE_COVERAGE_PYTHON.format(**{**defaults, **fake}), executable=True)
    return repo


def read_lines(path):
    return open(path).read().splitlines() if os.path.exists(path) else []


def pytest_diff_coverage_reports_changed_lines(base):
    repo = py_coverage_change(base)
    write(repo, ".coverage", "the project's own data\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and not lines(out, "error: ") and not lines(out, "warning: "), out
    assert lines(out, "ran: diff coverage") == [
        "ran: diff coverage (coverage.py): 2 of 5 changed lines covered by the related tests (40%); "
        "uncovered: pkg/mod.py:3, pkg/new.py:2-3; not in the report: pkg/gone.py"], out
    assert "ran: pytest on 1 related files: exit 0" in out, out
    bin_dir = os.path.join(repo, ".venv/bin")
    probe, run, report = read_lines(os.path.join(bin_dir, "python-calls"))
    assert probe.startswith("-c import coverage") and "pytest_cov" in probe, probe
    rcfile = run.split()[3]
    assert run.split()[:3] == ["-m", "coverage", "run"] and rcfile.startswith("--rcfile="), run
    assert run.split()[4].endswith(".venv/bin/pytest") and run.split()[5:] == ["-q", "tests/test_mod.py"], \
        "the launcher script runs under coverage, with the related run's own arguments:\n" + run
    assert report.split()[:4] == ["-m", "coverage", "json", rcfile] and report.split()[4] == "-o", report
    rc = read_lines(os.path.join(bin_dir, "rcfile"))
    include = [l.strip() for l in rc[rc.index("include =") + 1:] if l.startswith("    ")]
    real = os.path.realpath(repo)
    assert [os.path.realpath(p) for p in include] == [os.path.join(real, f) for f in ("pkg/gone.py", "pkg/mod.py", "pkg/new.py")], \
        "only the changed non-test files are collected:\n" + "\n".join(rc)
    data = read_lines(os.path.join(bin_dir, "coverage-file"))[0]
    tmp = os.path.dirname(rcfile[len("--rcfile="):])
    assert os.path.dirname(data) == tmp, (data, tmp)
    assert os.path.dirname(report.split()[5]) == tmp, report
    assert not os.path.exists(tmp), "the coverage temp dir was left behind"
    assert open(os.path.join(repo, ".coverage")).read() == "the project's own data\n", "the project's .coverage changed"
    left = [p for p in os.listdir(repo) if p.startswith(".coverage") and p != ".coverage" or p == "coverage"]
    assert not left, f"coverage wrote into the working tree: {left}"


def pytest_diff_coverage_uses_the_python_its_shebang_names(base):
    repo = py_coverage_change(base, python_dir="py/bin")
    os.remove(os.path.join(repo, ".venv/bin/pytest"))
    write(base, "bin/pytest", f"#!{base}/py/bin/python -O\n" + FAKE_RELATED_PYTEST.format(body="exit 0"), executable=True)
    # A python beside a pytest found on PATH isn't a venv's: the shebang decides.
    write(base, "bin/python", "#!/bin/sh\necho \"$*\" >> \"$(dirname \"$0\")/python-calls\"\nexit 9\n", executable=True)
    code, out = verify(repo, f"{base}/bin:{MINIMAL_PATH}")
    assert code == 0 and lines(out, "ran: diff coverage (coverage.py): 2 of 5 "), out
    probe, run = read_lines(os.path.join(base, "py/bin/python-calls"))[:2]
    assert probe.startswith("-O -c ") and run.split()[:4] == ["-O", "-m", "coverage", "run"], \
        "the shebang's flags reach the coverage run:\n" + run
    assert run.split()[5] == os.path.join(base, "bin/pytest"), run
    assert not os.path.exists(os.path.join(base, "bin/python-calls")), "the python beside a PATH pytest was used"
    os.remove(os.path.join(base, "bin/python"))
    write(base, "bin/pytest", "#!/usr/bin/env bash\n" + FAKE_RELATED_PYTEST.format(body="exit 0"), executable=True)
    code, out = verify(repo, f"{base}/bin:{MINIMAL_PATH}")
    assert code == 0 and "ran: pytest on 1 related files: exit 0" in out, out
    assert [l for l in lines(out, "skipped: diff coverage (") if "#!/usr/bin/env bash" in l], \
        "a launcher like a pyenv shim leaves the Python unknown:\n" + out
    assert len(read_lines(os.path.join(base, "py/bin/python-calls"))) == 3, "the unknown Python was probed"
    shutil.rmtree(repo)
    repo = py_coverage_change(base)
    write(repo, ".venv/bin/pytest", f"#!{repo}/.venv/bin/python -O\n" + FAKE_RELATED_PYTEST.format(body="exit 0"),
          executable=True)
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and lines(out, "ran: diff coverage (coverage.py): 2 of 5 "), out
    run = read_lines(os.path.join(repo, ".venv/bin/python-calls"))[1]
    assert run.split()[:4] == ["-O", "-m", "coverage", "run"], "a venv launcher's shebang flags were dropped:\n" + run
    assert len(read_lines(os.path.join(base, "bin/args"))) == 2, "the shim's pytest ran once"


def diff_coverage_skips_say_why(base):
    cases = [
        ("coverage.py missing", {"version": 'echo "No module named coverage" >&2; exit 1'}, {}, ["No module named coverage"]),
        ("pytest-cov installed", {"version": "exit 3"}, {}, ["pytest-cov is installed"]),
        ("a passing run without a data file", {"after_tests": ":"}, {}, ["left no data file"]),
        ("a run with no verdict", {"pytest_body": "exit 4"}, {}, ["gave no verdict (exit 4)"]),
        ("more changed files than the cap", {}, {"MAX_COVERAGE_FILES": 2}, ["3 changed Python files to measure, more than 2"]),
        ("no report", {"json": 'echo "Couldn\'t use data file" >&2; exit 1'}, {}, ["wrote no JSON report", "data file"]),
        ("an unreadable report", {"json": 'echo "{not json" > "$out"'}, {}, ["unreadable"]),
        # What coverage json prints, exit 1, when none of the collected files loaded.
        ("every file absent", {"json": 'echo "No data to report."; exit 1'}, {},
         ["no changed line is executable", "not in the report: pkg/gone.py, pkg/mod.py, pkg/new.py"]),
        ("a hanging report step", {"json": "sleep 30 & wait"}, {"LINT_TIMEOUT": 1},
         ["wrote no JSON report", "timed out after 1s"]),
        ("a hanging related run", {"before_tests": "sleep 30 & wait"}, {"TEST_TIMEOUT": 1}, ["timed out after 1s"]),
    ]
    for why, fake, timeouts, reasons in cases:
        repo = py_coverage_change(base, **fake)
        code, out = verify(repo, MINIMAL_PATH, timeouts=timeouts)
        assert [l for l in lines(out, "skipped: diff coverage (") if all(r in l for r in reasons)], f"{why}:\n{out}"
        assert not lines(out, "ran: diff coverage") and not lines(out, "error: "), f"{why}:\n{out}"
        assert code == 0, f"{why}:\n{out}"
        runs = read_lines(os.path.join(repo, ".venv/bin/args"))
        if "TEST_TIMEOUT" in timeouts:
            assert [l for l in lines(out, "skipped: pytest (exit 124") if "timed out" in l], f"{why}:\n{out}"
            assert runs == [], f"{why}: pytest ran again after the timeout"
        else:
            assert runs == ["-q tests/test_mod.py"], f"{why}: pytest ran {len(runs)} times"
        if "MAX_COVERAGE_FILES" in timeouts:
            assert not os.path.exists(os.path.join(repo, ".venv/bin/python-calls")), f"{why}: coverage.py was probed"
        shutil.rmtree(repo)
    repo = py_coverage_change(base, json='echo \'{"files": {"pkg/mod.py": {"executed_lines": [1, 2, 3], '
                                         '"missing_lines": []}}}\' > "$out"')
    git(repo, "checkout", "--", ".")
    os.remove(os.path.join(repo, "pkg/new.py"))
    write(repo, "pkg/mod.py", "a = 1\nb = 2\nc = 3\n# only a comment\n")
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "skipped: diff coverage (") if "executable" in l], "comments only:\n" + out
    git(repo, "checkout", "--", ".")
    write(repo, "tests/test_mod.py", "def test_a(): assert True\n")
    os.remove(os.path.join(repo, ".venv/bin/python-calls"))
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "skipped: diff coverage (") if "non-test" in l], "tests only:\n" + out
    assert not os.path.exists(os.path.join(repo, ".venv/bin/python-calls")), "a tests-only change was measured"
    git(repo, "checkout", "--", ".")
    write(repo, "pkg/gone.py", "g = 3\n")  # no test_gone.py: no related run
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and [l for l in lines(out, "skipped: diff coverage (") if "no related" in l], "no related run:\n" + out


def diff_coverage_keeps_the_related_run_verdict(base):
    failing = 'echo "FAILED tests/test_mod.py::test_a - assert 20 == 2"; exit 1'
    repo = py_coverage_change(base, pytest_body=failing)
    code, out = verify(repo, MINIMAL_PATH)
    os.remove(os.path.join(repo, ".venv/bin/python"))
    bare_code, bare_out = verify(repo, MINIMAL_PATH)
    assert (code, lines(out, "error: ")) == (bare_code, lines(bare_out, "error: ")) == (
        1, ["error: [pytest] FAILED tests/test_mod.py::test_a - assert 20 == 2"]), out + bare_out
    assert lines(out, "ran: diff coverage (coverage.py): 2 of 5 "), "a failing run's coverage is reported too:\n" + out


def coverage_that_fails_before_the_tests_reruns_them_without_it(base):
    repo = py_coverage_change(base, before_tests='echo "coverage: cannot start" >&2; exit 1')
    code, out = verify(repo, MINIMAL_PATH)
    assert code == 0 and "ran: pytest on 1 related files: exit 0" in out, out
    assert [l for l in lines(out, "skipped: diff coverage (") if "cannot start" in l], out
    assert read_lines(os.path.join(repo, ".venv/bin/args")) == ["-q tests/test_mod.py"], "pytest ran once, without coverage"


# c8 report turns the V8 coverage npm test left in --temp-directory into the Istanbul JSON report the test gives it.
FAKE_C8 = """#!/bin/sh
bin=$(dirname "$0")
echo "$*" >> "$bin/c8-calls"
[ "$1" = report ] || exit 9
for a; do case "$a" in --reports-dir=*) dir=${{a#--reports-dir=}};; --temp-directory=*) raw=${{a#--temp-directory=}};; esac; done
ls "$raw" >> "$bin/c8-raw"
{report_step}
"""
# What the fake npm adds to collect V8 coverage: node writes a file into NODE_V8_COVERAGE.
NPM_V8 = 'echo "${NODE_V8_COVERAGE:-none}" >> "$(dirname "$0")/v8-dirs"; [ -z "$NODE_V8_COVERAGE" ] || echo "{}" > "$NODE_V8_COVERAGE/coverage-1.json"\n'


def istanbul(path, statements):
    """An Istanbul file entry as c8 writes it, from (line, count) per statement."""
    return {path: {"path": path, "s": {str(i): n for i, (_, n) in enumerate(statements)},
                   "statementMap": {str(i): {"start": {"line": l, "column": 0}, "end": {"line": l, "column": 5}}
                                    for i, (l, _) in enumerate(statements)}}}


def with_c8(base, repo, report_step='cp "{report}" "$dir/coverage-final.json"', on_base=MOCHA_FAILING):
    real = os.path.realpath(repo)
    # Line 1 has two statements and only the second ran: still covered.
    write(base, "c8-report.json", json.dumps(istanbul(f"{real}/src/sum.js", [(1, 0), (1, 1), (2, 0), (3, 2)])))
    write(repo, "node_modules/.bin/c8", FAKE_C8.format(report_step=report_step.replace("{report}", f"{base}/c8-report.json")),
          executable=True)
    write(base, "bin/npm", FAKE_NPM.format(base=on_base).replace("\n", "\n" + NPM_V8, 1), executable=True)
    write(repo, "src/sum.js", "fixed;\nmore;\n")


def v8_dirs(base):
    return read_lines(os.path.join(base, "bin/v8-dirs"))


def c8_measures_the_red_checks_working_tree_run(base):
    repo, path = js_change(base)
    with_c8(base, repo)
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "ran: red check: ") if "npm test" in l], out
    assert lines(out, "ran: diff coverage") == [
        "ran: diff coverage (c8): 1 of 2 changed lines covered by the related tests (50%); uncovered: src/sum.js:2"], out
    dirs = v8_dirs(base)
    assert len(dirs) == 2 and dirs[1] == "none" and os.path.basename(dirs[0]) == "raw", \
        "only the working-tree run collects V8 coverage:\n" + "\n".join(dirs)
    calls = read_lines(os.path.join(repo, "node_modules/.bin/c8-calls"))
    assert len(calls) == 1, calls
    args = calls[0].split()
    assert args[0] == "report" and f"--temp-directory={dirs[0]}" in args, args
    for flag in ("--check-coverage=false", "--100=false", "--skip-full=false", "--all=false", "--reporter=json"):
        assert flag in args, f"{flag} missing: {args}"
    assert [a for a in args if a.startswith("--include=")] == ["--include=src/sum.js"], args
    assert read_lines(os.path.join(repo, "node_modules/.bin/c8-raw")) == ["coverage-1.json"], "c8 read another dir"
    assert not os.path.exists(os.path.dirname(dirs[0])), "the coverage temp dir was left behind"
    assert not os.path.exists(os.path.join(repo, "coverage")), "c8 wrote coverage/ into the working tree"


def c8_leaves_out_blank_and_line_comment_lines(base):
    repo, path = js_change(base)
    with_c8(base, repo)
    write(repo, "src/sum.js", "fixed;\n\n  // why\nmore;\n")
    statements = [(1, 1), (2, 0), (3, 0), (4, 0)]
    write(base, "c8-report.json", json.dumps(istanbul(f"{os.path.realpath(repo)}/src/sum.js", statements)))
    code, out = verify(repo, path)
    assert lines(out, "ran: diff coverage") == [
        "ran: diff coverage (c8): 1 of 2 changed lines covered by the related tests (50%); uncovered: src/sum.js:4"], out


def c8_reports_a_failing_working_tree_run(base):
    repo, path = js_change(base)
    with_c8(base, repo)
    write(repo, "src/sum.js", "broken;\nmore;\n")
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "fail in the working tree" in l], out
    assert lines(out, "ran: diff coverage (c8): 1 of 2 changed lines"), out
    assert len(npm_calls(base)) == 1, npm_calls(base)


def c8_report_failures_are_skipped(base):
    cases = [
        ("no report", 'echo "c8: cannot read" >&2; exit 1', {}, ["wrote no JSON report (exit 1)", "cannot read"]),
        ("an unreadable report", 'echo "{not json" > "$dir/coverage-final.json"', {}, ["unreadable"]),
        ("a line number past a float's range",
         'sed "s/\\"line\\": 1,/\\"line\\": 1e309,/" "{report}" > "$dir/coverage-final.json"', {}, ["unreadable"]),
        ("a hanging report step", "sleep 30 & wait", {"LINT_TIMEOUT": 1}, ["timed out after 1s"]),
        # The report runs after the merge-base run, so one as slow as the red check's budget leaves it its verdict.
        ("a report step as slow as the red check", "sleep 30 & wait", {"LINT_TIMEOUT": 6, "RED_CHECK_TIMEOUT": 6},
         ["timed out after 6s"]),
    ]
    for why, step, timeouts, reasons in cases:
        repo, path = js_change(base)
        with_c8(base, repo, report_step=step, on_base="sleep 2; " + MOCHA_FAILING)
        code, out = verify(repo, path, timeouts=timeouts)
        assert code == 0 and [l for l in lines(out, "ran: red check: ") if "npm test" in l], f"{why}:\n{out}"
        assert [l for l in lines(out, "skipped: diff coverage (") if all(r in l for r in reasons)], f"{why}:\n{out}"
        assert not lines(out, "ran: diff coverage"), f"{why}:\n{out}"
        shutil.rmtree(repo)
        os.remove(os.path.join(base, "bin/calls"))


def c8_skips_vitest_projects_and_tests_only_changes(base):
    repo, path = js_change(base, {**JS_BASE, "package.json": '{"scripts": {"test": "mocha"}, '
                                                              '"devDependencies": {"vitest": "^2"}}\n'})
    write(repo, "node_modules/.bin/vitest", "#!/bin/sh\nexit 0\n", executable=True)
    with_c8(base, repo)
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "skipped: diff coverage (") if "vitest is a dependency" in l], out
    assert not lines(out, "ran: diff coverage") and "ran: vitest tests related to" in out, out
    assert not os.path.exists(os.path.join(repo, "node_modules/.bin/c8-calls")), "c8 ran for a vitest project"
    assert v8_dirs(base) == ["none", "none"], v8_dirs(base)
    shutil.rmtree(repo)
    os.remove(os.path.join(base, "bin/v8-dirs"))
    repo, path = js_change(base)
    with_c8(base, repo)
    git(repo, "checkout", "--", "src/sum.js")
    code, out = verify(repo, path)
    assert code == 0 and [l for l in lines(out, "skipped: diff coverage (") if "non-test JS/TS" in l], out
    assert not os.path.exists(os.path.join(repo, "node_modules/.bin/c8-calls")) and \
        all(d == "none" for d in v8_dirs(base)), "a tests-only change was measured"

for t in [no_changes_is_nothing_to_check, unsupported_files_only, eslint_reports_only_changed_lines,
          eslint_missing_is_skipped, vitest_failure_is_error, vitest_dependency_not_installed_is_skipped,
          ruff_reports_only_changed_lines, committed_branch_work_is_checked, pytest_runs_related_tests, php_lint_catches_syntax_error,
          phpunit_runs_related_test_and_reports_failures, phpunit_that_cannot_start_is_skipped,
          shell_and_go_without_tools_are_skipped,
          npm_test_red_check_runs_changed_tests_on_the_base, npm_test_red_check_errors_when_tests_pass_on_the_base,
          npm_test_red_check_without_failure_summary_is_skipped, npm_placeholder_or_missing_test_script_is_skipped,
          npm_test_red_check_hanging_runner_is_skipped, npm_test_red_check_skips_workspace_links,
          npm_test_red_check_skips_tests_failing_in_the_working_tree, pytest_red_check_runs_changed_tests_on_the_base,
          pytest_red_check_errors_when_tests_pass_on_the_base, pytest_red_check_counts_a_collection_error,
          pytest_red_check_with_empty_report_is_skipped, pytest_red_check_without_pytest_is_skipped,
          pytest_red_check_skips_tests_failing_in_the_working_tree, pytest_red_check_copy_build_error_is_skipped,
          pytest_red_check_without_a_conclusive_case_is_skipped, pytest_red_check_hanging_runner_is_skipped,
          pytest_red_check_on_committed_work_and_ignored_files, pytest_red_check_ignores_test_support_without_code,
          npm_test_red_check_without_npm_is_skipped, npm_test_red_check_follows_links_that_stay_in_node_modules,
          js_failure_summary_matches_the_runners_failure_lines, red_check_needs_tests_and_code_of_the_stack,
          pytest_diff_coverage_reports_changed_lines, pytest_diff_coverage_uses_the_python_its_shebang_names,
          diff_coverage_skips_say_why, diff_coverage_keeps_the_related_run_verdict,
          coverage_that_fails_before_the_tests_reruns_them_without_it, c8_measures_the_red_checks_working_tree_run,
          c8_leaves_out_blank_and_line_comment_lines, c8_reports_a_failing_working_tree_run, c8_report_failures_are_skipped,
          c8_skips_vitest_projects_and_tests_only_changes]:
    test(t)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
