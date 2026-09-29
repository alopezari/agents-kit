#!/usr/bin/env python3
"""skills/spec: the lint that checks a spec's shape, and the prompt for its second reading."""
import os
import shutil
import subprocess
import sys
import tempfile

KIT = os.path.expanduser("~/.agents")
os.environ["TMPDIR"] = tempfile.mkdtemp(prefix="agents-test-spec-tmp-")  # path.sh moves specs out of $TMPDIR
LINT = os.path.join(KIT, "skills", "spec", "lint.py")
REVIEW_PROMPT = os.path.join(KIT, "skills", "spec", "review-prompt.sh")
SPEC_PATH = os.path.join(KIT, "skills", "spec", "path.sh")

GOOD = """# SHOP-12: Require Compose v2

Goal: The installer accepts a standalone Compose v2 binary and rejects v1 with install steps.

## Acceptance criteria
1. A standalone `docker-compose` v2 is accepted — verify: tests/test_compose.py::accepts_v2
2. Compose v1 is rejected with the install steps, and the
   installer exits 1 — verify: run `./install --dry-run` with a v1 stub on PATH
3. The plugin form `docker compose` still works — verify: tests/test_compose.py::plugin

## Out of scope
- Podman.

## Assumptions
- v2.0.0 is the lowest version we accept.

## Open questions
- None.
"""


def lint(base, text):
    path = os.path.join(base, "spec.md")
    open(path, "w").write(text)
    result = subprocess.run([LINT, path], capture_output=True, text=True)
    return result.returncode, result.stdout


def lint_passes_the_documented_shape(base):
    status, out = lint(base, GOOD)
    assert status == 0 and "FAIL" not in out and "warn" not in out, out


def lint_names_every_missing_part(base):
    text = GOOD.replace("# SHOP-12: Require Compose v2\n", "").replace("Goal: The installer", "The installer")
    text = text.replace("## Out of scope\n- Podman.\n\n", "")
    text = text.replace(" — verify: tests/test_compose.py::plugin", "")
    status, out = lint(base, text)
    assert status == 1, out
    for problem in ("no title", "no 'Goal:' line", "no '## Out of scope' section", "criterion 3 has no 'verify:'"):
        assert problem in out, (problem, out)
    assert "criterion 2" not in out, f"a verify on a continuation line counts: {out}"
    status, out = lint(base, GOOD.replace("verify: tests/test_compose.py::plugin", "verify the plugin fixture"))
    assert status == 1 and "criterion 3: write its check as" in out, f"a verify without its colon says how to fix it: {out}"


def lint_takes_a_change_map_only_when_filled(base):
    filled = GOOD + "\n## Change map\n- Ways in: installer CLI — install.sh:40\n"
    status, out = lint(base, filled)
    assert status == 0, f"a filled map passes, and a spec without one too (GOOD): {out}"
    status, out = lint(base, GOOD + "\n## Change map\n\n")
    assert status == 1 and "'## Change map' is empty" in out, out


def lint_rejects_a_placeholder_verify(base):
    for placeholder in ("", "tests", "Manual", "TBD", "unit tests.", "none", "see above", "run the tests"):
        status, out = lint(base, GOOD.replace("verify: tests/test_compose.py::plugin", f"verify: {placeholder}"))
        assert status == 1 and "criterion 3: 'verify:" in out, (placeholder, out)


def lint_warns_outside_three_to_six_criteria(base):
    extra = "".join(f"{n}. Case {n} — verify: tests/test_compose.py::case_{n}\n" for n in range(4, 9))
    status, out = lint(base, GOOD.replace("\n## Out of scope", extra + "\n## Out of scope"))
    assert status == 0 and "warn 8 criteria" in out, out
    status, out = lint(base, GOOD.replace("3. The plugin form", "The plugin form"))
    assert status == 0 and "warn 2 criteria" in out, out
    six = "".join(f"{n}. Case {n} — verify: tests/test_compose.py::case_{n}\n" for n in range(4, 7))
    status, out = lint(base, GOOD.replace("\n## Out of scope", six + "\n## Out of scope"))
    assert status == 0 and "warn" not in out, out


def lint_exits_2_on_an_unreadable_spec(base):
    result = subprocess.run([LINT, os.path.join(base, "missing.md")], capture_output=True, text=True)
    assert result.returncode == 2 and "cannot read the spec" in result.stderr, result


def review_prompt_bundles_the_request_and_the_spec(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    subprocess.run(["git", "init", "-q", "-b", "trunk"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "i"],
                   cwd=repo, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "feature/compose"], cwd=repo, check=True)

    def prompt(request):
        return subprocess.run([REVIEW_PROMPT], cwd=repo, input=request, capture_output=True, text=True)

    missing = prompt("Support Compose v2 (#12)")
    assert missing.returncode == 2 and "no spec for this branch" in missing.stderr, missing.stderr
    spec = subprocess.run([SPEC_PATH], cwd=repo, capture_output=True, text=True).stdout.strip()
    open(spec, "w").write(GOOD)
    empty = prompt(" \n")
    assert empty.returncode == 2 and "pass the request" in empty.stderr, empty.stderr
    full = prompt("Support Compose v2 (#12)\nComment: v1 is end of life.")
    assert full.returncode == 0, full.stderr
    request_at, spec_at = full.stdout.index("v1 is end of life"), full.stdout.index("Goal: The installer")
    assert full.stdout.index("misreads") < request_at < spec_at, full.stdout


RESULTS = []
for test in (lint_passes_the_documented_shape, lint_names_every_missing_part, lint_rejects_a_placeholder_verify,
             lint_warns_outside_three_to_six_criteria, lint_exits_2_on_an_unreadable_spec,
             lint_takes_a_change_map_only_when_filled,
             review_prompt_bundles_the_request_and_the_spec):
    base = tempfile.mkdtemp(prefix="agents-test-spec-")
    try:
        test(base)
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {error}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

shutil.rmtree(os.environ["TMPDIR"], ignore_errors=True)
for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
sys.exit(1 if any(error for _, error in RESULTS) else 0)
