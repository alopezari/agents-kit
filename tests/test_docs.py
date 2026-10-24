#!/usr/bin/env python3
"""bin/docs renders docs/framework.md whole: every table in it stays one table."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.machinery
import importlib.util
import os
import re
import sys

sys.dont_write_bytecode = True  # loading bin/docs would leave bin/__pycache__ behind
_loader = importlib.machinery.SourceFileLoader("docs", os.path.expanduser("~/.agents/bin/docs"))
docs = importlib.util.module_from_spec(importlib.util.spec_from_loader("docs", _loader))
_loader.exec_module(docs)

lines = docs.render().splitlines()
SEPARATOR = re.compile(r"^\|[-:| ]+\|$")
# A blank line ends a markdown table: a row after it, with no header separator below it, renders as a paragraph.
split = [f"line {n + 3}: {lines[n + 2][:60]}" for n in range(len(lines) - 2)
         if lines[n].startswith("|") and lines[n + 1] == "" and lines[n + 2].startswith("|")
         and not (n + 3 < len(lines) and SEPARATOR.match(lines[n + 3]))]
print(("FAIL a blank line splits a table in docs/framework.md:\n     " + "\n     ".join(split)) if split
      else "ok   no blank line splits a table in docs/framework.md")
sys.exit(1 if split else 0)
