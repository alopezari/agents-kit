#!/bin/bash
# A real install of the current kit into an empty HOME, as on a new machine, then --doctor on it.
# Scheduled jobs are skipped: launchd labels are per user and would replace the real ones.
set -uo pipefail
home=$(mktemp -d "${TMPDIR:-/tmp}/agents-install-XXXXXX")
trap 'rm -rf "$home"' EXIT
rsync -a --exclude node_modules --exclude logs --exclude backups --exclude research --exclude profiles \
  --exclude approvals --exclude monitors/state "$HOME/.agents/" "$home/.agents/"
# Dependencies are linked, not reinstalled: the test is about install.sh, not npm.
for tool in "$HOME"/.agents/tools/*/; do
  [ -d "$tool/node_modules" ] && ln -s "$tool/node_modules" "$home/.agents/tools/$(basename "$tool")/node_modules"
done
fail=0
if out=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" 2>&1); then
  echo "ok   install.sh completes on an empty HOME"
else
  echo "FAIL install.sh on an empty HOME:"; echo "$out" | tail -5; fail=1
fi
if echo "$out" | grep -qiE "unbound|error|No such file"; then
  echo "FAIL install.sh printed errors:"; echo "$out" | grep -iE "unbound|error|No such file"; fail=1
fi
# Trusting Codex hooks is done in Codex itself, so a new machine legitimately warns about it.
warnings=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" --doctor 2>&1 | grep "  warn " | grep -v "codex hook not trusted")
if [ -z "$warnings" ]; then echo "ok   --doctor is clean after installing"
else echo "FAIL --doctor after installing:"; echo "$warnings"; fail=1; fi
exit $fail
