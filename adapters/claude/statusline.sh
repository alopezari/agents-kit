#!/bin/bash
# Claude Code status line: model, effort, context and plan usage, the branch's phase in the kit's flow (bin/phase),
# followed by Xirp's line
# (Xirp's wrapper also relays the context size to its daemon, so it must keep running).
input=$(cat)
# Plan limits are per account, but Claude Code sends them only after a session's first response: keep the last
# ones seen, and show them marked "~" in a new session until its own arrive.
limits_cache="$HOME/.agents/monitors/state/rate-limits.json"
limits=$(jq -c '.rate_limits // empty' <<<"$input" 2>/dev/null)
limits_stale=""
if [ -n "$limits" ]; then
  mkdir -p "$(dirname "$limits_cache")" && printf '%s' "$limits" > "$limits_cache.$$" && mv "$limits_cache.$$" "$limits_cache"
else
  limits=$(cat "$limits_cache" 2>/dev/null) && limits_stale="~"
fi
ours=$(jq -r --argjson limits "${limits:-null}" --arg stale "$limits_stale" --argjson now "$(date +%s)" '
  def k: if . >= 1000 then "\(. / 1000 | floor)K" else tostring end;
  def pct(x): if x == null then "?" else "\(x)%" end;
  # A remembered limit whose window has since reset says nothing about now.
  def limit(w): if w == null or ($stale != "" and (w.resets_at // 0) > 0 and w.resets_at < $now) then "?"
                else "\($stale)\(pct(w.used_percentage))" end;
  [ (.model.display_name // .model.id | sub(" \\(.*\\)$"; "")),
    (.effort.level // "default"),
    (if .context_window.current_usage == null then "ctx –"
     else "ctx \(pct(.context_window.used_percentage)) (\(.context_window.current_usage | (.input_tokens + .cache_read_input_tokens + .cache_creation_input_tokens) | k))" end),
    "5h \(limit($limits.five_hour))",
    "7d \(limit($limits.seven_day))",
    (if .fast_mode then "FAST MODE ON" else empty end)
  ] | join(" · ")' <<<"$input" 2>/dev/null)
dir=$(jq -r '.workspace.current_dir // .cwd // empty' <<<"$input" 2>/dev/null)
phase=$(cd "${dir:-.}" 2>/dev/null && "$HOME/.agents/bin/phase" 2>/dev/null)
[ -n "$phase" ] && ours="${ours:+$ours · }flow: $phase"
# Xirp either calls this script from its wrapper (settings.json points at the wrapper, which sets
# CHIRP_STATUSLINE_NESTED and appends its own line) or is called from here; never both, or its line shows twice.
xirp=""
[ -z "${CHIRP_STATUSLINE_NESTED:-}" ] && [ -x "$HOME/.claude/xirp-statusline-wrapper.sh" ] \
  && xirp=$(printf '%s' "$input" | "$HOME/.claude/xirp-statusline-wrapper.sh" 2>/dev/null)
echo "${ours}${ours:+${xirp:+ · }}${xirp}"
