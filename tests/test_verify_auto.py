#!/usr/bin/env python3
"""Tests for repos/_shared/verify_auto.py. Each test builds a throwaway git repo with fake project-local
tools (node_modules/.bin, .venv/bin, vendor/bin), so results don't depend on what is installed here.

Run: python3 ~/.agents/tests/test_verify_auto.py   (exit 1 on any failure)
"""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
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


def verify(repo, path=None, red_check_timeout=None, **env):
    env = {**os.environ, **({"PATH": path} if path else {}), **env}
    args = [sys.executable, SCRIPT]
    if red_check_timeout:
        lowered = ("import importlib.util, sys\n"
                   f"spec = importlib.util.spec_from_file_location('verify_auto', {SCRIPT!r})\n"
                   "module = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(module)\n"
                   f"module.RED_CHECK_TIMEOUT = {red_check_timeout}\nsys.exit(module.main())")
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
    assert args.split()[:3] == ["-q", "-p", "no:cacheprovider"] and args.split()[3].startswith("--junitxml="), args
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
        "if grep -q fixed", "pwd -P > \"$bin/red\"; exec sleep 30\nif grep -q fixed"), executable=True)
    code, out = verify(repo, MINIMAL_PATH, red_check_timeout=1)
    assert code == 0 and [l for l in lines(out, "skipped: red check (") if "timed out after 1s" in l], out
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
          js_failure_summary_matches_the_runners_failure_lines, red_check_needs_tests_and_code_of_the_stack]:
    test(t)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
