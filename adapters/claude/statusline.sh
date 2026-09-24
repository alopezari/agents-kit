#!/bin/bash
# Claude Code status line: model, effort, context and plan usage, followed by Xirp's line
# (Xirp's wrapper also relays the context size to its daemon, so it must keep running).
input=$(cat)
ours=$(jq -r '
  def k: if . >= 1000 then "\(. / 1000 | floor)K" else tostring end;
  def pct(x): if x == null then "?" else "\(x)%" end;
  [ (.model.display_name // .model.id | sub(" \\(.*\\)$"; "")),
    (.effort.level // "default"),
    "ctx \(pct(.context_window.used_percentage)) (\(.context_window.current_usage | (.input_tokens + .cache_read_input_tokens + .cache_creation_input_tokens) | k))",
    "5h \(pct(.rate_limits.five_hour.used_percentage))",
    "7d \(pct(.rate_limits.seven_day.used_percentage))",
    (if .fast_mode then "FAST MODE ON" else empty end)
  ] | join(" · ")' <<<"$input" 2>/dev/null)
xirp=""
[ -x "$HOME/.claude/xirp-statusline-wrapper.sh" ] && xirp=$(printf '%s' "$input" | "$HOME/.claude/xirp-statusline-wrapper.sh" 2>/dev/null)
echo "${ours}${ours:+${xirp:+ · }}${xirp}"
