#!/usr/bin/env python3
"""The kit's own skills go by their kit- names, nothing the agent reads still sends it to a former or retired one, and
every call to the other model family pins its model and effort."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import re
import sys

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
SKILLS = ("kit-spec", "kit-review", "kit-validate", "kit-follow-pr", "kit-ship", "kit-write-pr-description")
# Merged into kit-ship, which opens the pull request now.
RETIRED = re.compile(r"\b(?:kit-)?create-pr\b")
SHIP_SECTIONS = ("## Deploy", "## Smoke checks", "## Rollback", "## Installed on this machine")
FORMER = r"(?:spec|self-review|validate|create-pr|follow-pr|ship|write-pr-description)"
# `spec` skill, the self-review or validate skills, run `spec`, /spec, skills/spec/, ../spec/: phase names alone stay unprefixed.
SENDS_TO_FORMER = re.compile(rf"(?<![\w/-])`?{FORMER}`?(?:(?:,| and| or) `?{FORMER}`?)* skills?\b|\brun `{FORMER}`|`/{FORMER}\b|(?:skills|\.\.)/{FORMER}(?![\w-])")
# The review's former name, kept on purpose: the stamp file, the phase alias, old reports' header.
KEPT = re.compile(r"self-review\.stamp|FORMER_NAMES|\(Self-review\|Review\)|\"Self-review\" heads")
# The other model family's readings run on one known setup, whatever the user's own default is. A call is the command
# followed by anything but the closing backtick, so prose naming "step 2's `claude -p` command" isn't one; it runs to
# the next pipe, `;`, `&&` or backtick, so a pinned call doesn't cover an unpinned one beside it.
CROSS_MODEL_CALL = re.compile(r"\b(codex exec|claude (?:-p|--print))(?!`)\b[^|;&`]*")
# A quoted prompt can hold any of those separators, so quoted text is blanked first; an apostrophe follows a letter and
# opens no quote.
QUOTED = re.compile(r"\"[^\"\n]*\"|(?<!\w)'[^'\n]*'")
CROSS_MODEL_PINS = {"codex": ("-m gpt-6.1-sol", "-c model_reasoning_effort=high"), "claude": ("--model opus", "--effort high")}
READ_BY_THE_AGENT = ["AGENTS.md", "README.md", "skills/kit-*/**/*", "hooks/*.py", "bin/*", "mods/kit/hooks/*.js", "deps.txt"]

failed = False
for skill in SKILLS:
    path = os.path.join(KIT, "skills", skill, "SKILL.md")
    name = re.search(r"^name: (.+)$", open(path).read(), re.M) if os.path.exists(path) else None
    ok = bool(name) and name.group(1).strip() == skill
    failed |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} skills/{skill}/SKILL.md is named {name.group(1).strip() if name else '(missing)'}")

stale = []
for pattern in READ_BY_THE_AGENT:
    for path in glob.glob(os.path.join(KIT, pattern), recursive=True):
        if not os.path.isfile(path) or "node_modules" in path:
            continue
        try:
            lines = open(path).read().splitlines()
        except UnicodeDecodeError:
            continue
        stale += [f"{os.path.relpath(path, KIT)}:{n}: {line.strip()[:120]}" for n, line in enumerate(lines, 1)
                  if SENDS_TO_FORMER.search(line) or RETIRED.search(line) or ("self-review" in line.lower() and not KEPT.search(line))]
ship = open(os.path.join(KIT, "skills", "kit-ship", "SKILL.md")).read()
missing = [section for section in SHIP_SECTIONS if f"`{section}`" not in ship]
failed |= bool(missing)
print(f"FAIL kit-ship doesn't name the ship.md sections {missing}" if missing else "ok   kit-ship names the four ship.md sections")
failed |= bool(stale)
print(("FAIL a former skill name is still read by the agent:\n     " + "\n     ".join(stale)) if stale
      else "ok   no instruction, message or path names a former skill")


def unpinned_calls(text):
    """(line, command, missing pin) for each cross-model call in text, a `\\`-continued line counted as its first."""
    found, joined, start = [], "", 1
    for n, line in enumerate(text.splitlines() + [""], 1):
        if not joined:
            start = n
        joined += line
        if line.endswith("\\"):
            joined = joined[:-1]
            continue
        for call in CROSS_MODEL_CALL.finditer(QUOTED.sub('""', joined)):
            found += [(start, call.group(1), pin) for pin in CROSS_MODEL_PINS[call.group(1).split()[0]] if pin not in call.group(0)]
        joined = ""
    return found


# The check itself: each of these must be caught, or a call can drift off its pins unseen.
for sample in ['codex exec "$prompt"', 'claude -p "$p"', "claude --print <<<x", "a | claude -p --model opus --effort high | codex exec -",
               'claude -p --model opus \\\n  --allowedTools "Read"']:
    assert unpinned_calls(sample), f"unpinned call not caught: {sample!r}"
assert not unpinned_calls("pipe it into step 2's `claude -p` command, or `codex exec` from Claude"), "prose counted as a call"
assert not unpinned_calls("claude -p --effort high \\\n  --model opus -"), "a continued line's pin not seen"
for sample in ['codex exec --ephemeral "Review A; check B" -m gpt-6.1-sol -c model_reasoning_effort=high',
               "claude -p --allowedTools Read 'Review A | check B' --model opus --effort high"]:
    assert not unpinned_calls(sample), f"a separator inside quotes cut a pinned call: {sample!r}"
assert unpinned_calls('codex exec "Review A; check B" -m gpt-6.1-sol'), "an unpinned call with a quoted prompt not caught"

unpinned = []
for path in glob.glob(os.path.join(KIT, "skills", "**", "*"), recursive=True):
    if not os.path.isfile(path):
        continue
    try:
        text = open(path).read()
    except UnicodeDecodeError:
        continue
    unpinned += [f"{os.path.relpath(path, KIT)}:{n}: {call} without {pin}" for n, call, pin in unpinned_calls(text)]
failed |= bool(unpinned)
print(("FAIL a cross-model call doesn't pin its model and effort:\n     " + "\n     ".join(unpinned)) if unpinned
      else "ok   every codex exec and claude -p in the skills pins its model and effort")
sys.exit(1 if failed else 0)
