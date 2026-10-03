#!/bin/bash
# Regression suite for the kit. Exit 0 only when everything passes.
#   ~/.agents/tests/run.sh                        every section
#   ~/.agents/tests/run.sh hooks install          only those sections
#   ~/.agents/tests/run.sh --skip hooks install   all but those (CI's third job, so a new section lands there)
# Sections run concurrently; each prints as one block, in this order, once it and every section above it have finished.
cd "$(dirname "$0")" || exit 2
# A hook run by a test must log into the test's own place (its HOME, or AGENTS_LOG_DIR), never into the real log the
# monthly job reads. A line counts as this run's when it carries the run's id (hooklog adds AGENTS_SUITE_RUN) or its cwd
# is under the run's own temp root; sessions logging from elsewhere in the temp dir while the suite runs have neither.
hook_log="$HOME/.agents/logs/hooks.jsonl"
from_tests() { python3 - "$hook_log" "$run_root" "$AGENTS_SUITE_RUN" <<'PY'
import json, os, sys
log, root, run = sys.argv[1:]
root = os.path.realpath(root)
lines = open(log, errors="replace").read().splitlines() if os.path.exists(log) else []
count = 0
for line in lines:
    try:
        entry = json.loads(line)
        cwd = os.path.realpath(str(entry.get("cwd") or ""))
    except (ValueError, AttributeError):
        continue  # a line cut by a crash, or still being appended
    count += entry.get("suite_run") == run or os.path.commonpath([cwd, root]) == root
print(count)
PY
}

# Each section is a function section_<name> (dashes as underscores) that returns non-zero on any failure.
sections=("hooks|hooks" "generic-verify|generic verify" "gh-wrapper|gh proxy wrapper" "baselines|harness baselines"
  "semgrep|semgrep rules" "phase|flow phase" "quality-log|quality log input"
  "pr-comments|pull request comments not handled yet" "free-branch|free a branch from its worktree"
  "pr-validation|pull request validation section" "spec|spec lint and second reading" "browse|browser A/B harness"
  "verify-changed|verify on changed lines" "deps|dependencies" "outcomes|outcomes and escapes"
  "sessions|sessions and phases" "triage|triage" "ci-wait|CI wait" "mods|Claude Code mod"
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
section_triage() {
  local f=0
  if ~/.agents/bin/triage --json --range HEAD~1..HEAD 2>/dev/null | jq -e '.tier and .lenses.correctness' >/dev/null; then
    echo "ok   triage produces a tier and lenses"
  else echo "FAIL triage"; f=1; fi
  python3 test_triage.py || f=1
  return $f
}
section_ci_wait() { python3 test_ci_wait.py; }
section_mods() {
  local f=0 kit=~/.agents
  command -v claude >/dev/null || { echo "FAIL claude is not installed: the mod can't be validated or tested"; return 1; }
  local validated; validated=$(cd "$kit/mods/kit" && claude plugin validate --strict . 2>&1) && echo "ok   claude plugin validate --strict" \
    || { echo "FAIL claude plugin validate --strict:"; echo "$validated" | tail -8; f=1; }
  (cd "$kit/mods/kit" && claude plugin test) || f=1
  # The flow must work without the mod: nothing it runs on may depend on it. Only bin/docs (which documents it)
  # and bin/changelog (which sets its version) name it.
  local users dir dirs=()
  for dir in hooks skills bin repos/_shared adapters monitors launchd tools review-mining; do dirs+=("$kit/$dir"); done
  users=$(grep -rlE "kit@agents-kit|mods/kit|features\.js|hooks/register\.js" "${dirs[@]}" --exclude-dir=node_modules 2>/dev/null \
    | grep -vxE "$kit/bin/(docs|changelog)")
  if [ -z "$users" ]; then echo "ok   no hook, skill or tool depends on the mod"
  else echo "FAIL these depend on the mod, so the flow would break without it:"; echo "$users"; f=1; fi
  return $f
}
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
  plugin_version=$(jq -r .version ~/.agents/mods/kit/.claude-plugin/plugin.json)
  if [ "$plugin_version" = "$version" ]; then echo "ok   the kit's Claude Code plugin is at VERSION $version"
  else echo "FAIL mods/kit/.claude-plugin/plugin.json has version $plugin_version, VERSION has $version (bin/changelog release sets both)"; f=1; fi
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

run_root=$(mktemp -d "${TMPDIR:-/tmp}/kit-suite-XXXXXX") || exit 2
trap 'rm -rf "$run_root"' EXIT
out="$run_root/out" && mkdir "$out" "$run_root/tmp" || exit 2
export TMPDIR="$run_root/tmp" AGENTS_SUITE_RUN="${run_root##*/}"
# A script's background jobs ignore SIGINT, so a signal here ends each section's process tree. No set -m: a group per
# section would outlive a harness that kills this shell's process group on timeout. Each process is stopped before its
# children are listed: test_hooks' pool would otherwise start the next test as each one dies, and orphan it.
end_tree() {
  local child
  kill -STOP "$1" 2>/dev/null
  for child in $(pgrep -P "$1"); do end_tree "$child"; done
  kill -TERM "$1" 2>/dev/null; kill -CONT "$1" 2>/dev/null
}
interrupted() { for pid in "${pids[@]}"; do end_tree "$pid"; done; wait; exit "$1"; }
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
  unset "pids[$n]"  # reaped: an interrupt must not signal a pid the system may have reused
  printf '\n== %s\n' "${sections[$i]#*|}"
  cat "$out/$i"
done

printf '\n== %s\n' "real hook log untouched"
if ! logged=$(from_tests); then
  echo "FAIL couldn't read $hook_log"; fail=1
elif [ "$logged" -gt 0 ]; then
  echo "FAIL $logged lines from tests landed in $hook_log: give the test its own HOME or AGENTS_LOG_DIR"; fail=1
else echo "ok   no test logged into $hook_log"; fi

echo
[ $fail = 0 ] && echo "ALL PASSED" || echo "FAILURES ABOVE"
exit $fail
