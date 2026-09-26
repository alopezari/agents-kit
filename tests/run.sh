#!/bin/bash
# Regression suite for the kit. Exit 0 only when everything passes.
#   ~/.agents/tests/run.sh
cd "$(dirname "$0")" || exit 2
fail=0
section() { printf '\n== %s\n' "$1"; }

section "hooks"
python3 test_hooks.py || fail=1

section "generic verify"
python3 test_verify_auto.py || fail=1

section "gh proxy wrapper"
bash test_gh_wrapper.sh || fail=1

section "harness baselines"
python3 test_baseline.py || fail=1

section "semgrep rules"
got=$(cd fixtures && semgrep --config ~/.agents/repos/_shared/wordpress.semgrep.yml --metrics=off --disable-version-check \
  --json wordpress-rules.php update-option.php 2>/dev/null | jq -r '.results[] | "\(.path):\(.start.line) \(.check_id|split(".")|last)"' | sort)
if [ "$got" = "$(sort fixtures/semgrep-expected.txt)" ]; then echo "ok   rules match fixtures"
else echo "FAIL rules differ from fixtures:"; diff <(sort fixtures/semgrep-expected.txt) <(echo "$got"); fail=1; fi

section "flow phase"
python3 test_phase.py || fail=1

section "browser A/B harness"
python3 test_browse.py || fail=1

section "verify on changed lines"
python3 test_verify_changed.py || fail=1

section "dependencies"
python3 test_deps.py || fail=1

section "outcomes and escapes"
python3 test_outcomes.py || fail=1

section "skill frontmatter"
node test_skill_frontmatter.mjs || fail=1

section "pi adapter"
node test_pi_adapter.mts 2>/dev/null || fail=1

section "triage"
if ~/.agents/bin/triage --json --range HEAD~1..HEAD 2>/dev/null | jq -e '.tier and .lenses.correctness' >/dev/null; then
  echo "ok   triage produces a tier and lenses"
else echo "FAIL triage"; fail=1; fi

section "framework reference"
~/.agents/bin/docs --check || fail=1
node ~/.agents/tools/mermaid/check.mjs ~/.agents/docs/framework.md || fail=1

section "site build"
if [ ! -d ~/.agents/site/node_modules ]; then echo "FAIL site dependencies missing: run install.sh"; fail=1
elif node ~/.agents/site/build.mjs >/dev/null; then echo "ok   landing and docs build from the current sources"
else fail=1; fi

section "install on a new machine"
bash test_install.sh || fail=1

section "install doctor"
doctor=$(~/.agents/install.sh --doctor 2>&1); doctor_status=$?
warnings=$(grep "  warn " <<<"$doctor")
if [ $doctor_status = 0 ] && [ -z "$warnings" ]; then echo "ok   no warnings"
else
  [ -n "$warnings" ] && echo "$warnings"
  [ $doctor_status = 0 ] || echo "FAIL install.sh --doctor exited $doctor_status: $(tail -3 <<<"$doctor")"
  fail=1
fi

echo
[ $fail = 0 ] && echo "ALL PASSED" || echo "FAILURES ABOVE"
exit $fail
