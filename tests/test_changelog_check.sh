#!/bin/bash
# .github/changelog-check and bin/changelog release against throwaway repos.
. "$(dirname "$0")/kit_home.sh"
set -uo pipefail
repo=$(mktemp -d "${TMPDIR:-/tmp}/agents-changelog-XXXXXX")
trap 'rm -rf "$repo"' EXIT
check_script="$HOME/.agents/.github/changelog-check"
changelog="$HOME/.agents/bin/changelog"
fail=0
ok() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1 (got $2, expected $3)"; fail=1; fi; }
commit() { git add -A && git -c user.name=t -c user.email=t@t commit -qm "$1"; }

cd "$repo" && git init -q -b main
printf '# Changelog\n\n## [Unreleased]\n\n- Old entry (#1).\n\n## [0.1.0] - 2026-09-24\n\n- First (#0).\n' > CHANGELOG.md
mkdir changelog.d && printf -- '- Already released elsewhere.\n' > changelog.d/older.md
echo '# One file per pull request' > changelog.d/README.md
echo 0.1.0 > VERSION
mkdir -p mods/kit/.claude-plugin && printf '{"name": "kit", "version": "0.1.0", "author": {"name": "a"}}\n' > mods/kit/.claude-plugin/plugin.json
commit base

case_() {  # case_ <expect pass|fail> <description> <command that changes the branch>
  git checkout -q -B branch main
  eval "$3"
  commit "$2" >/dev/null 2>&1
  if "$check_script" main >/dev/null 2>&1; then got=pass; else got=fail; fi
  ok "$2" "$got" "$1"
}
case_ pass "a new fragment passes" "printf -- '- New entry.\n' > changelog.d/feat~x.md"
case_ fail "no change fails" ":"
case_ fail "a line in CHANGELOG.md alone fails: entries go in changelog.d/" \
  "sed -i.bak 's/^- Old entry (#1)./&\n- New entry./' CHANGELOG.md && rm CHANGELOG.md.bak"
case_ fail "editing an existing fragment fails" "printf -- '- Reworded.\n' > changelog.d/older.md"
case_ fail "an empty fragment fails" ": > changelog.d/feat~x.md"
case_ fail "a fragment with only an empty bullet fails" "printf -- '- \n' > changelog.d/feat~x.md"
case_ fail "a bullet with only a tab after it fails: the release drops it" "printf -- '- \\t\\n' > changelog.d/feat~x.md"
case_ pass "a fragment named after a non-ASCII branch passes" "printf -- '- New entry.\\n' > changelog.d/feat~año.md"
case_ fail "a file outside changelog.d/ fails" "printf -- '- New entry.\n' > notes.md"
case_ fail "a fragment in a subdirectory fails: the release doesn't read it" \
  "mkdir -p changelog.d/sub && printf -- '- New entry.\\n' > changelog.d/sub/x.md"
case_ fail "editing changelog.d/README.md isn't an entry" "printf -- '- New entry.\n' >> changelog.d/README.md"
git checkout -q main && git branch -qD branch

# A release: two merged pull requests, and the line already under Unreleased.
git rm -q changelog.d/older.md && commit "drop the old fragment"
for pr in 7 8; do
  git checkout -q -b "feat/$pr" main
  printf -- "- Thing $pr.\n" > "changelog.d/feat~$pr.md"
  echo "$pr" > "code-$pr.txt" && commit "work $pr"
  git checkout -q main && git -c user.name=t -c user.email=t@t merge -q --no-ff "feat/$pr" -m "Merge pull request #$pr from o/feat-$pr"
done
"$changelog" release 0.2.0 > /dev/null 2>&1; ok "release exits 0" "$?" 0
expected=$(printf '## [Unreleased]\n\n## [0.2.0] - %s\n\n- Old entry (#1).\n- Thing 7 (#7).\n- Thing 8 (#8).\n\n## [0.1.0] - 2026-09-24' "$(date +%F)")
ok "release moves Unreleased and each fragment, with its PR number, under the version" \
  "$(sed -n '/^## \[Unreleased\]/,/^## \[0.1.0\]/p' CHANGELOG.md)" "$expected"
ok "release deletes the fragments it released, and keeps the README" "$(ls changelog.d)" README.md
ok "release sets VERSION" "$(cat VERSION)" 0.2.0
ok "and the kit plugin's version, keeping its other fields" "$(jq -c . mods/kit/.claude-plugin/plugin.json)" '{"name":"kit","version":"0.2.0","author":{"name":"a"}}'
commit "release 0.2.0"
git tag v0.2.0

# A later branch reusing a released entry's name gets its own number, not the first merge since the release.
git checkout -q -b feat/9 main && printf -- "- Thing 9.\n" > changelog.d/feat~9.md && commit "work 9"
git checkout -q main && git -c user.name=t -c user.email=t@t merge -q --no-ff feat/9 -m "Merge pull request #9 from o/feat-9"
git checkout -q -B feat/7 main && printf -- "- Thing 7 again.\n" > "changelog.d/feat~7.md" && commit "work 10"
git checkout -q main && git -c user.name=t -c user.email=t@t merge -q --no-ff feat/7 -m "Merge pull request #10 from o/feat-7"
"$changelog" release 0.2.1 > /dev/null 2>&1
ok "a reused entry name takes the number of its own merge" "$(grep -c '^- Thing 7 again (#10)\.$' CHANGELOG.md)" 1
commit "release 0.2.1" && git tag v0.2.1

printf -- '- Committed straight to main.\n' > changelog.d/direct.md && commit direct
# A later, unrelated merge has the direct commit in its history, but didn't bring it in.
git checkout -q -b feat/11 main && echo 11 > code-11.txt && commit "work 11"
git checkout -q main && git -c user.name=t -c user.email=t@t merge -q --no-ff feat/11 -m "Merge pull request #11 from o/feat-11"
before=$(cat CHANGELOG.md VERSION)
out=$("$changelog" release 0.3.0 2>&1); code=$?
ok "a fragment no merge brought in fails" "$code" 1
ok "and names it" "$(echo "$out" | grep -c 'changelog.d/direct.md')" 1
ok "and changes nothing" "$(cat CHANGELOG.md VERSION)" "$before"
git rm -q changelog.d/direct.md && commit "drop direct"
"$changelog" release 0.2.1 > /dev/null 2>&1; ok "releasing a version CHANGELOG.md already has fails" "$?" 1
"$changelog" release 00.3.0 > /dev/null 2>&1; ok "a version with a leading zero fails" "$?" 1
ok "and changes nothing" "$(cat CHANGELOG.md VERSION)" "$before"
git checkout -q -b feat/12 main && printf -- "- Thing 12.\n" > changelog.d/feat~12.md && commit "work 12"
git checkout -q main && git -c user.name=t -c user.email=t@t merge -q --no-ff feat/12 -m "Merge pull request #12 from o/feat-12"
echo '[]' > mods/kit/.claude-plugin/plugin.json && commit "a manifest that isn't an object"
before=$(cat CHANGELOG.md VERSION mods/kit/.claude-plugin/plugin.json changelog.d/*)
out=$("$changelog" release 0.3.0 2>&1); code=$?
ok "a release whose kit plugin manifest isn't a JSON object fails" "$code" 1
ok "and names it" "$(echo "$out" | grep -c "mods/kit/.claude-plugin/plugin.json is not a JSON object")" 1
ok "and changes nothing" "$(cat CHANGELOG.md VERSION mods/kit/.claude-plugin/plugin.json changelog.d/*)" "$before"
printf '{"name": "kit", "version": "0.2.1"}\n' > mods/kit/.claude-plugin/plugin.json && commit "a manifest again" && chmod 444 mods/kit/.claude-plugin/plugin.json
before=$(cat CHANGELOG.md VERSION mods/kit/.claude-plugin/plugin.json changelog.d/*)
out=$("$changelog" release 0.3.0 2>&1); code=$?
chmod 644 mods/kit/.claude-plugin/plugin.json
ok "a release that can't write the kit plugin's manifest fails" "$code" 1
ok "and names it" "$(echo "$out" | grep -c "can't write mods/kit/.claude-plugin/plugin.json")" 1
ok "and changes nothing" "$(cat CHANGELOG.md VERSION mods/kit/.claude-plugin/plugin.json changelog.d/*)" "$before"
git rm -q mods/kit/.claude-plugin/plugin.json && commit "drop the plugin manifest"
before=$(cat CHANGELOG.md VERSION changelog.d/*)
out=$("$changelog" release 0.3.0 2>&1); code=$?
ok "a release without the kit plugin's manifest fails" "$code" 1
ok "and names it" "$(echo "$out" | grep -c "can't read mods/kit/.claude-plugin/plugin.json")" 1
ok "and changes nothing" "$(cat CHANGELOG.md VERSION changelog.d/*)" "$before"
exit $fail
