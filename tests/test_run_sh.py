#!/usr/bin/env python3
"""tests/run.sh --only: each section runs only the tests it names, and a name nothing ran fails the run."""
import kit_home
import ast
import os
import re
import subprocess
import sys
import tempfile

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
RUN_SH = os.path.join(TESTS_DIR, "run.sh")
TITLES = dict(re.findall(r'"([a-z-]+)\|([^"]+)"', open(RUN_SH).read()))
RESULTS = []
# One test per file whose tests are named functions, by section; hooks holds two such files.
PICKED = {
    "hooks": ["only_the_user_runs_the_switch_cli", "pr_text_is_checked_before_gh"],
    "generic-verify": ["no_changes_is_nothing_to_check"],
    "phase": ["status_line_names_the_branch"],
    "quality-log": ["refuses_malformed_entries"],
    "test-types": ["counts_each_canonical_type"],
    "pr-comments": ["none_new_says_so"],
    "free-branch": ["leaves_the_main_checkout_alone"],
    "prune-merged": ["a_failed_fetch_removes_nothing"],
    "pr-validation": ["turns_tabs_into_spaces"],
    "decision-report": ["a_template_heading_is_not_the_first_paragraph"],
    "approve": ["names_the_merge_and_protected_push_approvals"],
    "spec": ["lint_exits_2_on_an_unreadable_spec"],
    "build-ab": ["a_repo_without_a_name_is_refused"],
    "verify-changed": ["files_outside_cwd_are_not_linted"],
    "structure-facts": ["outside_a_checkout_exits_2"],
    "outcomes": ["signal_cells"],
    "review-eval": ["refuses_a_limit_below_one"],
    "sessions": ["short_gaps_add_up"],
}


def suite_env(**extra):
    env = {**os.environ, **extra}
    # Inherited, the nested run.sh would delete the HOME of the run that started this test when it exits.
    env.pop("AGENTS_SUITE_OWN_HOME", None)
    return env


def run_sh(*args, **env):
    done = subprocess.run(["bash", RUN_SH, *args], capture_output=True, text=True, errors="replace",
                          env=suite_env(**env), timeout=900)
    return done.returncode, done.stdout + done.stderr


def blocks(out):
    """Each `== <title>` block's lines, by title."""
    found, title = {}, None
    for line in out.splitlines():
        if line.startswith("== "):
            title = line[3:]
            found[title] = []
        elif title is not None:
            found[title].append(line)
    return found


def named_lines(out, section):
    """The ok, FAIL and skip lines in a section's block."""
    lines = blocks(out).get(TITLES[section], [])
    return [line for line in lines if line.startswith(("ok   ", "FAIL ", "skip "))]


def phase_tests():
    tree = ast.parse(open(os.path.join(TESTS_DIR, "test_phase.py")).read())
    tests = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "TESTS")
    return [element.id for element in tests.elts]


def the_helper_keeps_only_the_named_tests():
    called = []

    def kept():
        called.append("kept")

    def dropped():
        called.append("dropped")

    def also_kept():
        called.append("also_kept")

    before = os.environ.get("AGENTS_SUITE_ONLY")
    try:
        os.environ["AGENTS_SUITE_ONLY"] = "kept,also_kept"
        for picked in kit_home.selected((kept, dropped, also_kept)):
            picked()
        assert called == ["kept", "also_kept"], called
        os.environ["AGENTS_SUITE_ONLY"] = "also_kept"
        assert kit_home.selected((kept, dropped, also_kept)) == [also_kept], "a name matched part of another"
        os.environ.pop("AGENTS_SUITE_ONLY")
        assert kit_home.selected((kept, dropped)) == [kept, dropped]
    finally:
        if before is None:
            os.environ.pop("AGENTS_SUITE_ONLY", None)
        else:
            os.environ["AGENTS_SUITE_ONLY"] = before


def each_harness_runs_only_the_named_test():
    names = [name for picked in PICKED.values() for name in picked]
    code, out = run_sh("--only", ",".join(names), *PICKED)
    assert code == 0, out
    for section, picked in PICKED.items():
        assert sorted(named_lines(out, section)) == sorted(f"ok   {name}" for name in picked), (section, out)


def two_names_run_both():
    code, out = run_sh("--only", "status_line_names_the_branch,reports_path_takes_several_kinds", "phase")
    assert code == 0, out
    assert sorted(named_lines(out, "phase")) == ["ok   reports_path_takes_several_kinds", "ok   status_line_names_the_branch"], out
    assert "2/2 passed" in blocks(out)[TITLES["phase"]], out


def an_unknown_name_fails_the_run():
    code, out = run_sh("--only", "status_line_names_the_branch,no_such_test", "phase")
    assert code != 0, out
    assert named_lines(out, "phase") == ["ok   status_line_names_the_branch"], out
    assert "FAIL no test named no_such_test in phase" in out.splitlines(), out


def a_name_one_file_of_a_section_holds_is_enough():
    code, out = run_sh("--only", "kit_prs_are_checked", "hooks")
    assert code == 0, out
    assert named_lines(out, "hooks") == ["ok   kit_prs_are_checked"], out
    assert "no test named" not in out, out


def usage_errors_stop_before_any_section():
    for args in (["--only", "a"], ["--only", "", "phase"], ["--only", "a,,b", "phase"], ["--only", ",", "phase"],
                 ["--only", "a,", "phase"], ["--skip", "x", "--only", "a"], ["--only"],
                 ["--skip", "--only", "a", "phase"], ["--only", "--skip", "phase"], ["--only", "a\nb", "phase"],
                 ["--only", "a b", "phase"]):
        code, out = run_sh(*args)
        assert code == 2, (args, code, out)
        assert "== " not in out and "unknown section" not in out and "--only" in out, (args, out)
    code, out = run_sh("--skip", "phase", "--only", "a")
    assert code == 2 and "--skip" in out and "unknown section" not in out, out


def without_only_every_test_runs_whatever_the_environment_holds():
    code, out = run_sh("phase", AGENTS_SUITE_ONLY="status_line_names_the_branch")
    assert code == 0, out
    assert sorted(named_lines(out, "phase")) == sorted(f"ok   {name}" for name in phase_tests()), out


def only_survives_the_home_re_execution():
    with tempfile.TemporaryDirectory() as home:
        code, out = run_sh("--only", "status_line_names_the_branch", "phase", HOME=home,
                           AGENTS_SUITE_ONLY="walks_the_flow")
    assert code == 0, out
    assert named_lines(out, "phase") == ["ok   status_line_names_the_branch"], out


def every_harness_with_named_tests_applies_the_selection():
    for name in sorted(os.listdir(TESTS_DIR)):
        if re.fullmatch(r"test_.*\.py", name):
            source = open(os.path.join(TESTS_DIR, name)).read()
            if re.search(r"^(for test in |for t in |TESTS = )", source, re.M):
                assert re.search(r"^\s*(for test in |for t in |TESTS = )kit_home\.selected\(", source, re.M), \
                    f"{name} loops over named tests without kit_home.selected"


def a_child_runs_its_named_test_whatever_the_selection():
    for harness, name, other in (("test_phase.py", "status_line_names_the_branch", "walks_the_flow"),
                                 ("test_hooks.py", "only_the_user_runs_the_switch_cli", "stamp_flags_take_an_equals_sign")):
        done = subprocess.run([sys.executable, os.path.join(TESTS_DIR, harness), name], capture_output=True, text=True,
                              errors="replace", env=suite_env(AGENTS_SUITE_ONLY=other), timeout=600)
        assert done.returncode == 0 and f"ok   {name}" in done.stdout.splitlines(), (harness, done.stdout, done.stderr)


for test in kit_home.selected((the_helper_keeps_only_the_named_tests, usage_errors_stop_before_any_section,
                               each_harness_runs_only_the_named_test, two_names_run_both, an_unknown_name_fails_the_run,
                               a_name_one_file_of_a_section_holds_is_enough,
                               without_only_every_test_runs_whatever_the_environment_holds,
                               a_child_runs_its_named_test_whatever_the_selection,
                               only_survives_the_home_re_execution,
                               every_harness_with_named_tests_applies_the_selection)):
    try:
        test()
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {str(error)[:3000]}"))

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
sys.exit(1 if any(error for _, error in RESULTS) else 0)
