#!/usr/bin/env python3
"""Tests for repos/_shared/verify_auto.py. Each test builds a throwaway git repo with fake project-local
tools (node_modules/.bin, .venv/bin, vendor/bin), so results don't depend on what is installed here.

Run: python3 ~/.agents/tests/test_verify_auto.py   (exit 1 on any failure)
"""
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


def verify(repo, path=None):
    env = {**os.environ, "PATH": path} if path else os.environ
    proc = subprocess.run([sys.executable, SCRIPT], cwd=repo, capture_output=True, text=True, env=env, timeout=120)
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


for t in [no_changes_is_nothing_to_check, unsupported_files_only, eslint_reports_only_changed_lines,
          eslint_missing_is_skipped, vitest_failure_is_error, vitest_dependency_not_installed_is_skipped,
          ruff_reports_only_changed_lines, committed_branch_work_is_checked, pytest_runs_related_tests, php_lint_catches_syntax_error,
          phpunit_runs_related_test_and_reports_failures, phpunit_that_cannot_start_is_skipped,
          shell_and_go_without_tools_are_skipped]:
    test(t)

failed = [(n, e) for n, e in RESULTS if e]
for name, err in RESULTS:
    print(f"{'FAIL' if err else 'ok  '} {name}")
    if err:
        print("     " + err.strip().replace("\n", "\n     "))
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
sys.exit(1 if failed else 0)
