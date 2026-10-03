"""Imported first by every Python test: the tests reach the kit through ~/.agents, so it must be this checkout."""
import os
import sys

CHECKOUT = os.path.realpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
INSTALLED = os.path.realpath(os.path.expanduser("~/.agents"))
if CHECKOUT != INSTALLED:
    print(f"error: these tests run the kit at ~/.agents ({INSTALLED}), not this checkout ({CHECKOUT}).\n"
          f"Run {CHECKOUT}/tests/run.sh <section>, or set HOME to a directory whose .agents links to this checkout.",
          file=sys.stderr)
    sys.exit(2)
