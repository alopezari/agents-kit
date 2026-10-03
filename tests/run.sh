#!/bin/bash
# Regression suite for the kit. Exit 0 only when everything passes.
#   ~/.agents/tests/run.sh                        every section
#   ~/.agents/tests/run.sh hooks install          only those sections
#   ~/.agents/tests/run.sh --skip hooks install   all but those (CI's third job, so a new section lands there)
# Sections run concurrently; each prints as one block, in this order, once it and every section above it have finished.
cd "$(dirname "$0")" || exit 2
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

# Each section is a function section_<name> (dashes as underscores) that returns non-zero on any failure.
sections=("hooks|hooks" "generic-verify|generic verify" "gh-wrapper|gh proxy wrapper" "baselines|harness baselines"
  "semgrep|semgrep rules" "phase|flow phase" "quality-log|quality log input"
  "pr-comments|pull request comments not handled yet" "free-branch|free a branch from its worktree"
  "pr-validation|pull request validation section" "spec|spec lint and second reading" "browse|browser A/B harness"
  "verify-changed|verify on changed lines" "deps|dependencies" "outcomes|outcomes and escapes"
  "sessions|sessions and phases" "frontmatter|skill frontmatter" "pi|pi adapter" "triage|triage" "ci-wait|CI wait"
  "docs|framework reference" "site|site build" "version|version" "install|install on a new machine"
  "doctor|install doctor")
names=("${sections[@]%%|*}")

section_hooks() { local f=0; python3 test_hooks.py || f=1; python3 test_private_terms.py || f=1; return $f; }
section_generic_verify() { python3 test_verify_auto.py; }
section_gh_wrapper() { bash test_gh_wrapper.sh; }
section_baselines() { python3 test_baseline.py; }
section_semgrep() {
  local got
  got=$(cd fixtures && semgrep --config ~/.agents/repos/_shared/wordpress.semgrep.yml --metrics=off --disable-version-check \
    --json wordpress-rules.php update-option.php 2>/dev/null | jq -r '.results[] | "\(.path):\(.start.line) \(.check_id|split(".")|last)"' | sort)
  if [ "$got" = "$(sort fixtures/semgrep-expected.txt)" ]; then echo "ok   rules match fixtures"
  else echo "FAIL rules differ from fixtures:"; diff <(sort fixtures/semgrep-expected.txt) <(echo "$got"); return 1; fi
}
section_phase() { python3 test_phase.py; }
section_quality_log() { python3 test_quality_log.py; }
section_pr_comments() { python3 test_pr_comments.py; }
section_free_branch() { python3 test_free_branch.py; }
section_pr_validation() { python3 test_pr_validation.py; }
section_spec() { python3 test_spec.py; }
section_browse() { python3 test_browse.py; }
section_verify_changed() { python3 test_verify_changed.py; }
section_deps() { python3 test_deps.py; }
section_outcomes() { python3 test_outcomes.py; }
section_sessions() { python3 test_extract_sessions.py; }
section_frontmatter() { node test_skill_frontmatter.mjs; }
section_pi() { node test_pi_adapter.mts 2>/dev/null; }
section_triage() {
  local f=0
  if ~/.agents/bin/triage --json --range HEAD~1..HEAD 2>/dev/null | jq -e '.tier and .lenses.correctness' >/dev/null; then
    echo "ok   triage produces a tier and lenses"
  else echo "FAIL triage"; f=1; fi
  python3 test_triage.py || f=1
  return $f
}
section_ci_wait() { python3 test_ci_wait.py; }
section_docs() {
  local f=0
  ~/.agents/bin/docs --check || f=1
  node ~/.agents/tools/mermaid/check.mjs ~/.agents/docs/framework.md || f=1
  return $f
}
section_site() {
  if [ ! -d ~/.agents/site/node_modules ]; then echo "FAIL site dependencies missing: run install.sh"; return 1
  elif node ~/.agents/site/build.mjs >/dev/null; then echo "ok   landing and docs build from the current sources"
  else return 1; fi
}
section_version() {
  local f=0 version
  version=$(cat ~/.agents/VERSION)
  if [[ $version =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] \
    && grep -qxE "## \[${version//./\\.}\] - [0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])" ~/.agents/CHANGELOG.md; then
    echo "ok   VERSION $version has its CHANGELOG entry"
  else echo "FAIL VERSION ($version) needs a '## [$version] - <date>' heading in CHANGELOG.md"; f=1; fi
  bash test_changelog_check.sh || f=1
  return $f
}
section_install() { bash test_install.sh; }
section_doctor() {
  local doctor doctor_status warnings
  doctor=$(~/.agents/install.sh --doctor 2>&1); doctor_status=$?
  warnings=$(grep "  warn " <<<"$doctor")
  if [ $doctor_status = 0 ] && [ -z "$warnings" ]; then echo "ok   no warnings"; return 0; fi
  [ -n "$warnings" ] && echo "$warnings"
  [ $doctor_status = 0 ] || echo "FAIL install.sh --doctor exited $doctor_status: $(tail -3 <<<"$doctor")"
  return 1
}

skip=0 asked=()
[ "${1-}" = --skip ] && { skip=1; shift; [ $# -gt 0 ] || { echo "--skip needs section names"; exit 2; }; }
for arg in "$@"; do
  known=0
  for name in "${names[@]}"; do [ "$arg" = "$name" ] && known=1; done
  [ $known = 1 ] || { echo "unknown section: $arg (sections: ${names[*]}; --skip goes first)"; exit 2; }
  asked+=("$arg")
done
picked=()
for i in "${!names[@]}"; do
  named=0
  for arg in "${asked[@]-}"; do [ "$arg" = "${names[$i]}" ] && named=1; done
  if [ ${#asked[@]} = 0 ] || [ $named != $skip ]; then picked+=("$i"); fi
done
[ ${#picked[@]} -gt 0 ] || { echo "no section left to run"; exit 2; }

logged_before=$(from_tests) || { echo "FAIL couldn't read $hook_log"; exit 1; }
out=$(mktemp -d "${TMPDIR:-/tmp}/kit-suite-XXXXXX") || exit 2
trap 'rm -rf "$out"' EXIT
# A script's background jobs ignore SIGINT, so a signal here ends each section's process tree. No set -m: a group per
# section would outlive a harness that kills this shell's process group on timeout. Each process is stopped before its
# children are listed: test_hooks' pool would otherwise start the next test as each one dies, and orphan it.
end_tree() {
  local child
  kill -STOP "$1" 2>/dev/null
  for child in $(pgrep -P "$1"); do end_tree "$child"; done
  kill -TERM "$1" 2>/dev/null; kill -CONT "$1" 2>/dev/null
}
interrupted() { for pid in "${pids[@]}"; do end_tree "$pid"; done; exit "$1"; }
trap 'interrupted 129' HUP
trap 'interrupted 130' INT
trap 'interrupted 143' TERM
pids=()
for i in "${picked[@]}"; do
  "section_${names[$i]//-/_}" >"$out/$i" 2>&1 &
  pids+=($!)
done
fail=0
for n in "${!picked[@]}"; do
  i=${picked[$n]}
  wait "${pids[$n]}" || fail=1
  printf '\n== %s\n' "${sections[$i]#*|}"
  cat "$out/$i"
done

printf '\n== %s\n' "real hook log untouched"
if ! logged_after=$(from_tests); then
  echo "FAIL couldn't read $hook_log"; fail=1
elif [ $(( logged_after - logged_before )) -gt 0 ]; then
  logged=$(( logged_after - logged_before ))
  echo "FAIL $logged lines from tests landed in $hook_log: give the test its own HOME or AGENTS_LOG_DIR"; fail=1
else echo "ok   no test logged into $hook_log"; fi

echo
[ $fail = 0 ] && echo "ALL PASSED" || echo "FAILURES ABOVE"
exit $fail
