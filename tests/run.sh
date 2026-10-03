#!/bin/bash
# Regression suite for the kit. Exit 0 only when everything passes.
#   ~/.agents/tests/run.sh                        every section
#   ~/.agents/tests/run.sh hooks install          only those sections
#   ~/.agents/tests/run.sh --skip hooks install   all but those (CI's third job, so a new section lands there)
# Sections run concurrently, each printed as one block in this order when it finishes.
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

# Each section is a function section_<name> (dashes as underscores) that returns non-zero on any failure; its title
# is the matching entry of `titles`.
names=(hooks generic-verify gh-wrapper baselines semgrep phase quality-log pr-comments free-branch pr-validation spec
  browse verify-changed deps outcomes sessions frontmatter pi triage ci-wait docs site version install doctor)
titles=("hooks" "generic verify" "gh proxy wrapper" "harness baselines" "semgrep rules" "flow phase" "quality log input"
  "pull request comments not handled yet" "free a branch from its worktree" "pull request validation section"
  "spec lint and second reading" "browser A/B harness" "verify on changed lines" "dependencies" "outcomes and escapes"
  "sessions and phases" "skill frontmatter" "pi adapter" "triage" "CI wait" "framework reference" "site build" "version"
  "install on a new machine" "install doctor")

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
for arg in "$@"; do
  if [ "$arg" = --skip ]; then skip=1; continue; fi
  [[ " ${names[*]} " == *" $arg "* ]] || { echo "unknown section: $arg (sections: ${names[*]})"; exit 2; }
  asked+=("$arg")
done
picked=()
for i in "${!names[@]}"; do
  if [ ${#asked[@]} = 0 ]; then picked+=("$i"); continue; fi
  [[ " ${asked[*]} " == *" ${names[$i]} "* ]] && named=1 || named=0
  [ $named != $skip ] && picked+=("$i")
done

logged_before=$(from_tests) || { echo "FAIL couldn't read $hook_log"; exit 1; }
out=$(mktemp -d "${TMPDIR:-/tmp}/kit-suite-XXXXXX") || exit 2
trap 'rm -rf "$out"' EXIT
# Without job control a script's background jobs ignore SIGINT, and Ctrl-C would leave every section running. With
# it each section is a process group of its own, which an interrupt ends with the tests it started, never the caller.
set -m
trap 'for pid in "${pids[@]}"; do kill -TERM -- "-$pid" 2>/dev/null; done; exit 130' INT TERM
pids=()
for i in "${picked[@]}"; do
  "section_${names[$i]//-/_}" >"$out/$i" 2>&1 &
  pids+=($!)
done
fail=0
for n in "${!picked[@]}"; do
  i=${picked[$n]}
  wait "${pids[$n]}" || fail=1
  printf '\n== %s\n' "${titles[$i]}"
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
