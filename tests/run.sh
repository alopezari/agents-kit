#!/bin/bash
# Regression suite for the kit. Exit 0 only when everything passes.
#   ~/.agents/tests/run.sh
cd "$(dirname "$0")" || exit 2
fail=0
section() { printf '\n== %s\n' "$1"; }
# A hook run by a test must log into the test's own place (its HOME, or AGENTS_LOG_DIR), never into the real log the
# monthly job reads: count the real log's lines from a checkout under the temp dir, before and after.
hook_log="$HOME/.agents/logs/hooks.jsonl"
tmp_root="$(python3 -c 'import os, tempfile; print(os.path.realpath(tempfile.gettempdir()))')"
from_tests() { python3 - "$hook_log" "$tmp_root" <<'PY'
import json, os, sys
log, root = sys.argv[1:]
lines = open(log, errors="replace").read().splitlines() if os.path.exists(log) else []
count = 0
for line in lines:
    try:
        cwd = os.path.realpath(str(json.loads(line).get("cwd") or ""))
    except ValueError:
        continue  # a line cut by a crash, or still being appended
    count += os.path.commonpath([cwd, root]) == root
print(count)
PY
}
logged_before=$(from_tests) || { echo "FAIL couldn't read $hook_log"; exit 1; }

section "hooks"
python3 test_hooks.py || fail=1
python3 test_private_terms.py || fail=1

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

section "quality log input"
python3 test_quality_log.py || fail=1

section "pull request comments not handled yet"
python3 test_pr_comments.py || fail=1

section "pull request validation section"
python3 test_pr_validation.py || fail=1

section "spec lint and second reading"
python3 test_spec.py || fail=1

section "browser A/B harness"
python3 test_browse.py || fail=1

section "verify on changed lines"
python3 test_verify_changed.py || fail=1

section "dependencies"
python3 test_deps.py || fail=1

section "outcomes and escapes"
python3 test_outcomes.py || fail=1

section "sessions and phases"
python3 test_extract_sessions.py || fail=1

section "skill frontmatter"
node test_skill_frontmatter.mjs || fail=1

section "pi adapter"
node test_pi_adapter.mts 2>/dev/null || fail=1

section "triage"
if ~/.agents/bin/triage --json --range HEAD~1..HEAD 2>/dev/null | jq -e '.tier and .lenses.correctness' >/dev/null; then
  echo "ok   triage produces a tier and lenses"
else echo "FAIL triage"; fail=1; fi
python3 test_triage.py || fail=1

section "CI wait"
python3 test_ci_wait.py || fail=1

section "framework reference"
~/.agents/bin/docs --check || fail=1
node ~/.agents/tools/mermaid/check.mjs ~/.agents/docs/framework.md || fail=1

section "site build"
if [ ! -d ~/.agents/site/node_modules ]; then echo "FAIL site dependencies missing: run install.sh"; fail=1
elif node ~/.agents/site/build.mjs >/dev/null; then echo "ok   landing and docs build from the current sources"
else fail=1; fi

section "version"
version=$(cat ~/.agents/VERSION)
if [[ $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
  && grep -qxE "## \[${version//./\\.}\] - [0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])" ~/.agents/CHANGELOG.md; then
  echo "ok   VERSION $version has its CHANGELOG entry"
else echo "FAIL VERSION ($version) needs a '## [$version] - <date>' heading in CHANGELOG.md"; fail=1; fi

bash test_changelog_check.sh || fail=1

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

section "real hook log untouched"
if ! logged_after=$(from_tests); then
  echo "FAIL couldn't read $hook_log"; fail=1
elif [ $(( logged_after - logged_before )) -gt 0 ]; then
  logged=$(( logged_after - logged_before ))
  echo "FAIL $logged lines from tests landed in $hook_log: give the test its own HOME or AGENTS_LOG_DIR"; fail=1
else echo "ok   no test logged into $hook_log"; fi

echo
[ $fail = 0 ] && echo "ALL PASSED" || echo "FAILURES ABOVE"
exit $fail
