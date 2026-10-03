#!/bin/bash
# Run by launchd every Monday at 9:00 (launchd/sample-weekly.plist): a weekly summary of the turns the after-turn
# counted in this profile's repositories.
log="$HOME/.agents/logs/example-plugin-turns.jsonl"
[ -f "$log" ] || { echo "$(date +%F): no turns logged yet"; exit 0; }
echo "$(date +%F): $(wc -l < "$log" | tr -d ' ') turns in example-plugin so far"
