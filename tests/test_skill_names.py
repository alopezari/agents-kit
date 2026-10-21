#!/usr/bin/env python3
"""The kit's own skills go by their kit- names, and nothing the agent reads still sends it to a former one."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import re
import sys

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
SKILLS = ("kit-spec", "kit-review", "kit-validate", "kit-create-pr", "kit-follow-pr", "kit-ship", "kit-write-pr-description")
FORMER = r"(?:spec|self-review|validate|create-pr|follow-pr|ship|write-pr-description)"
# `spec` skill, the self-review or validate skills, run `spec`, /spec, skills/spec/, ../spec/: phase names alone stay unprefixed.
SENDS_TO_FORMER = re.compile(rf"(?<![\w/-])`?{FORMER}`?(?:(?:,| and| or) `?{FORMER}`?)* skills?\b|\brun `{FORMER}`|`/{FORMER}\b|(?:skills|\.\.)/{FORMER}(?![\w-])")
# The review's former name, kept on purpose: the stamp file, the phase alias, old reports' header.
KEPT = re.compile(r"self-review\.stamp|FORMER_NAMES|\(Self-review\|Review\)|\"Self-review\" heads")
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
                  if SENDS_TO_FORMER.search(line) or ("self-review" in line.lower() and not KEPT.search(line))]
failed |= bool(stale)
print(("FAIL a former skill name is still read by the agent:\n     " + "\n     ".join(stale)) if stale
      else "ok   no instruction, message or path names a former skill")
sys.exit(1 if failed else 0)
