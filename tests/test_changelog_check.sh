#!/bin/bash
# .github/changelog-check against a throwaway repo: a new Unreleased line passes, anything else fails.
set -uo pipefail
repo=$(mktemp -d "${TMPDIR:-/tmp}/agents-changelog-XXXXXX")
trap 'rm -rf "$repo"' EXIT
check_script="$HOME/.agents/.github/changelog-check"
fail=0
cd "$repo" && git init -q -b main
printf '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n' > CHANGELOG.md
git add -A && git -c user.name=t -c user.email=t@t commit -qm base

case_() {  # case_ <expect pass|fail> <description> <CHANGELOG.md content>
  printf '%b' "$3" > CHANGELOG.md
  if "$check_script" main >/dev/null 2>&1; then got=pass; else got=fail; fi
  if [ "$got" = "$1" ]; then echo "ok   $2"; else echo "FAIL $2 (got $got)"; fail=1; fi
}
case_ pass "a new line under Unreleased passes" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n- New entry (#2).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
case_ fail "no change fails" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
case_ fail "a new line under a released version fails" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n- Sneaked in (#2).\n'
case_ fail "rewording the intro fails" \
  '# Changelog, edited\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
case_ fail "a line under another heading after Unreleased fails" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## Notes\n\n- Not an entry (#2).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
case_ fail "an empty bullet fails" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n- \n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
case_ pass "rewording an existing entry counts as new" \
  '# Changelog\n\n## [Unreleased]\n\n- Old entry, clarified (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n'
exit $fail
