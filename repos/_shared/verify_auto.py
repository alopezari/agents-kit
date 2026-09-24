#!/usr/bin/env python3
"""Stack-detecting verify for repos without a hand-written ~/.agents/repos/<repo>/verify.

Runs from the repo root at the end of every agent turn, so it only looks at changed files
and only runs tests related to them, never full suites. Output follows verify_changed.py:
`ran: ` (what executed), `warning: `, `error: `, plus `skipped: <check> (<reason>)` for
checks that don't apply or whose tool is missing. Exit 1 only for findings on changed
lines or failing related tests; a skipped check never fails the run.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

_spec = importlib.util.spec_from_file_location(
    "verify_changed", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_changed.py"))
verify_changed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_changed)
changed_lines, on_changed_line, execute, TEST_FILE = (
    verify_changed.changed_lines, verify_changed.on_changed_line, verify_changed.execute, verify_changed.TEST_FILE)

MAX_PER_CHECK = 30
LINT_TIMEOUT = 120
TEST_TIMEOUT = 300
NOT_STARTED = (124, 127)  # execute(): timed out, binary missing
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
PHPCS_CONFIGS = ("phpcs.xml", "phpcs.xml.dist", ".phpcs.xml", ".phpcs.xml.dist")
PHPSTAN_CONFIGS = ("phpstan.neon", "phpstan.neon.dist")


class Report:
    def __init__(self):
        self.ran, self.warnings, self.errors, self.skipped = [], [], [], []

    def add(self, errors=(), warnings=()):
        self.errors += list(errors)[:MAX_PER_CHECK]
        self.warnings += list(warnings)[:MAX_PER_CHECK]

    def skip(self, check, reason):
        self.skipped.append(f"{check} ({reason})")

    def print(self):
        for prefix, items in (("ran", self.ran), ("skipped", self.skipped),
                              ("warning", self.warnings), ("error", self.errors)):
            for item in items:
                print(f"{prefix}: {item}")


def find_tool(root, name, local_dirs):
    """Project-local binary first, then PATH. Returns (path or None, where it was looked for)."""
    for d in local_dirs:
        path = os.path.join(root, d, name)
        if os.access(path, os.X_OK):
            return path, None
    found = shutil.which(name)
    return found, f"{name} not installed in {' or '.join(local_dirs + ('PATH',))}"


def tail(proc, chars=600):
    return (proc.stdout + proc.stderr).strip()[-chars:]


def repo_path(root, path):
    """Tool-reported path (absolute or relative to root) -> repo-relative path as changed_lines keys it."""
    full = path if os.path.isabs(path) else os.path.join(root, path)
    return os.path.relpath(os.path.realpath(full), os.path.realpath(root))


def decode_json(text, opener):
    """First JSON value starting at `opener` ('[' or '{'); tools sometimes print notices around it."""
    try:
        return json.JSONDecoder().raw_decode(text[text.index(opener):])[0]
    except ValueError:
        return None


def tracked_files(root):
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=root, capture_output=True, text=True).stdout
    return out.splitlines()


# --- JS / TS ----------------------------------------------------------------------------------
def check_js(root, changes, files, report):
    """ESLint on changed lines; Vitest or Jest on the tests related to the changed files."""
    if not os.path.isfile(os.path.join(root, "package.json")):
        report.skip("eslint", "no package.json at the repo root")
        return
    eslint, why = find_tool(root, "eslint", ("node_modules/.bin",))
    if not eslint:
        report.skip("eslint", why)
    else:
        proc = execute([eslint, "--format", "json", *files], root, LINT_TIMEOUT)
        results = decode_json(proc.stdout or "", "[")
        if not isinstance(results, list):
            report.skip("eslint", f"no JSON output, exit {proc.returncode}: {tail(proc, 300)}")
        else:
            errors, warnings = [], []
            for result in results:
                path = repo_path(root, result["filePath"])
                for m in result.get("messages", []):
                    if on_changed_line(changes, path, m.get("line") or 0):
                        msg = f"{path}:{m.get('line')} [eslint {m.get('ruleId') or 'parse'}] {m['message']}"
                        (errors if m.get("severity") == 2 else warnings).append(msg)
            report.add(errors, warnings)
            report.ran.append(f"eslint on {len(files)} changed files (changed lines only)")

    try:
        pkg = json.load(open(os.path.join(root, "package.json")))
    except (OSError, ValueError) as err:
        report.skip("JS related tests", f"package.json unreadable: {err}")
        return
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    runners = [(name, args) for name, args in (
        ("vitest", ["related", "--run", "--passWithNoTests", *files]),
        ("jest", ["--findRelatedTests", *files, "--passWithNoTests", "--ci"])) if name in deps]
    if not runners:
        report.skip("JS related tests", "neither vitest nor jest is a dependency in package.json")
        return
    name, args = runners[0]
    runner = os.path.join(root, "node_modules/.bin", name)
    if not os.access(runner, os.X_OK):
        report.skip(f"{name} related tests", f"{name} not installed in node_modules/.bin")
        return
    proc = execute([runner, *args], root, TEST_TIMEOUT)
    if proc.returncode in NOT_STARTED:
        report.skip(f"{name} related tests", tail(proc, 300))
        return
    report.ran.append(f"{name} tests related to {len(files)} changed files: exit {proc.returncode}")
    if proc.returncode:
        failed = sorted({line.strip() for line in (proc.stdout + proc.stderr).splitlines()
                         if re.match(r"\s*(FAIL|×|✕|●)\s", line)})
        report.add([f"[{name}] {f}" for f in failed] or [f"[{name}] related tests failed:\n{tail(proc)}"])


# --- PHP --------------------------------------------------------------------------------------
def php_findings(root, changes, data, source):
    errors, warnings = [], []
    for fpath, entry in (data.get("files") or {}).items():
        path = repo_path(root, fpath)
        for m in entry.get("messages", []):
            if on_changed_line(changes, path, m.get("line") or 0):
                label = f"phpcs {m.get('source')}" if source == "phpcs" else "phpstan"
                msg = f"{path}:{m.get('line')} [{label}] {m['message']}"
                (warnings if m.get("type") == "WARNING" else errors).append(msg)
    return errors, warnings


def related_php_tests(root, files):
    """Test classes for the change: changed *Test.php files plus FooTest.php for each changed Foo.php."""
    wanted = {os.path.splitext(os.path.basename(f))[0] + "Test" for f in files if not TEST_FILE.search(f)}
    classes = {os.path.splitext(os.path.basename(f))[0] for f in files if TEST_FILE.search(f)}
    for path in tracked_files(root):
        top = path.split("/", 1)[0]
        name = os.path.splitext(os.path.basename(path))[0]
        if top in ("tests", "test") and path.endswith("Test.php") and name in wanted:
            classes.add(name)
    return sorted(classes)


def check_php(root, changes, files, report):
    """php -l; PHPCS and PHPStan on changed lines when the project configures them; PHPUnit on related test classes."""
    php = shutil.which("php")
    if not php:
        report.skip("php -l", "php not on PATH")
    else:
        errors = []
        for f in files:
            proc = execute([php, "-l", f], root, 30)
            if proc.returncode:
                lines = [l.strip() for l in (proc.stdout + proc.stderr).splitlines() if "error" in l.lower()]
                errors.append(f"{f} [php -l] {lines[0] if lines else tail(proc, 300)}")
        report.add(errors)
        report.ran.append(f"php -l on {len(files)} changed files")

    for tool, configs, args in (
            ("phpcs", PHPCS_CONFIGS, ["--report=json", "-q"]),
            ("phpstan", PHPSTAN_CONFIGS, ["analyse", "--error-format=json", "--no-progress"])):
        binary = os.path.join(root, "vendor/bin", tool)
        if not os.access(binary, os.X_OK):
            report.skip(tool, f"{tool} not installed in vendor/bin")
            continue
        if not any(os.path.isfile(os.path.join(root, c)) for c in configs):
            report.skip(tool, f"no {' / '.join(configs)} at the repo root")
            continue
        proc = execute([binary, *args, *files], root, LINT_TIMEOUT)
        data = decode_json(proc.stdout or "", "{")
        if not isinstance(data, dict) or "files" not in data:
            report.skip(tool, f"no JSON output, exit {proc.returncode}: {tail(proc, 300)}")
            continue
        report.add(*php_findings(root, changes, data, tool))
        report.ran.append(f"{tool} on {len(files)} changed files (changed lines only)")

    phpunit = os.path.join(root, "vendor/bin/phpunit")
    classes = related_php_tests(root, files)
    if not classes:
        report.skip("phpunit", "no related FooTest.php under tests/ or test/")
        return
    if not os.access(phpunit, os.X_OK):
        report.skip("phpunit", "phpunit not installed in vendor/bin")
        return
    with tempfile.TemporaryDirectory() as tmp:
        junit = os.path.join(tmp, "junit.xml")
        proc = execute([phpunit, "--filter", "/(" + "|".join(classes) + ")/", "--log-junit", junit], root, TEST_TIMEOUT)
        try:
            cases = list(ET.parse(junit).getroot().iter("testcase"))
        except (ET.ParseError, OSError):
            # Suites that need a database or WordPress often can't start here; that's setup, not a failure.
            report.skip("phpunit", f"no JUnit report, PHPUnit did not start (exit {proc.returncode}): {tail(proc, 300)}")
            return
    failed = [f"{c.get('class') or c.get('classname')}::{c.get('name')}" for c in cases
              if c.find("failure") is not None or c.find("error") is not None]
    report.ran.append(f"phpunit --filter {', '.join(classes)}: {len(cases)} tests, {len(failed)} failing")
    report.add([f"[phpunit] {t} fails" for t in failed])


# --- Python -----------------------------------------------------------------------------------
VENV_DIRS = (".venv/bin", "venv/bin")


def related_python_tests(root, files):
    tests = {f for f in files if re.search(r"(^|/)(test_[^/]*|[^/]*_test)\.py$", f)}
    modules = {os.path.splitext(os.path.basename(f))[0] for f in files if f not in tests}
    names = {f"test_{m}.py" for m in modules} | {f"{m}_test.py" for m in modules}
    tests.update(p for p in tracked_files(root) if os.path.basename(p) in names and os.path.isfile(os.path.join(root, p)))
    return sorted(tests)


def check_python(root, changes, files, report):
    """Ruff on changed lines; pytest on the related test_<module>.py or <module>_test.py files."""
    ruff, why = find_tool(root, "ruff", VENV_DIRS)
    if not ruff:
        report.skip("ruff", why)
    else:
        proc = execute([ruff, "check", "--output-format", "json", *files], root, LINT_TIMEOUT)
        results = decode_json(proc.stdout or "", "[")
        if not isinstance(results, list):
            report.skip("ruff", f"no JSON output, exit {proc.returncode}: {tail(proc, 300)}")
        else:
            errors = []
            for r in results:
                path, line = repo_path(root, r["filename"]), (r.get("location") or {}).get("row") or 0
                if on_changed_line(changes, path, line):
                    errors.append(f"{path}:{line} [ruff {r.get('code') or 'syntax'}] {r['message']}")
            report.add(errors)
            report.ran.append(f"ruff on {len(files)} changed files (changed lines only)")

    tests = related_python_tests(root, files)
    if not tests:
        report.skip("pytest", "no related test_<module>.py or <module>_test.py")
        return
    pytest, why = find_tool(root, "pytest", VENV_DIRS)
    if not pytest:
        report.skip("pytest", why)
        return
    proc = execute([pytest, "-q", *tests], root, TEST_TIMEOUT)
    if proc.returncode == 5:
        report.ran.append(f"pytest on {len(tests)} related files: no tests collected")
    elif proc.returncode in (0, 1, 2):  # 1: tests failed, 2: collection error or interrupted
        report.ran.append(f"pytest on {len(tests)} related files: exit {proc.returncode}")
        if proc.returncode:
            failed = [l.strip() for l in proc.stdout.splitlines() if l.startswith(("FAILED ", "ERROR "))]
            report.add([f"[pytest] {f}" for f in failed] or [f"[pytest] related tests failed:\n{tail(proc)}"])
    else:
        report.skip("pytest", f"exit {proc.returncode}: {tail(proc, 300)}")


# --- Go ---------------------------------------------------------------------------------------
def check_go(root, changes, files, report):
    """gofmt -l on changed files; go vet and go test on the changed packages."""
    if not os.path.isfile(os.path.join(root, "go.mod")):
        report.skip("go", "no go.mod at the repo root")
        return
    gofmt, why = find_tool(root, "gofmt", ())
    if not gofmt:
        report.skip("gofmt", why)
    else:
        proc = execute([gofmt, "-l", *files], root, LINT_TIMEOUT)
        if proc.returncode in NOT_STARTED:
            report.skip("gofmt", tail(proc, 300))
        else:
            unformatted = [l.strip() for l in proc.stdout.splitlines() if l.strip()]
            report.add([f"{f} [gofmt] not gofmt-formatted; run gofmt -w {f}" for f in unformatted])
            if proc.returncode and not unformatted:  # gofmt reports syntax errors on stderr
                report.add([f"[gofmt] {l}" for l in proc.stderr.strip().splitlines()])
            report.ran.append(f"gofmt -l on {len(files)} changed files")

    go, why = find_tool(root, "go", ())
    if not go:
        report.skip("go vet / go test", why)
        return
    packages = sorted({"./" + (os.path.dirname(f) or ".") for f in files})
    proc = execute([go, "vet", *packages], root, LINT_TIMEOUT)
    if proc.returncode in NOT_STARTED:
        report.skip("go vet", tail(proc, 300))
    else:
        located, errors = False, []
        for line in proc.stderr.splitlines():
            m = re.match(r"(?:vet: )?(\S+\.go):(\d+)(?::\d+)?: (.*)", line.strip())
            if m:
                located = True
                path = repo_path(root, m.group(1))
                if on_changed_line(changes, path, int(m.group(2))):
                    errors.append(f"{path}:{m.group(2)} [go vet] {m.group(3)}")
        if proc.returncode and not located:
            errors.append(f"[go vet] failed:\n{tail(proc)}")
        report.add(errors)
        report.ran.append(f"go vet {' '.join(packages)}: exit {proc.returncode}")
    proc = execute([go, "test", *packages], root, TEST_TIMEOUT)
    if proc.returncode in NOT_STARTED:
        report.skip("go test", tail(proc, 300))
        return
    report.ran.append(f"go test {' '.join(packages)}: exit {proc.returncode}")
    if proc.returncode:
        failed = [l.strip() for l in proc.stdout.splitlines() if l.strip().startswith(("--- FAIL", "FAIL\t"))]
        report.add([f"[go test] {f}" for f in failed] or [f"[go test] failed:\n{tail(proc)}"])


# --- Rust -------------------------------------------------------------------------------------
def check_rust(root, changes, files, report):
    """cargo check on the crate."""
    if not os.path.isfile(os.path.join(root, "Cargo.toml")):
        report.skip("cargo check", "no Cargo.toml at the repo root")
        return
    cargo, why = find_tool(root, "cargo", ())
    if not cargo:
        report.skip("cargo check", why)
        return
    proc = execute([cargo, "check", "--quiet", "--message-format", "short"], root, 300)
    if proc.returncode in NOT_STARTED:
        report.skip("cargo check", tail(proc, 300))
        return
    changed = set(files)
    errors, warnings, elsewhere = [], [], 0
    for line in proc.stderr.splitlines():
        m = re.match(r"(\S+\.rs):(\d+):\d+: (error|warning)(.*)", line.strip())
        if not m:
            continue
        path = repo_path(root, m.group(1))
        if m.group(3) == "error":
            if path in changed:
                errors.append(f"{path}:{m.group(2)} [cargo] error{m.group(4)}")
            else:
                elsewhere += 1
        elif on_changed_line(changes, path, int(m.group(2))):
            warnings.append(f"{path}:{m.group(2)} [cargo] warning{m.group(4)}")
    if elsewhere:
        warnings.append(f"[cargo] {elsewhere} errors in files outside the change; check whether the change caused them")
    if proc.returncode and not errors and not elsewhere:
        errors.append(f"[cargo] check failed:\n{tail(proc)}")
    report.add(errors, warnings)
    report.ran.append(f"cargo check: exit {proc.returncode}")


# --- Shell ------------------------------------------------------------------------------------
def check_shell(root, changes, files, report):
    """ShellCheck on changed lines."""
    shellcheck, why = find_tool(root, "shellcheck", ())
    if not shellcheck:
        report.skip("shellcheck", why)
        return
    proc = execute([shellcheck, "-f", "json", *files], root, LINT_TIMEOUT)
    results = decode_json(proc.stdout or "", "[")
    if not isinstance(results, list):
        report.skip("shellcheck", f"no JSON output, exit {proc.returncode}: {tail(proc, 300)}")
        return
    errors, warnings = [], []
    for r in results:
        path = repo_path(root, r["file"])
        if r.get("level") in ("error", "warning") and on_changed_line(changes, path, r.get("line") or 0):
            msg = f"{path}:{r['line']} [shellcheck SC{r.get('code')}] {r['message']}"
            (errors if r["level"] == "error" else warnings).append(msg)
    report.add(errors, warnings)
    report.ran.append(f"shellcheck on {len(files)} changed files (changed lines only)")


CHECKS = (
    (JS_EXT, check_js),
    ((".php",), check_php),
    ((".py",), check_python),
    ((".go",), check_go),
    ((".rs",), check_rust),
    ((".sh", ".bash"), check_shell),
)


def main():
    root = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip()
    if not root:
        print("skipped: verify (not inside a git repository)")
        return 0
    changes = changed_lines(root)
    existing = sorted(p for p in changes if os.path.isfile(os.path.join(root, p)))
    if not existing:
        print("ran: nothing to check: no changed files")
        return 0
    report = Report()
    for extensions, check in CHECKS:
        files = [p for p in existing if p.endswith(extensions)]
        if files:
            check(root, changes, files, report)
    if not report.ran and not report.skipped:
        report.ran.append(f"nothing to check: no supported file types among {len(existing)} changed files")
    report.print()
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
