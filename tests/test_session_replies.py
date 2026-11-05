#!/usr/bin/env python3
"""The agent's reply is a colleague's account, not a fixed set of headings: no text the agent reads or the docs repeat
names them, and AGENTS.md keeps the honesty rules the headings used to carry."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import re
import sys

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
NAMES = r"(?:Status|What it does|How sure|Review and testing|Blocked on me|Changed|Found)"
# Outside AGENTS.md's Communication section only the heading forms count: "Status" in a stored format or "What it
# does" as a table column stay legitimate there. Inside it, the plain words are banned too.
AS_HEADING = re.compile(rf"How sure|Blocked on me|\*\*{NAMES}:?\*\*|^#{{2,}}\s*{NAMES}\b|under {NAMES}\b", re.M)
PLAIN = re.compile(rf"\b{NAMES}\b")
READ_OR_PUBLISHED = ["AGENTS.md", "hooks/*.py", "skills/**/*", "bin/*", "docs/framework.md", "site/src/*.html"]
# What AGENTS.md's Communication section keeps from the old headings, each as a clause of the new text.
KEPT_RULES = {
    "no fixed headings": "No fixed headings",
    "no topic only to say nothing happened": "Don't mention a topic only to say nothing happened",
    "only what was observed": "never a merge, deploy or check assumed to have happened",
    "what was proven and what wasn't": "Say what was proven and how, and what wasn't",
    "stale evidence": "Evidence that no longer covers the current change is stale, not proof",
    "the status words": "ready to merge (CI green and reviews answered), merged (the flow's end)",
    "ship states only after ship": "only once I asked for `ship`, deployed, post-deploy checks pending or done",
    "owners": "You own what the kit lets you run. Merge and deploy are mine",
    "unreachable guide steps are the user's": "the checks guide's steps you can't reach",
    "ship after a merge is optional": "is optional and on request",
    "a user step names its commands": "the commands with their prerequisites and expected result",
    "a blocking question keeps a recommendation": "as options with a recommendation and what each costs",
    "the page is linked by absolute path": "link it by its absolute path",
    "the page's tables aren't repeated": "don't repeat its tables, counts or guide steps",
    "a stale page isn't current": "never link a page written before the last change, or an older file, as current",
    "a failed page gives the error and counts": "give the error, say each verdict and its counts in a sentence",
    "a failed page gives the paths": "give each report's absolute path and the checks guide's",
    "reports are listed before a page exists": "Before there is a page, give each report's absolute path",
    "switched-off phases are said": "In such a reply, say which phases I switched off",
    "follow-ups are mentioned": "Follow-ups worth an issue go at the end of the reply",
}

failed = []
for pattern in READ_OR_PUBLISHED:
    paths = [p for p in glob.glob(os.path.join(KIT, pattern), recursive=True)
             if os.path.isfile(p) and "node_modules" not in p]
    if not paths:
        failed.append(f"{pattern}: matches no file, so it checks nothing")
    for path in paths:
        try:
            lines = open(path).read().splitlines()
        except UnicodeDecodeError:
            continue
        failed += [f"{os.path.relpath(path, KIT)}:{n}: {line.strip()[:110]}" for n, line in enumerate(lines, 1)
                   if AS_HEADING.search(line)]

agents = open(os.path.join(KIT, "AGENTS.md")).read()
communication = agents.split("\n## Communication", 1)[1].split("\n## ", 1)[0]
failed += [f"AGENTS.md Communication: {line.strip()[:110]}" for line in communication.splitlines()
           if PLAIN.search(line)]
print(("FAIL a fixed reply heading is still named:\n     " + "\n     ".join(failed)) if failed
      else "ok   no instruction, message or doc names a fixed reply heading")

lost = [rule for rule, phrase in KEPT_RULES.items() if phrase not in communication]
print(("FAIL AGENTS.md's Communication section lost: " + ", ".join(lost)) if lost
      else "ok   AGENTS.md's Communication section keeps the honesty rules")
sys.exit(1 if failed or lost else 0)
