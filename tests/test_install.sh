#!/bin/bash
# A real install of the current kit into an empty HOME, as on a new machine, then --doctor on it.
# Scheduled jobs are skipped: launchd labels are per user and would replace the real ones.
set -uo pipefail
home=$(mktemp -d "${TMPDIR:-/tmp}/agents-install-XXXXXX")
trap 'rm -rf "$home"' EXIT
rsync -a --exclude node_modules --exclude logs --exclude backups --exclude research --exclude profiles \
  --exclude approvals --exclude monitors/state "$HOME/.agents/" "$home/.agents/"
# Dependencies are linked, not reinstalled: the test is about install.sh, not npm.
for dir in "$HOME"/.agents/tools/*/ "$HOME"/.agents/site/; do
  rel=${dir#"$HOME"/.agents/}
  [ -d "$dir/node_modules" ] && ln -s "$dir/node_modules" "$home/.agents/${rel}node_modules"
done
# The machine lacks gh, semgrep and gitleaks, and a fake brew "installs" them as stubs: install.sh must
# install missing required and recommended programs, and only report optional ones.
bin="$home/bin"; mkdir -p "$bin"
for program in python3 git jq node npm claude codex pi; do
  path=$(command -v "$program") && ln -s "$path" "$bin/$program"
done
printf '#!/bin/bash\n[ "$1" = install ] || exit 1\nprintf "#!/bin/sh\\n" > "%s/${!#}"; chmod +x "%s/${!#}"\n' "$bin" "$bin" > "$bin/brew"
chmod +x "$bin/brew"
export PATH="$bin:/usr/bin:/bin:/usr/sbin:/sbin"
# A profile needs docker, which the core lists as optional: the stricter tier wins, so it gets installed.
mkdir -p "$home/.agents/profiles/work"
echo "required docker brew:docker the profile's containerized verify" > "$home/.agents/profiles/work/deps.txt"
fail=0
# Nobody can answer a prompt here: without --yes nothing is installed, and the warning says how to.
first=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" </dev/null 2>&1)
if [ ! -e "$bin/gh" ] && echo "$first" | grep -q "warn  gh is missing (required).*or with --yes"; then
  echo "ok   without a terminal or --yes, install.sh installs nothing and says how to"
else echo "FAIL install.sh without --yes: $(echo "$first" | grep -E "gh( |$)")"; fail=1; fi
if out=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" --yes 2>&1); then
  echo "ok   install.sh completes on an empty HOME"
else
  echo "FAIL install.sh on an empty HOME:"; echo "$out" | tail -5; fail=1
fi
if echo "$out" | grep -qiE "unbound|error|No such file"; then
  echo "FAIL install.sh printed errors:"; echo "$out" | grep -iE "unbound|error|No such file"; fail=1
fi
for program in gh semgrep gitleaks docker; do
  if echo "$out" | grep -qF "fix   $program installed (brew install $program)"; then echo "ok   install.sh installs a missing $program"
  else echo "FAIL install.sh did not install the missing $program"; fail=1; fi
done
if echo "$out" | grep -q "  info  playwright-cli not installed" && [ ! -e "$bin/playwright-cli" ]; then
  echo "ok   install.sh reports a missing optional program without installing it"
else echo "FAIL optional playwright-cli: $(echo "$out" | grep playwright-cli)"; fail=1; fi
# Trusting Codex hooks is done in Codex itself, so a new machine legitimately warns about it.
warnings=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" --doctor 2>&1 | grep "  warn " | grep -v "codex hook not trusted")
if [ -z "$warnings" ]; then echo "ok   --doctor is clean after installing"
else echo "FAIL --doctor after installing:"; echo "$warnings"; fail=1; fi
if [ "$(echo "$out" | grep -c "  fix   docker installed")" = 1 ] && ! echo "$out" | grep -q "info  docker"; then
  echo "ok   a program in two lists is checked once, at the stricter tier"
else echo "FAIL docker in core and profile: $(echo "$out" | grep docker)"; fail=1; fi
# A program older than its declared minimum is reported, never passed as ok.
printf '#!/bin/sh\necho "oldtool version 1.9.3"\n' > "$bin/oldtool"; chmod +x "$bin/oldtool"
echo "required oldtool>=1.10 brew:oldtool a test" >> "$home/.agents/profiles/work/deps.txt"
doctor=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$home/.agents/install.sh" --doctor 2>&1)
if echo "$doctor" | grep -q "  warn  oldtool 1.9.3 is older than 1.10"; then
  echo "ok   --doctor reports a program older than its minimum"
else echo "FAIL --doctor did not flag oldtool 1.9.3 < 1.10"; fail=1; fi

# uninstall.sh on the same HOME, with a fake launchctl so the real jobs (labels are per user) stay untouched.
printf '#!/bin/sh\necho "$@" >> "%s/launchctl.log"\n[ "$1" = list ] && exit 1\nexit 0\n' "$home" > "$bin/launchctl"
chmod +x "$bin/launchctl"
if [ "$(command -v launchctl)" != "$bin/launchctl" ]; then echo "FAIL the fake launchctl isn't first in PATH"; exit 1; fi
kit="$home/.agents"
wired=$(HOME="$home" "$kit/install.sh" --yes 2>&1)
S="$home/.claude/settings.json"
jq '.hooks.Stop += [{matcher: "", hooks: [{type: "command", command: "my-own-hook"}]}]' "$S" > "$S.tmp" && mv "$S.tmp" "$S"
mkdir -p "$home/.claude/skills/mine"
kit_links() { find "$home" -path "$kit" -prune -o -type l -lname "$kit/*" -print | wc -l | tr -d ' '; }
jobs=$(ls "$home/Library/LaunchAgents" 2>/dev/null | wc -l | tr -d ' ')
before=$(kit_links)
if [ "$before" -gt 0 ] && [ "$jobs" -gt 0 ]; then echo "ok   a full install to remove: $before links, $jobs scheduled jobs"
else echo "FAIL the install to remove has $before links and $jobs jobs:"; echo "$wired" | tail -5; fail=1; fi
out=$(HOME="$home" "$kit/uninstall.sh" </dev/null 2>&1)
if [ "$(kit_links)" = "$before" ] && echo "$out" | grep -q "Nothing removed: run uninstall.sh in a terminal"; then
  echo "ok   without a terminal or --yes, uninstall.sh lists what it would remove and changes nothing"
else echo "FAIL uninstall.sh without --yes:"; echo "$out" | tail -3; fail=1; fi
out=$(HOME="$home" "$kit/uninstall.sh" --yes 2>&1)
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
check "uninstall.sh --yes removes every link into the kit" '[ "$(kit_links)" = 0 ]'
check "and the kit's hooks and status line from Claude Code and Codex" \
  '! grep -qF "/.agents/" "$S" "$home/.codex/hooks.json"'
check "keeping the user's own hook, skill and settings" \
  'jq -e ".hooks.Stop[0].hooks[0].command == \"my-own-hook\" and .effortLevel == \"medium\"" "$S" >/dev/null && [ -d "$home/.claude/skills/mine" ]'
check "unloading and removing every scheduled job" \
  '[ "$jobs" -gt 0 ] && [ -z "$(ls "$home/Library/LaunchAgents")" ] && [ "$(grep -c "^bootout" "$home/launchctl.log")" -ge "$jobs" ]'
check "with the settings files backed up outside the kit" '[ -f "$(ls -d "$home"/.agents-uninstall-backups/*/ | head -1)claude-settings.json" ]'
check "and printing no errors" '! echo "$out" | grep -qiE "unbound|error|No such file"'
check "a second run finds nothing to remove" \
  'HOME="$home" "$kit/uninstall.sh" --yes 2>&1 | grep -q "Nothing of the kit is wired in"'
HOME="$home" "$kit/install.sh" --yes >/dev/null 2>&1
check "install.sh wires it all back in" '[ "$(kit_links)" = "$before" ]'
exit $fail
