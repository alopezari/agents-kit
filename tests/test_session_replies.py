#!/usr/bin/env python3
"""The agent's reply is a colleague's account, not a fixed set of headings: no text the agent reads or the docs repeat
names them, and AGENTS.md keeps the honesty rules the headings used to carry."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import glob
import os
import re
import sys

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
# Exact heading tokens only: "Status" in a stored format or "What it does" as a table column stay legitimate.
HEADINGS = re.compile(r"How sure|Blocked on me|\*\*(?:Status|What it does|Review and testing|Changed|Found)\*\*|under Found")
READ_OR_PUBLISHED = ["AGENTS.md", "hooks/*.py", "skills/**/*", "bin/*", "docs/framework.md", "site/src/index.html"]
# What AGENTS.md's Communication section keeps from the old headings, each as a phrase of the new text.
KEPT_RULES = {
    "only what was observed": "never a merge, deploy or check assumed",
    "stale evidence": "stale, not proof",
    "a gap that matters is said": "gap that matters",
    "merge is the user's unless asked": "unless my message asks you to merge",
    "ship after a merge is optional": "optional",
    "a user step names its commands": "commands with their prerequisites and expected result",
    "a blocking question keeps a recommendation": "recommendation",
    "follow-ups are mentioned": "worth an issue",
    "no fixed headings": "no empty category",
    "the page is linked by absolute path": "link it by its absolute path",
    "the page's tables aren't repeated": "don't repeat its tables, counts or guide steps",
    "a failed page gives the error": "give the error",
    "an older file isn't current": "never link an older file as current",
    "reports are listed before a page exists": "each report's absolute path",
}

failed = []
for pattern in READ_OR_PUBLISHED:
    for path in glob.glob(os.path.join(KIT, pattern), recursive=True):
        if not os.path.isfile(path) or "node_modules" in path:
            continue
        try:
            lines = open(path).read().splitlines()
        except UnicodeDecodeError:
            continue
        failed += [f"{os.path.relpath(path, KIT)}:{n}: {line.strip()[:110]}" for n, line in enumerate(lines, 1)
                   if HEADINGS.search(line)]
print(("FAIL a fixed reply heading is still named:\n     " + "\n     ".join(failed)) if failed
      else "ok   no instruction, message or doc names a fixed reply heading")

agents = open(os.path.join(KIT, "AGENTS.md")).read()
communication = agents.split("\n## Communication", 1)[1]
lost = [rule for rule, phrase in KEPT_RULES.items() if phrase not in communication]
print(("FAIL AGENTS.md's Communication section lost: " + ", ".join(lost)) if lost
      else "ok   AGENTS.md's Communication section keeps the honesty rules")
sys.exit(1 if failed or lost else 0)
