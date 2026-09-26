#!/bin/bash
# A real install of the current kit into an empty HOME, as on a new machine, then --doctor on it.
# Scheduled jobs are skipped: launchd labels are per user and would replace the real ones.
set -uo pipefail
home=$(mktemp -d "${TMPDIR:-/tmp}/agents-install-XXXXXX")
trap 'rm -rf "$home"' EXIT
rsync -a --exclude node_modules --exclude /logs --exclude /backups --exclude /research --exclude /profiles \
  --exclude /approvals --exclude /monitors/state "$HOME/.agents/" "$home/.agents/"
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
printf '#!/bin/bash\n[ "$1" = install ] || exit 1\nrm -f "%s/${!#}"; printf "#!/bin/sh\\n" > "%s/${!#}"; chmod +x "%s/${!#}"\n' "$bin" "$bin" "$bin" > "$bin/brew"
chmod +x "$bin/brew"
real_path=$PATH q="'"
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
if grep -qx "agents-kit $(cat "$home/.agents/VERSION")" <<<"$doctor"; then echo "ok   --doctor names the kit's version"
else echo "FAIL --doctor does not print agents-kit <VERSION>"; fail=1; fi
if echo "$doctor" | grep -q "  warn  oldtool 1.9.3 is older than 1.10"; then
  echo "ok   --doctor reports a program older than its minimum"
else echo "FAIL --doctor did not flag oldtool 1.9.3 < 1.10"; fail=1; fi

# The sample profile, installed as a user would: each extension point must do what its README says.
semgrep_dir=$(dirname "$(PATH="$real_path" command -v semgrep 2>/dev/null || echo /nonexistent/semgrep)")
kit="$home/.agents"; sample="$kit/examples/sample-profile"
mkdir -p "$home/.claude"  # Claude Code is wired by its directory when it isn't installed (CI)
out=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$kit/install.sh" --yes --profile "$sample" 2>&1)
check() { if eval "$2"; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi; }
check "sample profile: repo overlay, skill and research linked into the kit and the harness" \
  '[ -L "$kit/repos/example-plugin" ] && [ -L "$kit/skills/release-notes" ] && [ -L "$kit/research/example-study.md" ] \
   && [ "$(readlink "$home/.claude/skills/release-notes")" = "$kit/skills/release-notes" ]'
check "sample profile: its optional program is reported, not installed" 'echo "$out" | grep -q "info  composer not installed"'
mcp() {  # mcp <tool> <input json>: the MCP guard's decision; it prints nothing to allow
  local decision; decision=$(printf '{"tool_name":"%s","tool_input":%s,"session_id":"t"}' "$1" "$2" \
    | HOME="$home" python3 "$kit/hooks/guard_mcp.py" | jq -r '.hookSpecificOutput.permissionDecision')
  echo "${decision:-allow}"
}
decisions="$(mcp mcp__tracker__create_issue '{}') $(mcp mcp__tracker__get_issue '{}')"
decisions+=" $(mcp mcp__hub__execute '{"service":"wiki","action":"edit-page"}') $(mcp mcp__hub__execute '{"service":"wiki","action":"read-page"}')"
check "sample profile: the guard blocks tracker and wiki writes, not reads" '[ "$decisions" = "deny allow deny allow" ]'
# A second profile reusing the sample's names, and one of the kit's own: the first profile keeps each name, the
# kit's own is never replaced, and nothing flips between runs.
other="$home/zz-other"
mkdir -p "$other/skills/release-notes" "$other/skills/spec" "$other/repos/example-plugin" "$other/research"
echo "# other" > "$other/research/example-study.md"
echo '{"direct": {"tracker": "^x", "linear": "^y"}}' > "$other/mcp-writes.json"
out=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$kit/install.sh" --yes --profile "$other" 2>&1); status=$?
again=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$kit/install.sh" --yes 2>&1)
check "two profiles with the same names: warned, the first keeps them, the kit's own stays" \
  '[ $status = 0 ] && grep -q "warn  skills/release-notes is in profiles sample-profile and zz-other; using sample-profile.s" <<<"$out" \
   && grep -q "warn  repos/example-plugin is in profiles sample-profile and zz-other" <<<"$out" \
   && grep -q "warn  research/example-study.md is in profiles sample-profile and zz-other" <<<"$out" \
   && grep -q "warn  skills/spec in profile zz-other has the name of the kit.s own; skipped" <<<"$out" \
   && [ "$(readlink "$kit/skills/release-notes")" = "$kit/profiles/sample-profile/skills/release-notes" ] \
   && [ "$(readlink "$kit/research/example-study.md")" = "$kit/profiles/sample-profile/research/example-study.md" ] \
   && [ -d "$kit/skills/spec" ] && [ ! -L "$kit/skills/spec" ] && ! grep -q "  fix " <<<"$again"'
check "and a direct MCP server in two places is warned about" \
  'grep -q "warn  MCP server tracker has write rules in profile sample-profile and profile zz-other" <<<"$out" \
   && grep -q "warn  MCP server linear has write rules in the kit and profile zz-other" <<<"$out"'
rm "$kit/profiles/zz-other"
mkdir -p "$home/elsewhere/sample-profile"
out=$(HOME="$home" AGENTS_SKIP_LAUNCHD=1 "$kit/install.sh" --doctor --profile "$home/elsewhere/sample-profile" 2>&1)
check "a new profile with an installed one's directory name is warned about" \
  'grep -q "warn  profile sample-profile was /.*/examples/sample-profile; /.*/elsewhere/sample-profile replaces it" <<<"$out"'
check "sample profile: its private terms are enforced" \
  '[ "$(HOME="$home" python3 -c "import sys; sys.path.insert(0, \"$kit/hooks\"); import private_terms; print(private_terms.found(\"see INTERNAL-42\"))")" = "[${q}INTERNAL-42${q}]" ]'
check "sample profile: the monthly review mining runs with its list of only comments" \
  'HOME="$home" FETCH_ONLY=1 "$kit/review-mining/run.sh" >/dev/null 2>&1'
# The monthly job hands other people's review comments to a model: it must run it without network or code
# execution, writing only its run folder and the proposal. A fake claude records the flags and writes the proposal.
# $bin/claude links to the real Claude Code: remove the link first, or the fake would be written into it.
rm -f "$bin/claude" "$bin/osascript"
printf '#!/bin/sh\nprintf "%%s\\n" "$@" > "%s/claude-args"\nmkdir -p "%s/.agents/research/proposals"\necho proposal > "%s/.agents/research/proposals/$(date +%%Y-%%m).md"\n' \
  "$home" "$home" "$home" > "$bin/claude"
printf '#!/bin/sh\nexit 0\n' > "$bin/osascript"; chmod +x "$bin/claude" "$bin/osascript"
HOME="$home" "$kit/review-mining/run.sh" >/dev/null 2>&1; mining_status=$?
flags=$(cat "$home/claude-args" 2>/dev/null)
run_dir="$kit/review-mining/runs/$(date +%Y-%m)"
writable=$(grep '^Edit(' <<<"$flags" | sort | tr '\n' ' ')
tools=$(grep -A1 -x -- --tools <<<"$flags" | tail -1)
check "review mining runs the model without network, a shell or writes outside its run and the proposal" \
  '[ $mining_status = 0 ] && grep -qx -- --restricted <<<"$flags" && grep -qx dontAsk <<<"$flags" \
   && [ "$tools" = "Read,Grep,Glob,Write,Edit,Agent" ] && ! grep -q "^Bash" <<<"$flags" \
   && [ "$writable" = "Edit(/$kit/research/proposals/$(date +%Y-%m).md) Edit(/$run_dir/**) " ]'
since=$(cat "$kit/review-mining/last-success")
rm -f "$bin/claude"; printf '#!/bin/sh\nexit 0\n' > "$bin/claude"; chmod +x "$bin/claude"
check "and a run that writes no proposal fails, even with an earlier one from this month" \
  '! HOME="$home" "$kit/review-mining/run.sh" >/dev/null 2>&1 && [ "$(cat "$kit/review-mining/last-success")" = "$since" ]'
plugin="$home/Projects/example-plugin"; mkdir -p "$plugin"
git -C "$plugin" init -q -b main && git -C "$plugin" commit -q --allow-empty -m init && git -C "$plugin" switch -q -c change
line=$(printf '{"workspace":{"current_dir":"%s"}}' "$plugin" | HOME="$home" "$kit/adapters/claude/statusline.sh" 2>/dev/null)
check "sample profile: its status line segment names the overlay" 'echo "$line" | grep -q "overlay example-plugin"'
if [ -x "$semgrep_dir/semgrep" ]; then
  printf '<?php\nerror_log( "x" );\n' > "$plugin/plugin.php"
  red=$(cd "$plugin" && HOME="$home" PATH="$semgrep_dir:$PATH" "$kit/repos/example-plugin/verify" 2>&1); red_status=$?
  printf '<?php\nExample_Plugin\\Logger::info( "x" );\n' > "$plugin/plugin.php"
  (cd "$plugin" && HOME="$home" PATH="$semgrep_dir:$PATH" "$kit/repos/example-plugin/verify" >/dev/null 2>&1); green_status=$?
  check "sample profile: its verify fails on the project rule and passes without it" \
    '[ $red_status = 1 ] && echo "$red" | grep -q "logger-not-error-log" && [ $green_status = 0 ]'
else echo "skip sample profile verify: semgrep not installed"; fi

# uninstall.sh on the same HOME, with a fake launchctl so the real jobs (labels are per user) stay untouched.
# `list` reports a job loaded only when its label is in $home/stuck, standing in for a bootout that failed.
printf '#!/bin/sh\necho "$@" >> "%s/launchctl.log"\n[ "$1" = list ] && { grep -qx "$2" "%s/stuck" 2>/dev/null; exit; }\nexit 0\n' \
  "$home" "$home" > "$bin/launchctl"
chmod +x "$bin/launchctl"
if [ "$(command -v launchctl)" != "$bin/launchctl" ]; then echo "FAIL the fake launchctl isn't first in PATH"; exit 1; fi
unset AGENTS_SKIP_LAUNCHD  # CI sets it; the fake launchctl above is what keeps the real jobs safe here
mkdir -p "$home/.claude" "$home/.codex"  # wired by their directories when the harnesses aren't installed (CI)
wired=$(HOME="$home" "$kit/install.sh" --yes 2>&1)
S="$home/.claude/settings.json"
jq '.hooks.Stop += [{matcher: "", hooks: [{type: "command", command: "my-own-hook"}]}] | .hooks.Notification = [{matcher: "x", hooks: []}]' \
  "$S" > "$S.tmp" && mv "$S.tmp" "$S"
mkdir -p "$home/.claude/skills/mine" "$home/elsewhere"
ln -s "$home/elsewhere" "$home/.claude/skills/linked-elsewhere"
ln -s "$kit/../elsewhere" "$home/.claude/skills/through-the-kit"
kit_links() { find "$home" -path "$kit" -prune -o -type l -lname "$kit/*" ! -lname "*/../*" -print | wc -l | tr -d ' '; }
jobs=$(ls "$home/Library/LaunchAgents" 2>/dev/null | wc -l | tr -d ' ')
before=$(kit_links)
if [ "$before" -gt 0 ] && [ "$jobs" -gt 0 ]; then echo "ok   a full install to remove: $before links, $jobs scheduled jobs"
else echo "FAIL the install to remove has $before links and $jobs jobs:"; echo "$wired" | tail -5; fail=1; fi
out=$(HOME="$home" "$kit/uninstall.sh" </dev/null 2>&1)
if [ "$(kit_links)" = "$before" ] && echo "$out" | grep -q "Nothing removed: run uninstall.sh in a terminal"; then
  echo "ok   without a terminal or --yes, uninstall.sh lists what it would remove and changes nothing"
else echo "FAIL uninstall.sh without --yes:"; echo "$out" | tail -3; fail=1; fi
# script(1) gives it a terminal, so it asks; answering no must change nothing. Input stays open a while:
# script kills the command when its input ends, which would pass this test whatever the answer did.
out=$( (printf 'n\n'; sleep 10) | HOME="$home" script -q /dev/null "$kit/uninstall.sh" 2>&1)
if [ "$(kit_links)" = "$before" ] && echo "$out" | grep -q "Remove these .* items? \[y/N\]"; then
  echo "ok   in a terminal, uninstall.sh asks, and answering no changes nothing"
else echo "FAIL uninstall.sh answered no:"; echo "$out" | tail -3; fail=1; fi
: > "$home/launchctl.log"
agents=$(ls "$home/Library/LaunchAgents")
mine=$(echo "$agents" | sed -n 1p); stuck=$(echo "$agents" | sed -n 2p)
echo '<plist><string>/usr/bin/true</string></plist>' > "$home/Library/LaunchAgents/$mine"  # the user's own job now
basename "$stuck" .plist > "$home/stuck"
out=$(HOME="$home" "$kit/uninstall.sh" --yes 2>&1)
check "uninstall.sh --yes removes every link into the kit" '[ "$(kit_links)" = 0 ]'
check "and the kit's hooks and status line from Claude Code and Codex" \
  '! grep -qF "/.agents/" "$S" "$home/.codex/hooks.json"'
check "keeping the user's own hook, skill and settings" \
  'jq -e ".hooks.Stop[0].hooks[0].command == \"my-own-hook\" and .hooks.Notification[0].matcher == \"x\" and .effortLevel == \"medium\"" "$S" >/dev/null && [ -d "$home/.claude/skills/mine" ] && [ -L "$home/.claude/skills/linked-elsewhere" ] \
  && [ -L "$home/.claude/skills/through-the-kit" ]'
check "unloading and removing the kit's scheduled jobs, keeping one still loaded and the user's own" \
  '[ "$jobs" -gt 2 ] && [ "$(ls "$home/Library/LaunchAgents" | sort | tr "\n" " ")" = "$(printf "%s\n" "$mine" "$stuck" | sort | tr "\n" " ")" ] \
   && [ "$(grep -c "^bootout" "$home/launchctl.log")" = $((jobs - 1)) ] && echo "$out" | grep -q "is still loaded"'
backups=$(ls -d "$home"/.agents-uninstall-backups/*/ | head -1)
check "with the settings files, as they were before, backed up outside the kit" \
  'grep -qF "/.agents/hooks/" "${backups}claude-settings.json" && grep -qF "/.agents/hooks/" "${backups}codex-hooks.json"'
check "and printing no errors" '! echo "$out" | grep -qiE "unbound|error|No such file"'
: > "$home/stuck"
check "a second run removes the job that was still loaded, then finds nothing" \
  'HOME="$home" "$kit/uninstall.sh" --yes >/dev/null 2>&1 && HOME="$home" "$kit/uninstall.sh" --yes 2>&1 | grep -q "Nothing of the kit is wired in"'
HOME="$home" "$kit/install.sh" --yes >/dev/null 2>&1
check "install.sh wires it all back in" '[ "$(kit_links)" = "$before" ]'
exit $fail
