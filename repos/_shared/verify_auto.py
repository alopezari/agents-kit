#!/usr/bin/env python3
"""Stack-detecting verify for repos without a hand-written ~/.agents/repos/<repo>/verify.

Runs from the repo root at the end of every agent turn, so it only looks at changed files
and only runs tests related to them, never full suites. Output follows verify_changed.py:
`ran: ` (what executed), `warning: `, `error: `, plus `skipped: <check> (<reason>)` for
checks that don't apply or whose tool is missing. Exit 1 only for findings on changed
lines, failing related tests, or changed tests that pass without the change; a skipped
check never fails the run.

The red check: when a change edits both tests and code of a stack (pytest, PHPUnit, or the
package.json `test` script), the changed tests also run on a pristine copy of the
merge-base, where at least one must fail (for PHPUnit, a case of a changed test class).

Diff coverage: the related pytest run goes through coverage.py when the Python behind pytest
is known and imports it (and not pytest-cov), the related PHPUnit run writes a Clover report
when php loads PCOV with pcov.enabled on or Xdebug, and, in a project without Vitest or Jest, the
red check's working-tree `npm test` collects V8 coverage for node_modules/.bin/c8 to report,
in every case only for the changed non-test files. `ran: diff coverage` gives how many changed
executable lines the tests ran and which they didn't (for c8, every line of a loaded file but blank
and `//` lines); it is report-only, and what keeps it from measuring is a `skipped:` line.
"""
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

_spec = importlib.util.spec_from_file_location(
    "verify_changed", os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify_changed.py"))
verify_changed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verify_changed)
changed_lines, on_changed_line, execute, TEST_FILE, tests_pass_without_change, diff_coverage = (
    verify_changed.changed_lines, verify_changed.on_changed_line, verify_changed.execute, verify_changed.TEST_FILE,
    verify_changed.tests_pass_without_change, verify_changed.diff_coverage)

MAX_PER_CHECK = 30
LINT_TIMEOUT = 120
TEST_TIMEOUT = 300
RED_CHECK_TIMEOUT = 120  # per stack: npm test's two runs (working tree, then the copy) share it
NOT_STARTED = (124, 127)  # execute(): timed out, binary missing
COVERAGE_PROBE_TIMEOUT = 20
MAX_COVERAGE_FILES = 50  # an --include per file: bounds the argv and the tool's path matching
JS_EXT = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
PHPCS_CONFIGS = ("phpcs.xml", "phpcs.xml.dist", ".phpcs.xml", ".phpcs.xml.dist")
PHPSTAN_CONFIGS = ("phpstan.neon", "phpstan.neon.dist")
PY_TEST_FILE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")
JS_TEST_FILE = re.compile(r"(^|/)__tests__/|\.(test|spec)\.\w+$")
# Changed files the changed tests may need in the pristine copy: fixtures, helpers, conftest.py.
TEST_SUPPORT = re.compile(r"(^|/)(tests?|__tests__|__snapshots__)/|(^|/)conftest\.py$")
NPM_PLACEHOLDER_TEST = "Error: no test specified"
JS_FAILURE_SUMMARY = re.compile(
    r"^\s*(?:(?:Tests|Test Suites|Test Files):?\s+[1-9]\d* failed|[1-9]\d* failing|[#ℹ] fail [1-9]\d*)", re.M)
ANSI_COLOR = re.compile(r"\x1b\[[0-9;]*m")
SHEBANG_PYTHON = re.compile(r"python\d*(\.\d+)?")


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
    return verify_changed.git(["ls-files", "--cached", "--others", "--exclude-standard"], root).splitlines()


# --- Red check --------------------------------------------------------------------------------
def red_check_scope(root, changes, files, test_file):
    """(changed test files to run, changed files to copy into the pristine copy) when the change edits both tests
    and code of a stack; None for a tests-only or code-only change."""
    tests = [f for f in files if test_file.search(f)]
    if not tests or all(f in tests or TEST_SUPPORT.search(f) for f in files):
        return None
    support = {p for p in changes if TEST_SUPPORT.search(p) and os.path.isfile(os.path.join(root, p))}
    return tests, sorted(support | set(tests))


# --- Diff coverage ----------------------------------------------------------------------------
def coverage_candidates(files, test_file, stack, report):
    """The changed files diff coverage measures, or None, skipped with the reason: tests and test support say nothing
    about the change."""
    candidates = [f for f in files if not test_file.search(f) and not TEST_SUPPORT.search(f)]
    if not candidates:
        report.skip("diff coverage", f"no changed non-test {stack} file to measure")
    elif len(candidates) > MAX_COVERAGE_FILES:
        report.skip("diff coverage", f"{len(candidates)} changed {stack} files to measure, "
                                     f"more than {MAX_COVERAGE_FILES}")
    else:
        return candidates
    return None


def report_diff_coverage(changes, tool, candidates, lines_by_file, report):
    summary, reason = diff_coverage(changes, candidates, lines_by_file)
    if summary:
        report.ran.append(f"diff coverage ({tool}): {summary}")
    else:
        report.skip("diff coverage", f"{tool}: {reason}")


def remaining(deadline):
    return max(1, round(deadline - time.monotonic()))


def c8_report(root, changes, c8, candidates, raw, report):
    """Diff coverage from the V8 coverage a test run left in `raw`, through `c8 report`. Flags override a project's
    c8 config that would fail the step (check-coverage, 100), drop fully covered files (skip-full) or report files the
    run never loaded (all)."""
    reports_dir = os.path.dirname(raw)
    try:
        step = execute([c8, "report", f"--temp-directory={raw}", f"--reports-dir={reports_dir}", "--reporter=json",
                        "--check-coverage=false", "--100=false", "--skip-full=false",
                        "--all=false", *(f"--include={f}" for f in candidates)], root, LINT_TIMEOUT)
    except OSError as err:  # execute() turns only a missing binary into 127
        report.skip("diff coverage", f"c8 report did not start: {err}")
        return
    final = os.path.join(reports_dir, "coverage-final.json")
    if not os.path.exists(final):
        report.skip("diff coverage", f"c8 report wrote no JSON report (exit {step.returncode}): {tail(step, 300)}")
        return
    try:
        with open(final) as fh:
            lines_by_file = {}
            for path, entry in json.load(fh).items():
                statements = [(int(loc["start"]["line"]), entry["s"][sid])
                              for sid, loc in entry["statementMap"].items()]
                lines_by_file[repo_path(root, entry.get("path") or path)] = (
                    {line for line, count in statements if count}, {line for line, _ in statements})
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as err:
        report.skip("diff coverage", f"c8: the JSON report is unreadable: {err!r}")
        return
    for path in candidates:
        if path not in lines_by_file:
            continue
        # v8-to-istanbul makes a statement of every line of a loaded file, blank and comment lines included.
        try:
            with open(os.path.join(root, path), errors="replace") as fh:
                prose = {n for n, text in enumerate(fh, 1) if not text.strip() or text.lstrip().startswith("//")}
        except OSError:
            continue
        for lines in lines_by_file[path]:
            lines -= prose
    report_diff_coverage(changes, "c8", candidates, lines_by_file, report)


# --- JS / TS ----------------------------------------------------------------------------------
def check_js(root, changes, files, report):
    """ESLint on changed lines; Vitest or Jest on the tests related to the changed files; the red check through
    the package.json `test` script; diff coverage of that script's run through c8."""
    if not os.path.isfile(os.path.join(root, "package.json")):
        report.skip("eslint", "no package.json at the repo root")
        report.skip("diff coverage", "no related JS test run in the working tree")
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
        report.skip("diff coverage", "no related JS test run in the working tree")
        return
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    runners = [(name, args) for name, args in (
        ("vitest", ["related", "--run", "--passWithNoTests", *files]),
        ("jest", ["--findRelatedTests", *files, "--passWithNoTests", "--ci"])) if name in deps]
    measure, c8 = None, os.path.join(root, "node_modules/.bin/c8")
    if runners:
        # Seen with vitest 4: its forks pool leaves the changed file out of the report, its threads pool counts
        # every line as run.
        report.skip("diff coverage", f"{runners[0][0]} is a dependency: c8 can't map its runs, whose workers load "
                                     "the changed files through their own transform")
    elif candidates := coverage_candidates(files, JS_TEST_FILE, "JS/TS", report):
        if os.access(c8, os.X_OK):
            measure = (c8, candidates)
        else:
            report.skip("diff coverage", "c8 not installed in node_modules/.bin")
    if not npm_test_red_check(root, changes, files, pkg, measure, report) and measure:
        report.skip("diff coverage", "no related JS test run in the working tree")
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


def workspace_link(root):
    """A node_modules entry (the root's or a package's) linked to a directory of the repo outside node_modules (a
    workspace package), or None. Through it, the pristine copy would run the working tree's code."""
    real_root = os.path.realpath(root)
    ignored = verify_changed.git(["ls-files", "--others", "--ignored", "--exclude-standard", "--directory"], root)
    # A node_modules with a tracked file (.gitkeep) is listed by its ignored children (node_modules/@acme/).
    parts = [rel.rstrip("/").split("/") for rel in ignored.splitlines()]
    dirs = {"/".join(p[:len(p) - p[::-1].index("node_modules")]) for p in parts if "node_modules" in p}
    for rel in sorted(dirs):
        modules = os.path.join(root, rel)
        real_modules = os.path.realpath(modules)
        try:
            names = os.listdir(modules)
            names += [f"{scope}/{name}" for scope in names
                      if scope.startswith("@") and os.path.isdir(os.path.join(modules, scope))
                      for name in os.listdir(os.path.join(modules, scope))]
        except OSError:
            continue
        for name in names:
            path = os.path.join(modules, name)
            target = os.path.realpath(path)
            if (os.path.islink(path) and os.path.commonpath([target, real_root]) == real_root
                    and os.path.commonpath([target, real_modules]) != real_modules):
                return os.path.join(rel, name)
    return None


def npm_test_red_check(root, changes, files, pkg, measure, report):
    """`npm test -- <changed test files>` in the working tree, collecting V8 coverage for c8 when `measure` is (c8,
    the files to report), then on the merge-base, where it must fail. Returns whether it ran the working tree's."""
    scope = red_check_scope(root, changes, files, JS_TEST_FILE)
    if not scope:
        return False
    tests, copied = scope
    script = (pkg.get("scripts") or {}).get("test")
    if not script or NPM_PLACEHOLDER_TEST in script:
        report.skip("red check", "npm test: package.json has no test script")
        return False
    npm = shutil.which("npm")
    if not npm:
        report.skip("red check", "npm not on PATH")
        return False
    try:
        link = workspace_link(root)
    except RuntimeError as err:
        report.skip("red check", f"npm test: {err}")
        return False
    if link:
        report.skip("red check", f"npm test: {link} links into the repo, so the merge-base copy would "
                                 "run the change through it")
        return False
    # CI=true: Jest writes no new snapshots into the working tree, and watch-mode scripts exit instead of waiting.
    env, deadline = {**os.environ, "CI": "true"}, time.monotonic() + RED_CHECK_TIMEOUT
    command = [npm, "test", "--", *tests]
    tmp = None
    if measure:
        try:
            tmp = tempfile.mkdtemp(prefix="agents-coverage-")
            os.mkdir(os.path.join(tmp, "raw"))
        except OSError as err:
            report.skip("diff coverage", f"c8: cannot create a temp dir: {err}")
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
            tmp = None
    raw = tmp and os.path.join(tmp, "raw")
    try:
        proc = execute(command, root, RED_CHECK_TIMEOUT, {**env, "NODE_V8_COVERAGE": raw} if raw else env)
        npm_red_check_on_base(root, command, tests, copied, proc, deadline, env, report)
        # After the merge-base run, with a budget of its own: a slow report never eats the red check's.
        if raw and proc.returncode in NOT_STARTED:
            report.skip("diff coverage", f"c8: npm test gave no verdict: {tail(proc, 300)}")
        elif raw:
            c8_report(root, changes, *measure, raw, report)
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
    return True


def npm_red_check_on_base(root, command, tests, copied, proc, deadline, env, report):
    """The red check's verdict from the working tree's run `proc`, then the same command on the merge-base."""
    if proc.returncode:
        reason = tail(proc, 300) if proc.returncode in NOT_STARTED else \
            f"the changed tests fail in the working tree (exit {proc.returncode})"
        report.skip("red check", f"npm test: {reason}")
        return
    try:
        with verify_changed.pristine_copy(root, copied) as copy:
            proc = execute(command, copy, remaining(deadline), env)
    except (OSError, subprocess.SubprocessError, RuntimeError) as err:
        report.skip("red check", f"npm test: could not build or run in the pristine copy: {err}")
        return
    if proc.returncode in NOT_STARTED:
        report.skip("red check", f"npm test: {tail(proc, 300)}")
    elif not proc.returncode:
        report.add([tests_pass_without_change(tests)])
    elif JS_FAILURE_SUMMARY.search(ANSI_COLOR.sub("", proc.stdout + proc.stderr)):
        report.ran.append(f"red check: npm test on {len(tests)} changed test files fails on the merge-base, "
                          f"passes with the change ({', '.join(tests)})")
    else:
        report.skip("red check", f"npm test: exit {proc.returncode} on the merge-base with no test failure summary: "
                                 f"{tail(proc, 300)}")


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
    """php -l; PHPCS and PHPStan on changed lines when the project configures them; PHPUnit on related test classes,
    then the red check on the changed ones; diff coverage of that PHPUnit run through PCOV or Xdebug."""
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
    scope = red_check_scope(root, changes, files, TEST_FILE)
    classes = related_php_tests(root, files)
    if not classes:
        report.skip("phpunit", "no related FooTest.php under tests/ or test/")
        report.skip("diff coverage", "no related PHPUnit run in the working tree")
        return
    if not os.access(phpunit, os.X_OK):
        report.skip("phpunit", "phpunit not installed in vendor/bin")
        report.skip("diff coverage", "no related PHPUnit run in the working tree")
        if scope:
            report.skip("red check", "phpunit: not installed in vendor/bin")
        return
    proc, cases = run_phpunit(root, changes, php, phpunit, phpunit_filter(classes),
                              coverage_candidates(files, TEST_FILE, "PHP", report), report)
    if cases is None:
        # Suites that need a database or WordPress often can't start here; that's setup, not a failure.
        report.skip("phpunit", f"no JUnit report, PHPUnit did not start (exit {proc.returncode}): {tail(proc, 300)}")
        if scope:
            report.skip("red check", "phpunit: the related run in the working tree did not start")
        return
    failed = [f"{c.get('class') or c.get('classname')}::{c.get('name')}" for c in cases if failed_case(c)]
    report.ran.append(f"phpunit --filter {', '.join(classes)}: {len(cases)} tests, {len(failed)} failing")
    report.add([f"[phpunit] {t} fails" for t in failed])
    if scope and (failed or all(c.find("skipped") is not None for c in cases)):
        report.skip("red check", f"phpunit: the related run in the working tree didn't pass conclusively "
                                 f"({len(cases)} tests, {len(failed)} failing)")
    elif scope:
        phpunit_red_check(root, *scope, report)


def phpunit_filter(classes):
    return ["--filter", "/(" + "|".join(classes) + ")/"]


def failed_case(case):
    return case.find("failure") is not None or case.find("error") is not None


def junit_cases(junit):
    """The <testcase> elements of a JUnit report, or None when there is no readable report."""
    try:
        return list(ET.parse(junit).getroot().iter("testcase"))
    except (ET.ParseError, OSError):
        return None


def run_phpunit(root, changes, php, phpunit, args, candidates, report):
    """PHPUnit `args` in the working tree with a JUnit report, also writing a Clover report of the changed `candidates`
    when php can collect coverage (None: not measured); returns (the run whose verdict counts, its cases or None)."""
    with tempfile.TemporaryDirectory() as tmp:
        junit = os.path.join(tmp, "junit.xml")
        command = [phpunit, *args, "--log-junit", junit]
        env = php_coverage_env(root, php, candidates, tmp, report) if candidates else None
        if env is None:
            proc = execute(command, root, TEST_TIMEOUT)
            return proc, junit_cases(junit)
        deadline = time.monotonic() + TEST_TIMEOUT
        clover = os.path.join(tmp, "clover.xml")
        # PHPUnit 9.6 takes only directories here.
        filters = [a for d in sorted({os.path.dirname(f) for f in candidates})
                   for a in ("--coverage-filter", os.path.join(root, d))]
        proc = execute([*command, "--coverage-clover", clover, *filters], root, TEST_TIMEOUT, env)
        cases = junit_cases(junit)
        if proc.returncode == 124:
            report.skip("diff coverage", f"phpunit: {tail(proc, 300)}")
        elif cases is None:
            # A PHPUnit that rejects a coverage flag stops before running any test.
            report.skip("diff coverage", f"phpunit: no JUnit report with the coverage flags (exit {proc.returncode}), "
                                         f"so the tests ran again without them: {tail(proc, 300)}")
            proc = execute(command, root, remaining(deadline))
            cases = junit_cases(junit)
        else:
            php_clover_report(root, changes, clover, candidates, proc, report)
        return proc, cases


def php_coverage_env(root, php, candidates, ini_dir, report):
    """The environment a PHPUnit run collects coverage of `candidates` in, or None, skipped with the reason."""
    if not php:
        report.skip("diff coverage", "phpunit: php not on PATH")
        return None
    probe = execute([php, "-r", "echo json_encode([extension_loaded('pcov'), "
                                "filter_var(ini_get('pcov.enabled'), FILTER_VALIDATE_BOOLEAN), "
                                "extension_loaded('xdebug')]);"], root, COVERAGE_PROBE_TIMEOUT)
    facts = decode_json(probe.stdout or "", "[")
    if not (isinstance(facts, list) and len(facts) == 3):
        report.skip("diff coverage", f"phpunit: the coverage driver probe gave no answer (exit {probe.returncode}): "
                                     f"{tail(probe, 300)}")
        return None
    pcov, pcov_enabled, xdebug = facts
    if pcov and pcov_enabled:
        # Unset, pcov.directory is src/, lib/ or app/ when the root has one, and files outside it are never collected.
        directory = os.path.commonpath([os.path.realpath(os.path.join(root, f)) for f in candidates])
        directory = directory if os.path.isdir(directory) else os.path.dirname(directory)
        with open(os.path.join(ini_dir, "agents-pcov.ini"), "w") as fh:
            fh.write(f"pcov.directory={directory}\n")
        # The empty element keeps PHP's own scan directory, where PCOV is usually loaded from.
        return {**os.environ, "PHP_INI_SCAN_DIR": os.environ.get("PHP_INI_SCAN_DIR", "") + os.pathsep + ini_dir}
    if xdebug:
        return {**os.environ, "XDEBUG_MODE": "coverage"}
    report.skip("diff coverage", "phpunit: PCOV is loaded but pcov.enabled is off" if pcov else
                                 "phpunit: no coverage driver, install pcov or xdebug")
    return None


def php_clover_report(root, changes, clover, candidates, proc, report):
    if not os.path.exists(clover):
        report.skip("diff coverage", f"phpunit wrote no Clover report (exit {proc.returncode}): {tail(proc, 300)}")
        return
    try:
        lines_by_file = {}
        for entry in ET.parse(clover).getroot().iter("file"):
            statements = [(int(line.get("num")), int(line.get("count")))
                          for line in entry.iter("line") if line.get("type") == "stmt"]
            lines_by_file[repo_path(root, entry.get("name"))] = (
                {num for num, count in statements if count > 0}, {num for num, _ in statements})
    except (OSError, ET.ParseError, TypeError, ValueError) as err:
        report.skip("diff coverage", f"phpunit: the Clover report is unreadable: {err!r}")
        return
    report_diff_coverage(changes, "phpunit", candidates, lines_by_file, report)


def phpunit_red_check(root, tests, copied, report):
    """The changed test classes on the merge-base, through the copy's own vendor/bin/phpunit so Composer autoloads the
    copy: at least one of their cases must fail or error there. Another class the filter matches says nothing."""
    classes = sorted({os.path.splitext(os.path.basename(t))[0] for t in tests})
    try:
        with verify_changed.pristine_copy(root, copied) as copy:
            junit = os.path.join(copy, ".agents-red-junit.xml")
            # .phpunit.result.cache is a symlink into the working tree: don't write the base's results into it.
            proc = execute([os.path.join(copy, "vendor/bin/phpunit"), *phpunit_filter(classes), "--log-junit", junit,
                            "--do-not-cache-result"], copy, RED_CHECK_TIMEOUT)
            cases = junit_cases(junit)
    except (OSError, subprocess.SubprocessError, RuntimeError) as err:
        report.skip("red check", f"phpunit: could not build or run in the pristine copy: {err}")
        return
    if proc.returncode in NOT_STARTED:
        report.skip("red check", f"phpunit: {tail(proc, 300)}")
        return
    if cases is None:
        report.skip("red check", f"phpunit: no JUnit report, PHPUnit did not start in the pristine copy "
                                 f"(exit {proc.returncode}): {tail(proc, 300)}")
        return
    names = ", ".join(classes)
    own = [c for c in cases if re.split(r"[\\.]", c.get("class") or c.get("classname") or "")[-1] in classes]
    failed = [c for c in own if failed_case(c)]
    if not own:
        report.skip("red check", f"phpunit: no case of {names} in the JUnit report on the merge-base "
                                 f"({len(cases)} other cases)")
    elif not failed and all(c.find("skipped") is not None for c in own):
        report.skip("red check", f"phpunit: every case of {names} was skipped on the merge-base")
    elif not failed:
        report.add([tests_pass_without_change(tests)])
    else:
        report.ran.append(f"red check: phpunit on {len(classes)} changed test classes: {len(failed)} of {len(own)} "
                          f"cases fail on the merge-base, pass with the change")




# --- Python -----------------------------------------------------------------------------------
VENV_DIRS = (".venv/bin", "venv/bin")


def related_python_tests(root, files):
    tests = {f for f in files if PY_TEST_FILE.search(f)}
    modules = {os.path.splitext(os.path.basename(f))[0] for f in files if f not in tests}
    names = {f"test_{m}.py" for m in modules} | {f"{m}_test.py" for m in modules}
    tests.update(p for p in tracked_files(root) if os.path.basename(p) in names and os.path.isfile(os.path.join(root, p)))
    return sorted(tests)


def pytest_python(pytest, in_venv):
    """The Python that runs the pytest launcher, as an argv prefix: the python* its shebang names, with the shebang's
    flags, else the venv's python beside a project-local one. Returns (argv, None) or (None, why it is unknown)."""
    try:
        with open(pytest, "rb") as fh:
            first = fh.readline(256).decode(errors="replace").strip()
    except OSError as err:
        return None, f"coverage.py: cannot read {pytest}: {err}"
    interpreter, *flags = (first[2:].split() or [""]) if first.startswith("#!") else [""]
    if SHEBANG_PYTHON.fullmatch(os.path.basename(interpreter)) and os.access(interpreter, os.X_OK):
        return [interpreter, *flags], None
    beside = os.path.join(os.path.dirname(pytest), "python")
    if in_venv and os.access(beside, os.X_OK):
        return [beside], None
    return None, (f"coverage.py: the Python behind {pytest} is unknown: no venv python beside it, "
                  f"first line {first[:80]}")


def run_pytest(root, changes, pytest, in_venv, args, candidates, report):
    """pytest `args` in the working tree, under coverage.py when its Python is known and imports it, collecting the
    changed `candidates` (None: not measured); returns the run whose verdict counts."""
    if not candidates:
        return execute([pytest, *args], root, TEST_TIMEOUT)
    python, why = pytest_python(pytest, in_venv)
    # Exit 3: pytest-cov would collect into our data file, and --no-cov changes the verdict of a project whose
    # addopts set --cov-fail-under.
    try:
        probe = python and execute([*python, "-c", "import coverage, importlib.util, sys; "
                                    "sys.exit(3 if importlib.util.find_spec('pytest_cov') else 0)"],
                                   root, COVERAGE_PROBE_TIMEOUT)
    except OSError as err:  # execute() turns only a missing binary into 127
        python, why = None, f"coverage.py: {python[0]} did not start: {err}"
    if not python:
        report.skip("diff coverage", why)
    elif probe.returncode == 3:
        report.skip("diff coverage", f"coverage.py: pytest-cov is installed for {python[0]}")
    elif probe.returncode:
        report.skip("diff coverage", f"coverage.py is not importable by {python[0]}: {tail(probe, 300)}")
    if not python or probe.returncode:
        return execute([pytest, *args], root, TEST_TIMEOUT)
    deadline = time.monotonic() + TEST_TIMEOUT
    try:
        tmp = tempfile.mkdtemp(prefix="agents-coverage-")
    except OSError as err:
        report.skip("diff coverage", f"coverage.py: cannot create a temp dir: {err}")
        return execute([pytest, *args], root, TEST_TIMEOUT)
    try:
        return measured_pytest(root, changes, pytest, python, args, candidates, tmp, deadline, report)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measured_pytest(root, changes, pytest, python, args, candidates, tmp, deadline, report):
    # Our own rcfile: the project's coverage config (source, omit, plugins, fail_under) is not read.
    rcfile, data, json_report = (os.path.join(tmp, name) for name in ("coveragerc", ".coverage", "coverage.json"))
    include = "".join(f"\n    {os.path.join(root, f)}" for f in candidates)
    try:
        with open(rcfile, "w") as fh:
            fh.write(f"[run]\ninclude ={include}\ndisable_warnings = no-data-collected\n")
    except OSError as err:
        report.skip("diff coverage", f"coverage.py: cannot write its config: {err}")
        return execute([pytest, *args], root, TEST_TIMEOUT)
    env = {**os.environ, "COVERAGE_FILE": data}
    # The launcher script, not -m pytest, so sys.path is the one pytest has without coverage.
    proc = execute([*python, "-m", "coverage", "run", f"--rcfile={rcfile}", pytest, *args], root, TEST_TIMEOUT, env)
    if proc.returncode == 124:
        report.skip("diff coverage", f"coverage.py: {tail(proc, 300)}")
        return proc
    # coverage.py saves its data file even when none of the collected files loaded: a failed run without one
    # stopped before the tests.
    if proc.returncode and not os.path.exists(data):
        report.skip("diff coverage", f"coverage.py left no data file (exit {proc.returncode}), so the tests ran "
                                     f"again without it: {tail(proc, 300)}")
        return execute([pytest, *args], root, remaining(deadline))
    if proc.returncode not in (0, 1, 2):
        report.skip("diff coverage", f"coverage.py: pytest gave no verdict (exit {proc.returncode})")
        return proc
    if not os.path.exists(data):
        report.skip("diff coverage", "coverage.py left no data file")
        return proc
    step = execute([*python, "-m", "coverage", "json", f"--rcfile={rcfile}", "-o", json_report], root,
                   min(LINT_TIMEOUT, remaining(deadline)), env)
    # coverage json writes no report, and exits 1, when none of the collected files loaded.
    if not os.path.exists(json_report) and "No data to report." in step.stdout:
        report_diff_coverage(changes, "coverage.py", candidates, {}, report)
        return proc
    if not os.path.exists(json_report):
        report.skip("diff coverage", f"coverage.py wrote no JSON report (exit {step.returncode}): {tail(step, 300)}")
        return proc
    try:
        with open(json_report) as fh:
            lines_by_file = {}
            for path, entry in json.load(fh)["files"].items():
                executed = {int(line) for line in entry["executed_lines"]}
                missing = {int(line) for line in entry["missing_lines"]}
                lines_by_file[repo_path(root, path)] = (executed, executed | missing)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as err:
        report.skip("diff coverage", f"coverage.py: the JSON report is unreadable: {err!r}")
        return proc
    report_diff_coverage(changes, "coverage.py", candidates, lines_by_file, report)
    return proc


def check_python(root, changes, files, report):
    """Ruff on changed lines; pytest on the related test_<module>.py or <module>_test.py files, then the red check
    on the changed ones; diff coverage through coverage.py."""
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

    scope = red_check_scope(root, changes, files, PY_TEST_FILE)
    tests = related_python_tests(root, files)
    if not tests:
        report.skip("pytest", "no related test_<module>.py or <module>_test.py")
        report.skip("diff coverage", "no related pytest run in the working tree")
        return
    pytest, why = find_tool(root, "pytest", VENV_DIRS)
    if not pytest:
        report.skip("pytest", why)
        report.skip("diff coverage", "no related pytest run in the working tree")
        if scope:
            report.skip("red check", why)
        return
    proc = run_pytest(root, changes, pytest, why is None, ["-q", *tests],
                      coverage_candidates(files, PY_TEST_FILE, "Python", report), report)
    if proc.returncode == 5:
        report.ran.append(f"pytest on {len(tests)} related files: no tests collected")
    elif proc.returncode in (0, 1, 2):  # 1: tests failed, 2: collection error or interrupted
        report.ran.append(f"pytest on {len(tests)} related files: exit {proc.returncode}")
        if proc.returncode:
            failed = [l.strip() for l in proc.stdout.splitlines() if l.startswith(("FAILED ", "ERROR "))]
            report.add([f"[pytest] {f}" for f in failed] or [f"[pytest] related tests failed:\n{tail(proc)}"])
    else:
        report.skip("pytest", f"exit {proc.returncode}: {tail(proc, 300)}")
    if scope and proc.returncode:
        report.skip("red check", f"pytest: the related run in the working tree didn't pass (exit {proc.returncode})")
    elif scope:
        pytest_red_check(root, pytest, *scope, report)


def pytest_red_check(root, pytest, tests, copied, report):
    """The changed test files on the merge-base, with a JUnit report: at least one case must fail or error there."""
    try:
        with verify_changed.pristine_copy(root, copied) as copy:
            src = os.path.join(copy, "src")
            pythonpath = [copy, *([src] if os.path.isdir(src) else []), *filter(None, [os.environ.get("PYTHONPATH")])]
            junit = os.path.join(copy, ".agents-red-junit.xml")
            # .pytest_cache and __pycache__ are symlinks into the working tree: write neither from the base, but keep
            # the cache plugin, whose `cache` fixture the changed tests may use.
            proc = execute([pytest, "-q", "-o", "cache_dir=.agents-red-cache", f"--junitxml={junit}", *tests], copy,
                           RED_CHECK_TIMEOUT, {**os.environ, "PYTHONPATH": os.pathsep.join(pythonpath),
                                               "PYTHONDONTWRITEBYTECODE": "1"})
            try:
                cases = list(ET.parse(junit).getroot().iter("testcase"))
            except (ET.ParseError, OSError):
                cases = []
    except (OSError, subprocess.SubprocessError, RuntimeError) as err:
        report.skip("red check", f"pytest: could not build or run in the pristine copy: {err}")
        return
    if proc.returncode in NOT_STARTED:
        report.skip("red check", f"pytest: {tail(proc, 300)}")
        return
    if not cases:
        report.skip("red check", f"pytest: no JUnit report or no test cases in it (exit {proc.returncode}): {tail(proc, 300)}")
        return
    failed = [c for c in cases if c.find("failure") is not None or c.find("error") is not None]
    passed = [c for c in cases if c not in failed and c.find("skipped") is None]
    if not failed and (proc.returncode or not passed):
        report.skip("red check", f"pytest: no case failed or passed conclusively on the merge-base "
                                 f"(exit {proc.returncode}, {len(cases) - len(passed)} of {len(cases)} skipped)")
        return
    if not failed:
        report.add([tests_pass_without_change(tests)])
        return
    report.ran.append(f"red check: pytest on {len(tests)} changed test files: {len(failed)} of {len(cases)} cases fail "
                      f"on the merge-base, pass with the change")


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
    root = verify_changed.git(["rev-parse", "--show-toplevel"], None).strip()
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
