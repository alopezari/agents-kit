#!/usr/bin/env python3
"""bin/triage's payments signal: a shop's checkout raises the risk, a git checkout doesn't."""
import json
import os
import subprocess
import sys
import tempfile

TRIAGE = os.path.expanduser("~/.agents/bin/triage")
GIT_SENSE = ["cwd = pr_checkout(command, cwd)", "git checkout -b fix", "- uses: actions/checkout@v4",
             "# the main checkout serves the stack", "for path in known_checkouts():"]
SHOP_SENSE = ["return wc_get_checkout_url();", "'wc-blocks-checkout',", "if ( is_checkout() ) {",
              "// Redirect to the checkout page", "git checkout-free refund( $order );"]


def payments_signal(line):
    with tempfile.TemporaryDirectory(prefix="agents-test-triage-") as repo:
        def git(*args):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "base")
        open(os.path.join(repo, "code.php"), "w").write(line + "\n")
        git("add", "code.php")
        git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "change")
        out = subprocess.run([TRIAGE, "--json", "--range", "HEAD~1..HEAD"], cwd=repo, capture_output=True, text=True)
        return any("payments" in reason for reason in json.loads(out.stdout)["reasons"])


fail = 0
for line, expected in [(l, False) for l in GIT_SENSE] + [(l, True) for l in SHOP_SENSE]:
    ok = payments_signal(line) == expected
    fail |= not ok
    print(f"{'ok  ' if ok else 'FAIL'} {'payments' if expected else 'no payments'}: {line}")
sys.exit(fail)
