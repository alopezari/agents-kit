#!/usr/bin/env python3
"""bin/docs renders docs/framework.md whole: every table in it stays one table, and only the files in bin/ are tools."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import importlib.machinery
import importlib.util
import os
import re
import shutil
import sys
import tempfile

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

# A stray bin/__pycache__ (a test that imported a tool) crashed the tools table with IsADirectoryError.
kit, docs.KIT = docs.KIT, tempfile.mkdtemp(prefix="agents-test-docs-")
os.makedirs(os.path.join(docs.KIT, "bin", "__pycache__"))
with open(os.path.join(docs.KIT, "bin", "a-script"), "w") as fh:
    fh.write("#!/bin/bash\n# Does one thing.\n")
try:
    listed = [row[0] for row in docs.tools()]
    stray = None if listed == ["a-script"] else f"listed {listed}"
except OSError as error:
    stray = f"{type(error).__name__}: {error}"
finally:
    shutil.rmtree(docs.KIT)
    docs.KIT = kit
print(f"FAIL a folder in bin/ breaks the tools table: {stray}" if stray else "ok   a folder in bin/ is not a tool")
sys.exit(1 if split or stray else 0)
