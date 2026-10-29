#!/usr/bin/env python3
"""bin/reports html: the branch's decision page, built from its spec, reports and evidence."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import datetime
import html
import html.parser
import json
import os
import re
import resource
import shutil
import subprocess
import tempfile
import urllib.parse

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
# A HOME of our own: the page reads the user's phase switches, and the real ones would change it.
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-decision-report-home-")
os.makedirs(os.path.expanduser("~/.agents"))
for entry in set(os.listdir(KIT)) - {"approvals", "logs", "repos"}:
    os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
os.makedirs(os.path.expanduser("~/.agents/repos"))  # a ship.md written here must not land in the kit's own overlays
REPORTS = os.path.join(KIT, "bin", "reports")
RESULTS = []
CSP = ("default-src 'none'; img-src file: data:; media-src file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
HEADINGS = ["Coverage", "Tests", "Review", "Merge and ship", "Reports"]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82")
# gh as the page calls it: `pr view`, then the check runs and statuses of one commit, from FAKE_GH_DIR/gh.json.
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, re, sys
state = json.load(open(os.path.join(os.environ["FAKE_GH_DIR"], "gh.json")))
args = sys.argv[1:]
open(os.path.join(os.environ["FAKE_GH_DIR"], "calls.log"), "a").write(" ".join(args) + "\n")
if state.get("error"):
    print("error connecting to api.github.com", file=sys.stderr)
    sys.exit(1)
if args[:2] == ["pr", "view"]:
    if not state.get("pr"):
        print('no pull requests found for branch "feature/cart"', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(state["pr"]))
    sys.exit(0)
found = re.search(r"commits/([0-9a-f]+)/(check-runs|status)", args[-1])
if args[:1] == ["api"] and state.get("api_error"):
    print("HTTP 502: bad gateway", file=sys.stderr)
    sys.exit(1)
if args[:1] == ["api"] and found:
    key = "check_runs" if found.group(2) == "check-runs" else "statuses"
    for item in state.get("checks", {}).get(found.group(1), {}).get(key, []):
        print(json.dumps(item))
    sys.exit(0)
print("fake gh: unexpected " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def sh(cwd, *cmd, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env)


def git(repo, *args):
    out = sh(repo, "git", "-c", "user.name=t", "-c", "user.email=t@t", *args)
    assert out.returncode == 0, out
    return out.stdout.strip()


def put(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb" if isinstance(text, bytes) else "w") as fh:
        fh.write(text)


def path_of(repo, kind):
    out = sh(repo, os.path.join(KIT, "skills", "kit-spec", "path.sh")) if kind == "spec" else sh(repo, REPORTS, "path", kind)
    assert out.returncode == 0, out
    return out.stdout.strip()


def write(repo, kind, text):
    put(path_of(repo, kind), text)


def working_tree(repo):
    """The tree bin/evidence records for the working tree as it is."""
    index = os.path.join(tempfile.mkdtemp(prefix="agents-test-index-"), "index")
    env = {**os.environ, "GIT_INDEX_FILE": index}
    assert sh(repo, "git", "add", "-A", env=env).returncode == 0
    tree = sh(repo, "git", "write-tree", env=env).stdout.strip()
    shutil.rmtree(os.path.dirname(index))
    return tree


def output(evidence, name, command, body, repo, tree, exit_code=0):
    """An output saved the way bin/evidence saves it."""
    head = git(repo, "rev-parse", "HEAD")
    put(os.path.join(evidence, name), f"# in {repo}, 2026-10-26 10:00:00 +0000, exit {exit_code}, commit {head}, tree {tree}\n"
                                      f"$ {command}\n\n{body}")


def new_repo(base):
    """shop on feature/cart: main has src/cart.py (2 lines); the branch replaces one line with three and adds a doc."""
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main")
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nb = 2\n")
    put(os.path.join(repo, "tests", "test_cart.py"), "def test(): pass\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "feature/cart")
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nc = 3\nd = 4\ne = 5\n")
    put(os.path.join(repo, "README.md"), "Shop\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "coupon once")
    return repo


SPEC = """# Coupon once

Goal: a shopper gets one discount per coupon, even on refresh.

Risk: standard — cart totals.

## Acceptance criteria
1. A coupon applies once. — verify: test.
2. A tampered total is refused. — verify: test.
3. The receipt names the coupon. — verify: manual.
"""

NOTES = """## What the task must do
The task must stop a second discount for one coupon.

## What the change does
The change adds a guard in `src/cart.py:2`.

### Where it sits

```text
cart
└── apply_coupon -> guard
```

## What it changes for users
A shopper sees one discount line.

## What you must decide
Decide if one FAIL on the phone check blocks the merge.
"""

REVIEW = """Review (risk: standard; lenses: correctness, tests; cross-model: codex on correctness; re-check rounds: 1)

## Spec table

| Criterion | Met? | Evidence |
|---|---|---|
| 1. A coupon applies once | Yes | tests/test_cart.py |

## Findings

Fixed:     a coupon applied twice on refresh — src/cart.py:40 — test_coupon_once
Fixed (re-check): the total ignored tax — src/cart.py:52
Rejected:  a race on checkout — handled at src/cart.py:80
Pre-existing: the log grows without a bound — src/log.py:9
Open:      totals in two currencies collapse — src/cart.py:77 — decision for the human
Not run:   security lens — no auth code touched
"""

GUIDE = """# Checks guide

## Before the merge (optional)

### M1. Pay on a phone
- **Where:** you, on a phone
- **Run:**
```bash
open "shop.test/cart?coupon=ONE"
```
- **Expected:** one discount line on the phone.
- **If not:** send the screenshot of the cart.

### M2. Refund in the sandbox
- **Where:** you, in the payment sandbox
- **Expected:** the refund is issued.
- **If not:** stop and report the refund id.

## After the merge

### P1. Backfill the totals
- **Where:** the agent, on this machine
- **Run:**
```bash
./backfill --dry-run
```
- **Expected:** zero errors from the backfill.
- **If not:** roll the backfill back.

### P2. Watch the error log
- **Where:** you, on the production host
- **Expected:** a quiet error log for an hour.
- **If not:** page the cart owner.

## Results (2026-10-26)

| Step | Result | Evidence |
|---|---|---|
| M1 | FAIL | the coupon doubled on Safari |
| M2 | PASS | refund seen |
"""

FOLLOW_PR = """## Run 2026-10-26 10:00
CI:      suite: pass
c123 https://github.com/o/shop/pull/7#c123 — reviewer — Uncertain — asks about rounding — reply drafted
Status:  waiting on reviewer
"""


def full_branch(base, notes=True, pr_body=True):
    """A branch with every report, a stale output (A2), a freshness warning (environment.txt) and a missing file (A4)."""
    repo = new_repo(base)
    evidence = path_of(repo, "evidence")
    current = working_tree(repo)
    first_tree = git(repo, "rev-parse", "main^{tree}")
    put(path_of(repo, "spec"), SPEC)
    if notes:
        write(repo, "decision-notes", NOTES)
    if pr_body:
        write(repo, "pr-body", "The cart applies a coupon once.\nA refresh no longer adds a second discount.\n\n"
                               "Second paragraph with detail.\n")
    write(repo, "verify", "# Verify: PASS\n\n2026-10-26 · verify in shop\n\nran: tests/run.sh cart\n")
    write(repo, "review", REVIEW)
    output(evidence, "A1.txt", "make test-cart", "ok 12 rows\n", repo, current)
    output(evidence, "A2.txt", "make tamper", "exit 0 on a tampered total\n", repo, first_tree)
    output(evidence, "environment.txt", "evidence-env", "php 8.3\n", repo, "ab" * 20)
    put(os.path.join(evidence, "shot.png"), PNG)
    put(os.path.join(evidence, "run.webm"), b"\x1aE\xdf\xa3 not really a video")
    put(os.path.join(evidence, "checks", "A1.sh"), "#!/bin/bash\nmake test-cart # kept script\n")
    write(repo, "validation", f"""# Validation: cart

Verdict: FAIL (2 PASS, 1 FAIL, 1 NOT RUN).

## A. Cart

| # | AC | Check | Case (+/−) | Expected | Result | Evidence |
|---|---|---|---|---|---|---|
| A1 | AC1 | Coupon applied once | + | one discount line | PASS | one line seen: [output]({evidence}/A1.txt), [shot]({evidence}/shot.png), [video]({evidence}/run.webm) |
| A2 | AC1, AC2 | Tampered total refused | − | exit 1 | **FAIL** | exit 0 instead: [output]({evidence}/A2.txt) |
| A3 | AC2 | Admin list loads | + | the list is shown | NOT RUN | needs the payment sandbox |
| A4 | AC2 | Old log kept | − | log unchanged | PASS | [output]({evidence}/gone.txt) |

## Test types

| Type | Ran | Evidence |
|---|---|---|
| unit | yes | [A1]({evidence}/A1.txt) |

## Notes
- Only unit-tested: the refund path.
- Not run locally: A3 needs the payment sandbox.
""")
    write(repo, "checks-guide", GUIDE)
    write(repo, "follow-pr", FOLLOW_PR)
    return repo, evidence


def fake_gh(base, state):
    bin_dir = os.path.join(base, "fake-bin")
    put(os.path.join(bin_dir, "gh"), FAKE_GH)
    os.chmod(os.path.join(bin_dir, "gh"), 0o755)
    put(os.path.join(base, "gh.json"), json.dumps(state))
    return {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE_GH_DIR": base}


def generate(repo, base, gh=None):
    out = sh(repo, REPORTS, "html", env=fake_gh(base, gh or {}))
    assert out.returncode == 0, out
    page = out.stdout.strip()
    assert os.path.isabs(page) and os.path.isfile(page), out
    with open(page) as fh:
        return page, fh.read()


def text(markup):
    """The page's text, tags dropped and entities decoded, whitespace collapsed."""
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", markup)).split())


def section(page, number):
    """A numbered section: a <details> holding nested <details>, so it ends where the next section or <main> does."""
    found = re.search(rf'<details class="sec"[^>]* id="s0{number}".*?(?=<details class="sec"|</main>)', page, re.S)
    assert found, f"no section 0{number}"
    return found.group(0)


def is_open(page, number):
    found = re.search(rf'<details class="sec"([^>]*) id="s0{number}"', page)
    assert found, f"no section 0{number}"
    return " open" in found.group(1)


def summary(page):
    found = re.search(r'<section id="summary".*?</section>', page, re.S)
    assert found, "no summary"
    return found.group(0)


def summary_row_markup(page, name):
    found = re.search(rf'<div class="row"><dt id="sum-{name}">(.*?)(?=<div class="row">|</dl>)', summary(page), re.S)
    assert found, f"no {name} row in the summary"
    return found.group(1)


def summary_row(page, name):
    return text(summary_row_markup(page, name))


def listed(page, name):
    """The items of one of the summary's lists: decisions, blockers or warnings."""
    markup = summary(page)
    start = markup.find(f'<ol class="items" id="{name}-items">')
    assert start >= 0, f"no {name} list in the summary"
    items, depth, item_start = [], 0, None
    for tag in re.finditer(r"<(/?)(ol|li)\b[^>]*>", markup[start:]):
        closing, kind = tag.group(1), tag.group(2)
        depth += -1 if closing else 1
        if kind == "li" and depth == 2 and not closing:
            item_start = tag.end()
        elif kind == "li" and depth == 1 and closing:
            items.append(text(markup[start:][item_start:tag.start()]))
        elif depth == 0:
            return items
    raise AssertionError(f"the {name} list is not closed")


def footer(page):
    found = re.search(r"<footer.*?</footer>", page, re.S)
    assert found, "no footer"
    return found.group(0)


def footer_value(page, name):
    found = re.search(rf"<dt>{name}</dt>\s*<dd[^>]*>(.*?)</dd>", footer(page), re.S)
    assert found, f"no {name} in the footer"
    return text(found.group(1))


def check_block(page, check_id):
    found = re.search(rf'<li class="check[^"]*" id="check-{check_id}">.*?</details></li>', page, re.S)
    assert found, f"no block for {check_id}"
    return found.group(0)


def decide_items(page):
    """Every item of the summary's three lists."""
    return listed(page, "decisions") + listed(page, "blockers") + listed(page, "warnings")


class Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


def tags(page):
    parser = Tags()
    parser.feed(page)
    return parser.tags


# AC1

def writes_the_page_with_its_csp_and_prints_its_path(base):
    repo, _ = full_branch(base)
    page, markup = generate(repo, base)
    assert page == os.path.join(os.path.dirname(path_of(repo, "spec")), "decision-shop-feature~cart.html"), page
    metas = [dict(attrs) for tag, attrs in tags(markup) if tag == "meta" and dict(attrs).get("http-equiv")]
    assert [m["content"] for m in metas if m["http-equiv"] == "Content-Security-Policy"] == [CSP], metas
    for tag, attrs in tags(markup):
        for name, value in attrs:
            remote = re.match(r"\s*(?!file:)([a-z]+:)?//", value or "", re.I)
            # a link the reader clicks may leave the page, and only to https; nothing loads from elsewhere
            assert not (name in ("src", "action", "poster", "srcset") and remote), (tag, name, value)
            assert not (name == "href" and remote and not (tag == "a" and value.startswith("https://"))), (tag, name, value)
    assert not re.search(r"url\(|@import", markup), "a stylesheet reaches out"
    assert git(repo, "rev-parse", "HEAD") in footer_value(markup, "HEAD read")
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d", footer_value(markup, "Generated")), footer(markup)
    assert "local file, not published" in text(footer(markup))


def bounds_what_it_embeds(base):
    repo, evidence = full_branch(base)
    big = "FIRST LINE\n" + "".join(f"middle {n:07d}\n" for n in range(20000)) + "LAST LINE\n"
    output(evidence, "big.txt", "make big", big, repo, working_tree(repo))
    put(os.path.join(evidence, "huge.png"), PNG + b"\0" * (2 * 1024 * 1024 + 10))
    names = []
    for n in range(11):  # 11 images of 1.9 MB: past the 20 MB total from the 11th on
        names.append(f"img{n:02d}.png")
        put(os.path.join(evidence, names[-1]), PNG + bytes([n]) * (1900 * 1024))
    rows = "".join(f"| B{n} | AC1 | image {n} | + | an image | PASS | [img]({evidence}/{name}) |\n" for n, name in enumerate(names))
    output(evidence, "under.txt", "make under", "UNDER FIRST\n" + "y" * (190 * 1024) + "\nUNDER LAST\n", repo, working_tree(repo))
    rows += f"| B92 | AC1 | an output under the bound | + | all of it | PASS | [output]({evidence}/under.txt) |\n"
    for n in range(93, 100):  # 195 KB each: the images leave about 1.5 MB, so the last ones are past the total
        output(evidence, f"late{n}.txt", "make late", f"LATE TEXT {n}\n" + "z" * (195 * 1024), repo, working_tree(repo))
        rows += f"| B{n} | AC1 | an output past the total | + | a link | PASS | [output]({evidence}/late{n}.txt) |\n"
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Bounds\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B90 | AC1 | big output | + | all rows | PASS | [output]({evidence}/big.txt) |\n"
                 f"| B91 | AC1 | huge image | + | an image | PASS | [img]({evidence}/huge.png) |\n" + rows)
    _, markup = generate(repo, base)
    block = check_block(markup, "B90")
    assert "FIRST LINE" in block and "LAST LINE" in block, "the head or tail of an oversize output is gone"
    assert "middle 0010000" not in markup, "an oversize output was embedded whole"
    assert re.search(r"\bcut\b", text(block)), text(block)[:400]
    cut = re.search(r'<p class="cut">.*?</p>', block, re.S)
    assert cut and f'href="file://{urllib.parse.quote(os.path.realpath(evidence))}/big.txt"' in cut.group(0), "no link to the full output"
    under = check_block(markup, "B92")
    assert "UNDER FIRST" in under and "UNDER LAST" in under and 'class="cut"' not in under, "an output under 200 KB was cut"
    late = check_block(markup, "B99")
    assert "LATE TEXT" not in late and "/late99.txt\"" in late and "past the 20 MB" in text(late), "a text file past the total was embedded"
    huge = check_block(markup, "B91")
    assert "data:image" not in huge and f"/huge.png\"" in huge, huge[:600]
    embedded = [n for n in range(11) if "data:image/png" in check_block(markup, f"B{n}")]
    linked = [n for n in range(11) if n not in embedded and f"/img{n:02d}.png\"" in check_block(markup, f"B{n}")]
    assert embedded and linked and embedded == list(range(len(embedded))) and len(embedded) + len(linked) == 11, (embedded, linked)
    assert len(embedded) * 1900 * 1024 <= 20 * 1024 * 1024, embedded


def reads_nothing_outside_the_evidence_directory(base):
    repo, evidence = full_branch(base)
    outside = os.path.join(base, "secret.txt")
    put(outside, f"# in {base}, 2026-10-26 10:00:00 +0000, exit 0, commit {'cd' * 20}, tree {'cd' * 20}\nSECRET-TOKEN-1\n")
    put(os.path.join(base, "secret2.txt"), "SECRET-TOKEN-2\n")
    os.symlink(outside, os.path.join(evidence, "link.txt"))
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## C. Scope\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| C1 | AC1 | through a symlink | − | nothing read | PASS | [out]({evidence}/link.txt) |\n"
                 f"| C2 | AC1 | an absolute path | − | nothing read | PASS | [out]({base}/secret2.txt) and {base}/secret2.txt |\n"
                 f"| C4 | AC1 | a sibling directory | − | nothing read | PASS | [out]({evidence}2/secret4.txt) |\n")
    put(evidence + "2/secret4.txt", "SECRET-TOKEN-4\n")
    _, markup = generate(repo, base)
    assert "SECRET-TOKEN" not in markup, "a file outside the evidence directory was read"
    assert "link.txt ran on" not in text(markup), "the freshness check read the file a symlink points to outside"
    os.makedirs(os.path.join(base, "elsewhere"))
    put(os.path.join(base, "elsewhere", "C3.outside-script.sh"), "echo SECRET-TOKEN-3\n")
    shutil.rmtree(os.path.join(evidence, "checks"))
    os.symlink(os.path.join(base, "elsewhere"), os.path.join(evidence, "checks"))
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("| C3 | AC1 | a symlinked checks directory | − | nothing listed | PASS | none |\n")
    _, markup = generate(repo, base)
    assert "outside-script" not in markup, "a checks/ directory outside the evidence directory was listed"
    assert f"{base}/secret2.txt" in text(check_block(markup, "C2")), "the outside path isn't shown as text"
    assert "file://" + urllib.parse.quote(os.path.realpath(outside)) not in markup


def a_long_last_line_keeps_its_tail(base):
    repo, evidence = full_branch(base)
    output(evidence, "long.txt", "make long", "FIRST LINE\n" + "x" * 300_000 + " LAST WORDS\n", repo, working_tree(repo))
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Long\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B1 | AC1 | one long line | + | its end | PASS | [output]({evidence}/long.txt) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "B1")
    assert "LAST WORDS" in block, "the tail of a file ending in one long line is gone"
    assert re.search(r"\bcut\b", text(block)), "no cut notice"
    assert len(block) < 250_000, len(block)


def unmatched_brackets_take_linear_time(base):
    repo, _ = full_branch(base)
    write(repo, "decision-notes", NOTES.replace("A shopper sees one discount line.", "[" * 60_000 + "]"))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for 60,000 unmatched brackets"


def a_long_check_table_takes_linear_time(base):
    repo, _ = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## E. Many\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 + "".join(f"| E{n} | AC1 | row {n} | + | ok | PASS | none |\n" for n in range(8000)))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for 8,000 check rows"


def a_failed_generation_leaves_no_page(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "feature/cart")
    put(os.path.join(repo, "a.txt"), "a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a")
    page, _ = generate(repo, base)
    # A default branch that shares no commit with this one: the change can't be measured, so generation fails.
    git(repo, "checkout", "-q", "--orphan", "main")
    git(repo, "commit", "-qm", "unrelated", "--allow-empty")
    git(repo, "checkout", "-q", "feature/cart")
    out = sh(repo, REPORTS, "html", env=fake_gh(base, {}))
    assert out.returncode != 0, out
    assert "no common commit" in out.stderr, out.stderr
    assert not os.path.exists(page), "the older page was left for the current one"
    assert not [n for n in os.listdir(os.path.dirname(page)) if n.startswith((".decision", "decision"))], \
        os.listdir(os.path.dirname(page))



def the_older_page_is_gone_while_it_builds(base):
    repo = os.path.join(base, "shop")
    os.makedirs(repo)
    git(repo, "init", "-q", "-b", "feature/cart")
    put(os.path.join(repo, "a.txt"), "a\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "a")
    page, _ = generate(repo, base)
    # gh runs mid-build: a kill there must not leave the older page to be taken for the current one.
    env = fake_gh(base, {})
    spy = os.path.join(base, "spy-bin")
    put(os.path.join(spy, "gh"), f'#!/bin/sh\n[ -e "{page}" ] && echo present >> "{base}/seen" || echo absent >> "{base}/seen"\n'
        f'exec "{base}/fake-bin/gh" "$@"\n')
    os.chmod(os.path.join(spy, "gh"), 0o755)
    out = sh(repo, REPORTS, "html", env={**env, "PATH": spy + ":" + env["PATH"]})
    assert out.returncode == 0, out
    with open(os.path.join(base, "seen")) as fh:
        seen = fh.read().split()
    assert seen and set(seen) == {"absent"}, seen

# AC2

def sections_come_in_order_with_the_notes(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    found = [(n, h) for n, h in re.findall(r'<h2[^>]*><span class="num">(\d\d)</span>\s*([^<]+)</h2>', markup)]
    assert found == [(f"0{i + 1}", h) for i, h in enumerate(HEADINGS)], found
    for row, line in (("task", "The task must stop a second discount"), ("solution", "The change adds a guard in"),
                      ("solution", "A shopper sees one discount line")):
        assert line in summary_row(markup, row), (row, summary_row(markup, row)[:300])
        assert "Written by the agent" in summary_row(markup, row), row
    assert any("Decide if one FAIL on the phone check" in d for d in listed(markup, "decisions"))
    assert path_of(repo, "decision-notes") in text(section(markup, 5))
    assert re.search(r"<h3[^>]*>Where it sits</h3>", summary(markup)), "a notes heading skips a level"
    levels = [int(n) for n in re.findall(r"<h(\d)", summary(markup))]
    assert all(b <= a + 1 for a, b in zip(levels, levels[1:])), f"a notes heading skips a level: {levels}"
    assert re.search(r'<pre[^>]*>(?:<code[^>]*>)?(?:[^<]|<span class="ln">|</span>)*└── apply_coupon', summary(markup)), "the notes' figure isn't preformatted"
    one = section(markup, 1)
    criteria = re.findall(r'<tr[^>]*id="ac-(\d+)">(.*?)</tr>', one, re.S)
    assert [n for n, _ in criteria] == ["1", "2", "3"], criteria
    first = text(criteria[0][1])
    assert "A1 PASS" in first and "A2 FAIL" in first and "A3" not in first, first
    assert "no check" in text(criteria[2][1]) and "no check" not in first, criteria
    five = text(section(markup, 5))
    assert "Goal: a shopper gets one discount per coupon" in five
    assert path_of(repo, "spec") in five
    assert "Second paragraph with detail." in five and path_of(repo, "pr-body") in five, five[:500]
    assert "src/ 1 +3 -1" in five and "(top level) 1 +1 -0" in five, five
    for kind in ("validation", "review", "verify", "checks-guide"):
        assert path_of(repo, kind) in five, kind
    listing = sh(repo, REPORTS)
    assert "decision-notes" not in listing.stdout, "the notes are listed with the reports"


def a_branch_without_notes_or_pr_body_says_not_recorded(base):
    repo, _ = full_branch(base, notes=False, pr_body=False)
    _, markup = generate(repo, base)
    for row in ("task", "solution"):
        assert "not recorded" in summary_row(markup, row), row
    assert "Written by the agent" not in text(summary(markup))
    assert "recommendation not recorded" in listed(markup, "decisions")[0]
    assert "a shopper gets one discount per coupon" in summary_row(markup, "task"), "no fallback to the spec's goal"
    for kind in ("decision-notes", "pr-body"):
        annex = re.search(rf'<details class="annex" id="annex-{kind}">.*?</details>', markup, re.S)
        assert "not recorded" in text(annex.group(0)) and "kit-ship" in text(annex.group(0)), kind


def falls_back_to_the_pr_body_first_paragraph(base):
    repo, _ = full_branch(base, notes=False)
    _, markup = generate(repo, base)
    solution = summary_row(markup, "solution")
    assert "The cart applies a coupon once." in solution, solution
    assert "Second paragraph" not in solution, "the fallback holds more than the first paragraph"


# AC3

def decide_names_every_item(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    items = decide_items(markup)
    joined = "\n".join(items)
    for name in ("totals in two currencies collapse", "A2", "A2.txt", "environment.txt", "gone.txt", "M1", "c123"):
        assert name in joined, (name, items)
    assert not any(re.search(r"\bM2\b", item) for item in items), items
    assert any("M1" in item and "FAIL" in item for item in listed(markup, "blockers")), items
    assert any("environment.txt" in item and "doesn't have" in item for item in items), items
    assert any("A2.txt" in item and "src/cart.py" in item for item in items), items
    after = text(section(markup, 4))
    assert re.search(r"P1 Backfill the totals: left", after) and re.search(r"P2 Watch the error log: left", after), after[:900]
    for count in ("Fixed 2", "Rejected 1", "Pre-existing 1", "Open 1", "Not run 1"):
        assert count in text(section(markup, 3)), count
    for count in ("PASS 2", "FAIL 1", "NOT RUN 1"):
        assert count in text(section(markup, 2)), count
    gates = summary_row(markup, "gates")
    assert "review stamp missing" in gates and "validate stamp missing" in gates and "verify PASS" in gates, gates
    assert "tests/run.sh cart" in footer_value(markup, "Verify")
    links = re.findall(r'<ol class="items" id="\w+-items">.*?</ol>', summary(markup), re.S)
    for target in re.findall(r'href="#([^"]+)"', "".join(links)):
        assert f'id="{target}"' in markup, target
    assert 'href="#check-A2"' in "".join(links) and 'href="#decisions"' in markup


def a_symlink_to_a_file_inside_keeps_its_freshness(base):
    repo, evidence = full_branch(base)
    os.makedirs(os.path.join(evidence, "saved"))
    os.rename(os.path.join(evidence, "A2.txt"), os.path.join(evidence, "saved", "A2.txt"))
    os.symlink(os.path.join(evidence, "saved", "A2.txt"), os.path.join(evidence, "A2.txt"))
    _, markup = generate(repo, base)
    items = decide_items(markup)
    assert any("A2.txt" in item and "src/cart.py" in item for item in items), items


def decide_keeps_what_later_sources_drop(base):
    repo, evidence = full_branch(base)
    os.remove(os.path.join(evidence, "environment.txt"))
    with open(path_of(repo, "follow-pr"), "a") as fh:
        fh.write("\n## Run 2026-10-27 09:00\nCI:      suite: pass\n"
                 "c456 https://github.com/o/shop/pull/7#c456 — reviewer — Confirmed — a typo — fix commit abc123\n"
                 "Status:  waiting on reviewer\n")
    os.chmod(path_of(repo, "checks-guide"), 0)
    _, markup = generate(repo, base)
    joined = "\n".join(decide_items(markup))
    assert "c123" in joined, "a comment left pending by an earlier follow-pr run was dropped"
    assert "c456" not in joined, "a comment fixed in a later run is still pending"
    assert re.search(r"environment\.txt.*missing", joined), joined
    assert re.search(r"[Cc]hecks guide.*unavailable", joined), joined


def ci_is_read_for_the_pr_head_only(base):
    repo, _ = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    old = git(repo, "rev-parse", "main")
    green = {"check_runs": [{"name": "suite", "status": "completed", "conclusion": "success", "details_url": ""}]}
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Apply a coupon once", "headRefOid": head,
          "state": "OPEN"}
    _, markup = generate(repo, base, {"pr": pr, "checks": {old: green}})
    ci = footer_value(markup, "CI")
    assert "missing" in ci and head[:12] in ci and "PASS" not in ci, ci
    assert "#7" in footer_value(markup, "PR") and "https://github.com/o/shop/pull/7" in footer_value(markup, "PR")
    assert re.search(r"<h1>Apply a coupon once</h1>", markup)
    other = "1234567890abcdef1234567890abcdef12345678"
    _, markup = generate(repo, base, {"pr": {**pr, "headRefOid": other}, "checks": {other: green}})
    assert "PASS" in footer_value(markup, "CI"), footer_value(markup, "CI")
    assert any(head[:12] in item and other[:12] in item for item in decide_items(markup)), decide_items(markup)


def no_pr_and_gh_failing_are_named(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    assert "no PR yet" in footer_value(markup, "CI"), footer_value(markup, "CI")
    _, markup = generate(repo, base, {"error": True})
    ci = footer_value(markup, "CI")
    assert "unknown" in ci and "error connecting to api.github.com" in ci, ci
    assert any("unknown" in item for item in decide_items(markup))


def missing_or_malformed_reports_are_unavailable(base):
    repo, _ = full_branch(base)
    os.remove(path_of(repo, "validation"))
    write(repo, "review", "some notes, nothing a review writes\n")
    _, markup = generate(repo, base)
    tests, review = summary_row(markup, "tests"), summary_row(markup, "review")
    assert "unavailable" in tests and "unavailable" in review, (tests, review)
    assert not re.search(r"\b(PASS|FAIL|NOT RUN|Fixed|Open) 0\b", tests + review), (tests, review)
    items = "\n".join(decide_items(markup))
    assert "validation" in items.lower() and "unavailable" in items, items
    for verdict in ("   ", " ."):
        write(repo, "verify", f"# Verify:{verdict}\n")
        _, markup = generate(repo, base)
        assert "verify unavailable" in summary_row(markup, "gates"), f"verdict {verdict!r}"


def ci_states_and_phases_off(base):
    repo, _ = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Apply a coupon once", "headRefOid": head,
          "state": "OPEN"}
    run = lambda **fields: {"check_runs": [{"name": "suite", "details_url": "", **fields}]}
    for checks, word in ((run(status="completed", conclusion="failure"), "FAIL"),
                         (run(status="in_progress", conclusion=None), "pending")):
        _, markup = generate(repo, base, {"pr": pr, "checks": {head: checks}})
        assert footer_value(markup, "CI").startswith(word), footer_value(markup, "CI")
        assert any("CI for the PR head" in item and word in item for item in decide_items(markup)), decide_items(markup)
    _, markup = generate(repo, base, {"pr": pr, "api_error": True})
    ci = footer_value(markup, "CI")
    assert "unknown" in ci and "bad gateway" in ci, ci
    for phase in ("ci", "verify", "review"):
        out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", phase)
        assert out.returncode == 0, out
    _, markup = generate(repo, base, {"pr": pr, "checks": {head: run(status="completed", conclusion="failure")}})
    assert footer_value(markup, "CI").startswith("CI phase off"), footer_value(markup, "CI")
    items = "\n".join(decide_items(markup))
    assert "CI for the PR head" not in items and "Verify" not in items and "Open finding" not in items, items
    assert "review stamp phase off" in text(markup) and "verify phase off" in text(markup)
    assert re.search(r"\bci\b", footer_value(markup, "Phases off")), footer_value(markup, "Phases off")


def stamps_verify_and_validation_states(base):
    repo, evidence = full_branch(base)
    stamp = os.path.join(KIT, "hooks", "review_stamp.py")
    assert sh(repo, "python3", stamp, "write").returncode == 0
    _, markup = generate(repo, base)
    assert "review stamp current" in text(markup), text(markup)[:600]
    assert not any("review stamp" in item for item in decide_items(markup))
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nc = 3\n")
    write(repo, "verify", "# Verify: PASS, but nothing was checked\n\n2026-10-26 · verify in shop\n")
    _, markup = generate(repo, base)
    assert "review stamp stale" in text(markup), text(markup)[:600]
    items = "\n".join(decide_items(markup))
    assert "The review stamp: stale" in items and "checked nothing" in items, items
    assert "verify checked nothing" in text(markup), text(markup)[:600]
    git(repo, "checkout", "main", "--", "src/cart.py")
    git(repo, "commit", "-qam", "docs only")
    os.remove(path_of(repo, "validation"))
    _, markup = generate(repo, base)
    assert "validate stamp not needed" in text(markup), text(markup)[:600]
    items = "\n".join(decide_items(markup))
    assert "Validation report" not in items and "validate stamp" not in items, items


def prose_names_no_file_it_lacks(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## E. Prose\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 "| E1 | AC1 | prose evidence | + | a 200 | PASS | took ~3s, README.md updated, GET /api/cart 200; see A1.txt |\n")
    _, markup = generate(repo, base)
    block = text(check_block(markup, "E1"))
    assert "missing" not in block and "not read" not in block, block
    assert "A1.txt" in block and ("ok 12 rows" in block or "shown above" in block), "a file the prose names and the evidence directory has wasn't shown"
    assert not any(item.startswith("E1") for item in decide_items(markup)), decide_items(markup)


def a_template_heading_is_not_the_first_paragraph(base):
    repo, _ = full_branch(base, notes=False)
    write(repo, "pr-body", "## Summary\n<!-- what it does -->\n\nThe cart applies a coupon once.\n\nSecond paragraph.\n")
    _, markup = generate(repo, base)
    solution = summary_row(markup, "solution")
    assert solution.startswith("Solution The cart applies a coupon once."), solution
    write(repo, "pr-body", "## Summary\n\n#123 is fixed here.\nMore.\n")
    _, markup = generate(repo, base)
    assert summary_row(markup, "solution").startswith("Solution #123 is fixed here."), "an issue reference was taken for a heading"


def the_validation_notes_keep_their_code(base):
    repo, _ = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("- Rerun it:\n\n```\n# KEEP-THIS-COMMENT\nmake test\n```\n")
    _, markup = generate(repo, base)
    assert "KEEP-THIS-COMMENT" in markup, "a shell comment in the validation notes was dropped"


# AC4

def each_check_row_carries_its_evidence(base):
    repo, evidence = full_branch(base)
    _, markup = generate(repo, base)
    block = check_block(markup, "A1")
    words = text(block)
    for part in ("A1", "Coupon applied once", "AC1", "one discount line", "one line seen", "PASS", "make test-cart",
                 repo, "2026-10-26 10:00:00 +0000", "Exit 0", git(repo, "rev-parse", "HEAD")[:12], working_tree(repo)[:12],
                 "ok 12 rows", "make test-cart # kept script"):
        assert part in words, (part, words[:1200])
    assert re.search(r'<img [^>]*src="data:image/png;base64,', block), "the screenshot isn't embedded"
    real = urllib.parse.quote(os.path.realpath(evidence))
    assert f'href="file://{real}/run.webm"' in block, "the video isn't linked"
    assert "php 8.3" in text(section(markup, 2)), "environment.txt isn't shown"
    assert "missing" in text(check_block(markup, "A4")), text(check_block(markup, "A4"))
    assert "missing" not in text(block)


def evidence_named_in_code_spans_and_old_headers(base):
    repo, evidence = full_branch(base)
    put(os.path.join(evidence, "D2.txt"), f"$ make old\n# in {repo}, 2026-10-01 10:00:00 +0000, exit 3\n\nold body\n")
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## D. Forms\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 "| D1 | AC1 | a code span | + | shown | PASS | ran `A1.txt` and **`gone2.txt`** |\n"
                 "| D2 | AC1 | an old header | + | shown | PASS | `D2.txt` |\n")
    _, markup = generate(repo, base)
    one = text(check_block(markup, "D1"))
    assert "ok 12 rows" in one or "shown above" in one, one[:800]
    assert "gone2.txt missing" in one, one[:800]
    two = text(check_block(markup, "D2"))
    assert "make old" in two and "Exit 3" in two and "$ make old" not in two, two[:800]


def a_report_from_before_pr_a_says_not_recorded(base):
    repo, evidence = full_branch(base)
    os.remove(os.path.join(evidence, "environment.txt"))
    write(repo, "validation", f"""# Validation

| # | AC | Check | Case | Result | Evidence |
|---|---|---|---|---|---|
| A1 | AC1 | Coupon applied once | + | PASS | [output]({evidence}/A1.txt) |
""")
    _, markup = generate(repo, base)
    block = text(check_block(markup, "A1"))
    assert "not recorded" in block and "missing" not in block, block
    two = text(section(markup, 2))
    assert re.search(r"Environment:? not recorded", two), two[:900]


# AC5

def every_review_entry_and_guide_step_in_order(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    four = section(markup, 3)
    for outcome, entry, where in (("Fixed", "a coupon applied twice on refresh", "src/cart.py:40"),
                                  ("Fixed", "the total ignored tax", "src/cart.py:52"),
                                  ("Rejected", "a race on checkout", "handled at src/cart.py:80"),
                                  ("Pre-existing", "the log grows without a bound", "src/log.py:9"),
                                  ("Open", "totals in two currencies collapse", "src/cart.py:77"),
                                  ("Not run", "security lens", "no auth code touched")):
        group = re.search(rf'<div class="outcome" id="review-{outcome.lower().replace(" ", "-")}">(.*?)</div>', four, re.S)
        assert group and entry in text(group.group(1)) and where in text(group.group(1)), (outcome, entry)
    assert "codex on correctness" in text(four) and "re-check rounds 1" in text(four).replace(":", ""), text(four)[:600]
    five = re.search(r'<details class="annex" id="annex-checks-guide">.*?</details>', markup, re.S).group(0)
    words = text(five)
    positions = [words.find(s) for s in ("Before the merge (optional)", "M1. Pay on a phone", "one discount line on the phone",
                                         "send the screenshot of the cart", "M2. Refund in the sandbox", "After the merge",
                                         "P1. Backfill the totals", "zero errors from the backfill", "roll the backfill back",
                                         "P2. Watch the error log", "page the cart owner", "Results (2026-10-26)",
                                         "the coupon doubled on Safari")]
    assert -1 not in positions and positions == sorted(positions), positions
    assert re.search(r"<pre[^>]*><code[^>]*>open &quot;shop.test/cart\?coupon=ONE&quot;", five), "commands aren't preformatted"
    assert "<button" not in markup


# AC6

def hostile_text_renders_inert(base):
    repo, evidence = full_branch(base)
    odd = os.path.join(evidence, 'a"b c#d.txt')
    put(odd, "<script>alert(1)</script>\n<img src=x onerror=alert(2)>\n")
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## D. Hostile\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| D1 | AC1 | <script>alert(3)</script> <img src=x onerror=alert(4)> | − | \" onmouseover=\"alert(5) | FAIL | "
                 f"[x](javascript:alert(6)) [y](http://evil.test/z) [odd]({odd}) |\n")
    write(repo, "pr-body", "Hi [x](javascript:alert(7)) and <script>alert(8)</script> ![i](http://evil.test/i.png)\n")
    write(repo, "decision-notes", NOTES.replace("A shopper sees", "<img src=x onerror=alert(9)> A shopper sees"))
    # What an attacker-controlled PR also reaches: its title, CI check names, file names, and text quoted in reports.
    evil = '"><script>alert(10)</script><img src=x onerror=alert(11)>'
    put(os.path.join(repo, "src", '<img src=x onerror=alert(12)>.py'), "x = 1\n")
    put(os.path.join(evidence, 'shot"onerror=alert(13).png'), PNG)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write(f"| D2 | AC1 | image | + | an image | PASS | [shot]({evidence}/shot\"onerror=alert(13).png) |\n")
    write(repo, "review", REVIEW.replace("decision for the human", evil))
    write(repo, "checks-guide", GUIDE.replace("### M2. Refund in the sandbox", f"### M2. Refund {evil}"))
    write(repo, "follow-pr", FOLLOW_PR.replace("asks about rounding", evil))
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": f"https://github.com/o/shop/pull/7{evil}", "title": evil, "headRefOid": head, "state": evil}
    checks = {head: {"check_runs": [{"name": evil, "status": "completed", "conclusion": "failure", "details_url": evil}]}}
    put(os.path.join(evidence, "attack.html"), "<script>alert(14)</script>\n")
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write(f"| D3 | AC1 | active file | + | not linked | PASS | proof in [open proof](attack.html) |\n")
    _, markup = generate(repo, base, {"pr": pr, "checks": checks})
    assert "attack.html" not in "".join(v or "" for _, attrs in tags(markup) for _, v in attrs), "a Markdown link opens an active file"
    assert "<script" not in markup.lower(), "a script tag reached the page"
    for tag, attrs in tags(markup):
        assert tag not in ("script", "iframe", "object", "embed", "base", "form"), tag
        for name, value in attrs:
            assert not name.startswith("on"), (tag, name, value)
            assert not (value or "").strip().lower().startswith(("javascript:", "http:", "data:text")), (tag, name, value)
            if (value or "").strip().lower().startswith("https:"):
                assert tag == "a" and name == "href" and "alert" not in value and "evil" not in value, (tag, name, value)
    assert "/commit/" not in markup and "/blob/" not in markup, "a PR URL that isn't GitHub's gave the page a repository"
    words = text(markup)
    assert "[x](javascript:alert(6))" in words and "[y](http://evil.test/z)" in words, "an unsafe link wasn't kept as text"
    assert "[x](javascript:alert(7))" in words
    real = urllib.parse.quote(os.path.realpath(evidence))
    assert f'href="file://{real}/a%22b%20c%23d.txt"' in markup, "the odd file name doesn't link right"
    assert "<script>alert(1)</script>" in words, "the output's text was dropped instead of escaped"


# AC7

def the_skills_regenerate_and_link_the_page(base):
    for skill, needles in (("kit-ship", ("reports html", "decision-notes", "Review and testing")),
                           ("kit-follow-pr", ("reports html", "Review and testing"))):
        with open(os.path.join(KIT, "skills", skill, "SKILL.md")) as fh:
            body = fh.read()
        for needle in needles:
            assert needle in body, (skill, needle)


def refuses_another_branch_and_a_detached_head(base):
    repo = new_repo(base)
    out = sh(repo, REPORTS, "--branch", "main", "html")
    assert out.returncode == 2 and "html" in out.stderr, out
    git(repo, "checkout", "-q", "--detach")
    out = sh(repo, REPORTS, "html")
    assert out.returncode == 2 and "branch" in out.stderr, out


# v2 AC1: the summary first

LENSES = """
## Lenses

| Lens | Model | Findings | Confirmed | Outcomes |
|---|---|---|---|---|
| Correctness | claude | 3 | 2 | fixed=2, wrong=1 |
| Correctness | codex | 2 | 1 | fixed=1, intentional=1 |
| Tests | claude | 1 | 1 | open=1 |
| Maintainability | codex | not run: exit 1 | — | — |
"""


def add_rows(repo, rows, header="| # | AC | Check | Case | Expected | Result | Evidence |"):
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write(f"\n## Z. More\n\n{header}\n|{'---|' * (header.count('|') - 1)}\n{rows}")


def the_summary_comes_first_in_order(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    main = markup[markup.index('<main id="main">'):]
    assert main.index('<section id="summary"') < main.index('<details class="sec"'), "AC1: a section comes before the summary"
    labels = re.findall(r'<div class="row"><dt id="sum-(\w+)">([^<]+)</dt>', summary(markup))
    assert [l for _, l in labels] == ["Task", "Solution", "Review", "Tests", "Gates", "Signals"], labels
    lists = re.findall(r'<ol class="items" id="(\w+)-items">', summary(markup))
    assert lists == ["decisions", "blockers", "warnings"], lists
    task = summary_row(markup, "task")
    assert task.startswith("Task The task must stop a second discount for one coupon."), task
    solution = summary_row(markup, "solution")
    assert "The change adds a guard in src/cart.py:2" in solution, solution
    folded = re.search(r'id="sum-solution".*?<details[^>]*>(.*?)</details>', summary(markup), re.S)
    assert folded and "Where it sits" in text(folded.group(1)) and "A shopper sees one discount line" in text(folded.group(1)), \
        "AC1: the rest of the notes isn't folded under the solution"
    assert "Written by the agent" in text(summary(markup))
    nav = re.findall(r'<a href="#s0(\d)">', summary(markup))
    assert nav == ["1", "2", "3", "4", "5"], nav
    sections = re.findall(r'<details class="sec"[^>]* id="s0(\d)"><summary><h2[^>]*><span class="num">0\d</span> ([^<]+)</h2>', markup)
    assert [h for _, h in sections] == ["Coverage", "Tests", "Review", "Merge and ship", "Reports"], sections


def the_summary_falls_back_to_the_spec_and_pr_body(base):
    repo, _ = full_branch(base, notes=False)
    _, markup = generate(repo, base)
    task = summary_row(markup, "task")
    assert "a shopper gets one discount per coupon, even on refresh." in task, task
    solution = summary_row(markup, "solution")
    assert "The cart applies a coupon once." in solution and "Second paragraph" not in solution, solution
    assert "Written by the agent" not in text(summary(markup))


def the_three_lists_hold_their_items(base):
    repo, _ = full_branch(base)
    assert sh(repo, "python3", os.path.join(KIT, "hooks", "review_stamp.py"), "write").returncode == 0
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nc = 3\n")  # the review stamp goes stale
    _, markup = generate(repo, base)
    decisions, blockers, warnings = (listed(markup, name) for name in ("decisions", "blockers", "warnings"))
    assert decisions and decisions[0].startswith("Merge"), decisions
    assert "recommendation not recorded" in decisions[0], decisions[0]
    assert any("Decide if one FAIL on the phone check blocks the merge." in d for d in decisions), decisions
    assert any("totals in two currencies collapse" in d for d in decisions), decisions
    assert not any("M1" in d or "M2" in d for d in decisions), "AC1: a guide step with a result is left as a decision"
    assert any("A2" in b and "FAIL" in b for b in blockers), blockers
    assert any("review stamp" in b and "stale" in b for b in blockers), blockers
    assert any("gone.txt" in b and "missing" in b for b in blockers), blockers
    assert any("M1" in b and "FAIL" in b for b in blockers), blockers
    assert any("environment.txt" in w for w in warnings), warnings
    assert not any("environment.txt" in b for b in blockers), blockers
    assert not any(re.search(r"\bA3\b", item) for item in decisions + blockers + warnings), "AC1: a NOT RUN check is listed"
    counts = re.findall(r'<h2>([^<]+) <span class="n">(\d+)</span></h2>', summary(markup))
    assert counts == [("Your decisions", str(len(decisions))), ("Blockers the agent owns", str(len(blockers))),
                      ("Warnings", str(len(warnings)))], counts
    assert "Gates CI no PR yet · review stamp stale · validate stamp missing" in summary_row(markup, "gates"), summary_row(markup, "gates")
    write(repo, "decision-notes", NOTES.replace("Decide if one FAIL on the phone check blocks the merge.",
                                                "- Merge: recommend yes once A2 is fixed; waiting costs a day.\n"
                                                "- Keep the phone check: A to keep, B to drop; recommend A."))
    _, markup = generate(repo, base)
    decisions = listed(markup, "decisions")
    assert decisions[0].startswith("Merge") and "recommend yes once A2 is fixed" in decisions[0], decisions
    assert sum("recommend yes once A2" in d for d in decisions) == 1, decisions
    assert any("Keep the phone check" in d for d in decisions), decisions
    write(repo, "review", REVIEW + "Open:      a second open point — src/cart.py:80 — decision for the human\n"
                                   "Open:      a third open point — src/cart.py:90 — decision for the human\n")
    write(repo, "checks-guide", GUIDE.replace("## Before the merge (optional)\n",
                                              "## Before the merge (optional)\n\n### M0. What you need\n- **Where:** you\n"))
    _, markup = generate(repo, base)
    decisions = listed(markup, "decisions")
    assert sum("left Open" in d for d in decisions) == 1 and "3 review findings left Open" in "".join(decisions), decisions
    assert not any("Open finding" in d or re.search(r"\bM0\b", d) for d in decisions), decisions
    assert any("Keep the phone check" in d for d in decisions), decisions


def sections_fold_unless_they_hold_a_failure(base):
    repo, evidence = full_branch(base)
    _, markup = generate(repo, base)
    assert is_open(markup, 2), "AC1: Tests holds a FAIL check and is closed"
    assert is_open(markup, 1), "AC1: Coverage holds a criterion with no check and is closed"
    assert not is_open(markup, 3), "AC1: Review holds no blocker and is open"
    write(repo, "validation", f"""# Validation

| # | AC | Check | Case | Expected | Result | Evidence |
|---|---|---|---|---|---|---|
| A1 | AC1, AC2, AC3 | Coupon applied once | + | one line | PASS | [output]({evidence}/A1.txt) |
""")
    write(repo, "checks-guide", GUIDE.replace("| M1 | FAIL | the coupon doubled on Safari |", "| M1 | PASS | one line |"))
    _, markup = generate(repo, base)
    assert not is_open(markup, 2) and not is_open(markup, 1) and not is_open(markup, 3) and not is_open(markup, 5), \
        [is_open(markup, n) for n in (1, 2, 3, 5)]
    write(repo, "checks-guide", GUIDE)
    _, markup = generate(repo, base)
    assert is_open(markup, 5), "AC1: a FAIL step in the checks guide leaves the Reports section closed"
    write(repo, "checks-guide", GUIDE.replace("| M1 | FAIL | the coupon doubled on Safari |", "| M1 | PASS | one line |"))
    write(repo, "validation", f"""# Validation

| # | AC | Check | Case | Expected | Result | Evidence |
|---|---|---|---|---|---|---|
| A1 | AC1, AC2, AC3 | Coupon applied once | + | one line | FAIL | [output]({evidence}/A1.txt) |
""")
    _, markup = generate(repo, base)
    assert is_open(markup, 2), "AC1: a lone FAIL check leaves Tests closed"
    assert [b for b in listed(markup, "blockers") if "A1" in b], listed(markup, "blockers")
    os.remove(path_of(repo, "review"))
    _, markup = generate(repo, base)
    assert is_open(markup, 3), "AC1: Review is unavailable and its section is closed"
    assert any("Review report" in b and "unavailable" in b for b in listed(markup, "blockers")), listed(markup, "blockers")
    line = summary_row(markup, "review")
    assert ("unavailable" in line or "not recorded" in line) and not re.search(r"Fixed 0|Open 0", line), line


def the_review_line_names_a_skipped_reviewer(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + LENSES)
    _, markup = generate(repo, base)
    line = summary_row(markup, "review")
    for part in ("2 lenses", "codex on correctness", "Fixed 2", "Open 1", "not run:", "codex Maintainability", "exit 1",
                 "security lens"):
        assert part in line, (part, line)


def the_tests_line_counts_each_level(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", report.replace("| unit | yes | [A1]({evidence}/A1.txt) |".format(evidence=evidence),
                                             f"| unit | yes | [A1]({evidence}/A1.txt) |\n| a11y | no | no screen reader here |"))
    _, markup = generate(repo, base)
    line = summary_row(markup, "tests")
    assert "unit 1/1" in line, line
    assert "level not recorded 1/3" in line and "1 FAIL" in line and "1 NOT RUN" in line, line
    assert "a11y not run: no screen reader here" in line, line
    os.remove(path_of(repo, "validation"))
    _, markup = generate(repo, base)
    line = summary_row(markup, "tests")
    assert ("unavailable" in line or "not recorded" in line) and not re.search(r"\b0/0\b|\b0 FAIL", line), line


# v2 AC2: levels and coverage

def levels_come_from_the_level_column(base):
    repo, evidence = full_branch(base)
    write(repo, "validation", f"""# Validation

| # | AC | Level | Check | Case | Expected | Result | Evidence |
|---|---|---|---|---|---|---|---|
| A1 | AC1 | Unit | Coupon applied once | + | one line | PASS | [output]({evidence}/A1.txt) |
| A2 | AC1, AC2 | e2e | Tampered total refused | − | exit 1 | FAIL | [output]({evidence}/A2.txt) |

## Test types

| Type | Ran | Evidence |
|---|---|---|
| mutation | yes | A1 and A2 |
""")
    _, markup = generate(repo, base)
    one = section(markup, 1)
    head = re.search(r"<thead>(.*?)</thead>", one, re.S)
    assert head and text(head.group(1)) == "AC Criterion e2e unit", head and text(head.group(1))
    rows = {n: body for n, body in re.findall(r'<tr[^>]*id="ac-(\d+)">(.*?)</tr>', one, re.S)}
    cells = lambda n: [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", rows[n], re.S)]
    assert cells("1")[1:] == ["A2 FAIL", "A1 PASS"], cells("1")
    assert cells("2")[1:] == ["A2 FAIL", "—"], cells("2")
    assert 'href="#check-A1"' in rows["1"] and 'href="#check-A2"' in rows["1"], rows["1"]
    assert "no check" in text(rows["3"]), rows["3"]
    assert any("AC3" in b and "no check" in b for b in listed(markup, "blockers")), listed(markup, "blockers")
    two = section(markup, 2)
    groups = re.findall(r'<h3 id="level-([^"]+)">', two)
    assert groups == ["e2e", "unit"], groups
    assert two.index('id="level-e2e"') < two.index('id="check-A2"') < two.index('id="level-unit"') < two.index('id="check-A1"')


def levels_fall_back_to_test_types(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    two = section(markup, 2)
    groups = re.findall(r'<h3 id="level-([^"]+)">', two)
    assert groups == ["unit", "not-recorded"], groups
    assert two.index('id="check-A1"') < two.index('id="level-not-recorded"') < two.index('id="check-A2"'), "A1 isn't unit"
    head = text(re.search(r"<thead>(.*?)</thead>", section(markup, 1), re.S).group(1))
    assert head == "AC Criterion unit level not recorded", head
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", re.sub(r"## Test types.*?(?=## Notes)", "", report, flags=re.S))
    _, markup = generate(repo, base)
    assert re.findall(r'<h3 id="level-([^"]+)">', section(markup, 2)) == ["not-recorded"]


# v2 AC3: each check's steps, expected, actual and evidence

def a_check_opens_to_steps_expected_actual_evidence(base):
    repo, evidence = full_branch(base)
    output(evidence, "S1.txt", "bash checks/S1.sh", "precondition: an empty cart\nstep: apply coupon ONE\nPASS one discount line\n"
           "noise\nstep: refresh the cart\nFAIL two discount lines\n", repo, working_tree(repo))
    add_rows(repo, f"| S1 | AC1 | refresh keeps one discount | + | one discount line after refresh | FAIL | two lines: [output]({evidence}/S1.txt) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "S1")
    terms = [text(t) for t in re.findall(r"<dt>(.*?)</dt>", block)]
    assert terms[:4] == ["Steps", "Expected", "Actual", "Evidence"], terms
    steps = re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", block, re.S).group(1)
    assert [text(s) for s in re.findall(r"<li>(.*?)</li>", steps)] == ["precondition: an empty cart", "step: apply coupon ONE",
                                                                         "step: refresh the cart"], steps
    actual = text(re.search(r"<dt>Actual</dt><dd>(.*?)</dd>", block, re.S).group(1))
    assert actual.startswith("two lines") and actual.index("PASS one discount line") < actual.index("FAIL two discount lines"), actual
    assert "noise" not in actual
    assert "one discount line after refresh" in text(re.search(r"<dt>Expected</dt><dd>(.*?)</dd>", block, re.S).group(1))
    assert "not recorded" in text(re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", check_block(markup, "A1"), re.S).group(1))


def a_long_log_shows_its_tail(base):
    repo, evidence = full_branch(base)
    output(evidence, "L1.txt", "make long", "".join(f"line {n:04d}\n" for n in range(1, 301)), repo, working_tree(repo))
    output(evidence, "L2.txt", "make huge", "HEAD LINE\n" + "".join(f"row {n:06d} {'x' * 40}\n" for n in range(7000))
           + "TAIL LINE\n", repo, working_tree(repo))
    add_rows(repo, f"| L1 | AC1 | 300 lines | + | its tail | PASS | [output]({evidence}/L1.txt) |\n"
                   f"| L2 | AC1 | 300 KB | + | its tail | PASS | [output]({evidence}/L2.txt) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "L1")
    tail = re.search(r'<pre class="out tail">(.*?)</pre>', block, re.S)
    assert tail, "AC3: no tail block"
    lines = tail.group(1).strip("\n").split("\n")
    assert lines[0] == "line 0261" and lines[-1] == "line 0300" and len(lines) == 40, (lines[:2], lines[-1], len(lines))
    earlier = re.search(r'<details class="earlier">(.*?)</details>', block, re.S)
    assert earlier and "line 0001" in earlier.group(1) and "line 0260" in earlier.group(1) and "line 0261" not in earlier.group(1)
    assert block.index('class="out tail"') < block.index('class="earlier"'), "AC3: the rest isn't folded under the tail"
    big = check_block(markup, "L2")
    tail = re.search(r'<pre class="out tail">(.*?)</pre>', big, re.S).group(1)
    assert "TAIL LINE" in tail and "HEAD LINE" not in tail, "AC3: the 300 KB log's tail isn't shown"
    earlier = re.search(r'<details class="earlier">(.*?)</details>', big, re.S).group(1)
    assert "HEAD LINE" in earlier and 'class="cut"' in earlier, "AC3: the cut isn't marked inside the fold"
    assert "row 003500" not in big and len(big) < 250_000


def a_video_plays_inline(base):
    repo, evidence = full_branch(base)
    put(os.path.join(evidence, 'v"<b>.mp4'), b"\0\0\0 ftypisom not really")
    put(os.path.join(base, "outside.webm"), b"\x1aE\xdf\xa3")
    add_rows(repo, f'| V1 | AC1 | a hostile name | + | a video | PASS | [v]({evidence}/v"<b>.mp4) |\n'
                   f"| V2 | AC1 | a video outside | + | not read | PASS | [v]({base}/outside.webm) |\n")
    _, markup = generate(repo, base)
    real = urllib.parse.quote(os.path.realpath(evidence))
    block = check_block(markup, "A1")
    video = re.search(r"<video controls[^>]*>(.*?)</video>", block, re.S)
    assert video, "AC3: the .webm isn't a video"
    assert f'<source src="file://{real}/run.webm" type="video/webm">' in video.group(1), video.group(1)
    assert f'href="file://{real}/run.webm"' in video.group(1), "AC3: no link inside the video as its fallback"
    assert '<video controls preload="metadata" aria-label="run.webm">' in block, "the video has no accessible name"
    hostile = check_block(markup, "V1")
    assert f'src="file://{real}/v%22%3Cb%3E.mp4" type="video/mp4"' in hostile, hostile[:800]
    assert "<b>" not in hostile, hostile[:800]
    assert 'aria-label="v&quot;&lt;b&gt;.mp4"' in hostile, hostile[:800]
    outside = check_block(markup, "V2")
    assert "<video" not in outside and "not read" in text(outside), outside[:800]


def a_suite_output_lists_its_tests(base):
    repo, evidence = full_branch(base)
    put(path_of(repo, "spec"), SPEC.replace("2. A tampered total is refused. — verify: test.",
                                            "2. A tampered total is refused. — verify: `refund_issued` and `tampered_total`."))
    output(evidence, "U1.txt", "tests/run.sh", "== cart\nok   coupon_once\nFAIL tampered_total\n     AssertionError: 2 != 1\n"
           "== refunds\nok   refund_issued\nFAILURES ABOVE\n", repo, working_tree(repo), exit_code=1)
    add_rows(repo, f"| U1 | AC1 | the suite | + | all ok | FAIL | [output]({evidence}/U1.txt) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "U1")
    runner = re.search(r'<details class="runner">(.*?)</details>', block, re.S)
    assert runner, "AC3: no test-runner list"
    summary_line = text(re.search(r"<summary>(.*?)</summary>", runner.group(1), re.S).group(1))
    assert "3 tests" in summary_line and "1 FAIL" in summary_line and "AC1" in summary_line, summary_line
    def rows_of(markup):
        return [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
                for r in re.findall(r"<tr>(.*?)</tr>", markup, re.S) if "<td" in r]
    every = re.search(r'<details class="earlier"><summary>All 3 tests, by section</summary>(.*)', runner.group(1), re.S)
    assert every, "AC9: the full list isn't folded under its count"
    head = runner.group(1)[:runner.group(1).index('<details class="earlier">')]
    assert rows_of(head.split("<h4>")[0]) == [["cart", "2", "1"], ["refunds", "1", "0"]], rows_of(head.split("<h4>")[0])
    parts = dict(re.findall(r"<h4>(.*?)</h4>(.*?)(?=<h4>|$)", head, re.S))
    assert list(parts) == ["Failures", "Named in the spec"], list(parts)
    assert rows_of(parts["Failures"]) == [["tampered_total", "FAIL", "AC2"]], rows_of(parts["Failures"])
    assert [r[0] for r in rows_of(parts["Named in the spec"])] == ["tampered_total", "refund_issued"], parts["Named in the spec"]
    groups = re.findall(r"<h4>(.*?)</h4>", every.group(1))
    assert groups == ["cart", "refunds"], groups
    assert rows_of(every.group(1)) == [["coupon_once", "ok", ""], ["tampered_total", "FAIL", "AC2"], ["refund_issued", "ok", "AC2"]]
    assert "<details class=\"runner\">" not in check_block(markup, "A1"), "AC3: `ok 12 rows` outside a section was read as a test"
    assert "(3 tests in the runner output, 1 FAIL)" in summary_row(markup, "tests"), summary_row(markup, "tests")


# v2 AC4: the review per lens

def the_review_shows_each_lens(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + LENSES)
    _, markup = generate(repo, base)
    three = section(markup, 3)
    table = re.search(r'aria-label="Table: Lens, What it checks, Model, Findings, Confirmed, Outcomes"><table>(.*?)</table>', three, re.S)
    assert table, "AC4: no lens table"
    head = [text(c) for c in re.findall(r"<th[^>]*>(.*?)</th>", re.search(r"<thead>(.*?)</thead>", table.group(1), re.S).group(1))]
    assert head == ["Lens", "What it checks", "Model", "Findings", "Confirmed", "Outcomes"], head
    rows = [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in re.findall(r"<tr>(.*?)</tr>", re.search(r"<tbody>(.*?)</tbody>", table.group(1), re.S).group(1), re.S)]
    assert rows[0] == ["Correctness", "You are reviewing for behavior bugs only. Ignore style.", "claude", "3", "2",
                       "fixed=2, wrong=1"], rows[0]
    assert rows[1][2:] == ["codex", "2", "1", "fixed=1, intentional=1"], rows[1]
    assert rows[2][0] == "Tests" and rows[2][1].startswith("You are reviewing the tests in the diff"), rows[2]
    assert rows[3][0] == "Maintainability" and rows[3][2] == "codex", rows[3]
    assert rows[3][3] == "not run: exit 1" and rows[3][4] == "not recorded" and rows[3][5] == "not recorded", rows[3]
    assert "per-lens counts not recorded" not in text(three)
    open_entry = re.search(r'<li class="entry" id="review-open-1"><details>(.*?)</details></li>', three, re.S)
    assert open_entry and "src/cart.py:77" in text(open_entry.group(1)), "AC4: a finding isn't folded with its parts"


def a_review_without_lenses_says_not_recorded(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    three = section(markup, 3)
    assert "per-lens counts not recorded" in text(three), text(three)[:600]
    rows = [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in re.findall(r"<tr>(.*?)</tr>", re.search(r'aria-label="Table: Lens, What it checks[^"]*"><table>.*?<tbody>(.*?)</tbody>', three, re.S).group(1), re.S)]
    assert [r[0] for r in rows] == ["correctness", "tests"], rows
    assert rows[0][1] == "You are reviewing for behavior bugs only. Ignore style.", rows[0]
    assert all(cell == "not recorded" for row in rows for cell in row[2:]), rows
    write(repo, "review", "# " + REVIEW)
    _, markup = generate(repo, base)
    line = summary_row(markup, "review")
    assert "2 lenses: correctness, tests" in line and "cross-model: codex on correctness" in line, line


# v2 AC5: merge and ship, the reports as annexes

SHIP = """# Ship

## Deploy

1. `make deploy` on the build host.

## Smoke checks

- Open the cart and apply coupon ONE.

## Rollback

`git revert` the merge, then `make deploy`.

## Installed on this machine

no
"""


def ship_path(repo):
    return os.path.expanduser(f"~/.agents/repos/{os.path.basename(repo)}/ship.md")


def merge_and_ship_reads_ship_md(base):
    repo, _ = full_branch(base)
    put(ship_path(repo), SHIP)
    head = git(repo, "rev-parse", "HEAD")
    merged = "fe" * 20
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Apply a coupon once", "headRefOid": head,
          "state": "MERGED", "mergeCommit": {"oid": merged}}
    _, markup = generate(repo, base, {"pr": pr})
    four = section(markup, 4)
    words = text(four)
    order = [words.find(s) for s in ("Your decisions", "After the merge", "P1", "P2", "Deploy", "make deploy", "Smoke checks",
                                     "apply coupon ONE", "Rollback", "git revert", "Installed on this machine")]
    assert -1 not in order and order == sorted(order), (order, words[:1500])
    owners = re.findall(r'<span class="owner">([^<]+)</span>', four)
    assert owners and set(owners) <= {"you", "agent"} and "agent" in owners, owners
    rollback = re.search(r'id="ship-rollback".*?(?=id="ship-installed")', four, re.S)
    assert rollback and "#7" in text(rollback.group(0)) and merged in text(rollback.group(0)), "AC5: no PR and merge commit by the rollback"
    assert f'href="https://github.com/o/shop/commit/{merged}"' in rollback.group(0), "AC7: the merge commit isn't linked"
    assert "unconfirmed" not in words and "not recorded" not in words.split("Deploy", 1)[1], words
    assert ship_path(repo) in words


def a_partial_or_missing_ship_md(base):
    repo, _ = full_branch(base)
    put(ship_path(repo), "## Deploy\n\n1. `npm run deploy` (inferred: package.json)\n2. Tag it.\n\n## Smoke checks\n\nunknown\n")
    _, markup = generate(repo, base)
    four = section(markup, 4)
    deploy = text(re.search(r'id="ship-deploy".*?(?=id="ship-smoke)', four, re.S).group(0))
    assert re.search(r"unconfirmed\s*\(inferred: package.json\)", deploy) and "Tag it." in deploy, deploy
    assert deploy.count("unconfirmed") == 1, deploy
    smoke = text(re.search(r'id="ship-smoke.*?(?=id="ship-rollback")', four, re.S).group(0))
    assert "unconfirmed" in smoke, smoke
    for name in ("rollback", "installed"):
        part = text(re.search(rf'id="ship-{name}".*?(?=id="ship-|</details>|$)', four, re.S).group(0))
        assert "not recorded" in part, (name, part)
    assert "no PR yet" in text(re.search(r'id="ship-rollback".*?(?=id="ship-installed")', four, re.S).group(0))
    os.remove(ship_path(repo))
    _, markup = generate(repo, base)
    four = text(section(markup, 4))
    assert "not recorded" in four and ship_path(repo) in four, four[:900]
    put(ship_path(repo), "## Deploy\n" + "x" * (2 * 1024 * 1024 + 10))
    _, markup = generate(repo, base)
    four = text(section(markup, 4))
    assert "unavailable" in four and "over the 2 MB" in four and "xxxx" not in four, four[:900]


def reports_are_annexes_without_built_from(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    assert "Built from" not in text(markup), "AC5: a Built from line is left"
    five = section(markup, 5)
    for kind in ("spec", "decision-notes", "pr-body", "verify", "review", "validation", "checks-guide", "follow-pr"):
        annex = re.search(rf'<details class="annex" id="annex-{kind}"><summary>(.*?)</summary>', five, re.S)
        assert annex and text(annex.group(1)).endswith(path_of(repo, kind)), (kind, annex and text(annex.group(1)))
        assert text(five).count(path_of(repo, kind)) == 1, (kind, text(five).count(path_of(repo, kind)))
    assert "c123" in text(five) and "Only unit-tested: the refund path." in text(five) and "Second paragraph with detail." in text(five)
    os.remove(path_of(repo, "follow-pr"))
    _, markup = generate(repo, base)
    annex = re.search(r'<details class="annex" id="annex-follow-pr">.*?</details>', section(markup, 5), re.S).group(0)
    assert "not recorded" in text(annex) and text(annex).count(path_of(repo, "follow-pr")) == 1, text(annex)


# v2 AC6: the skills ask for what the page reads

def the_skills_ask_for_the_new_formats(base):
    def read(*parts):
        with open(os.path.join(KIT, *parts)) as fh:
            return fh.read()
    validate = read("skills", "kit-validate", "SKILL.md")
    assert "`step:`" in validate and "| Level |" in validate, "kit-validate asks for no step: line or Level column"
    for level in ("unit", "integration", "e2e", "mutation", "property", "regex", "design", "a11y", "query budget", "migration", "manual"):
        assert level in validate, level
    review = read("skills", "kit-review", "SKILL.md")
    assert "## Lenses" in review and "| Lens | Model | Findings | Confirmed | Outcomes |" in review
    ship = read("skills", "kit-ship", "SKILL.md")
    assert "first paragraph" in ship and "recommendation" in ship
    assert "folded" in read("docs", "framework.md").split("**`bin/decision-report`**", 1)[1][:2000]


# v2 review fixes

def runner_lines_take_linear_time(base):
    repo, evidence = full_branch(base)
    output(evidence, "R1.txt", "tests/run.sh", "== cart\n" + ("ok a" + " " * 30_000 + "b\n") * 4, repo, working_tree(repo))
    add_rows(repo, f"| R1 | AC1 | spaces | + | ok | PASS | [output]({evidence}/R1.txt) |\n")
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for runner lines of 30,000 spaces"


def hostile_text_in_the_new_parts_renders_inert(base):
    repo, evidence = full_branch(base)
    evil = '"><script>alert(20)</script><img src=x onerror=alert(21)>'
    output(evidence, "H1.txt", "make h", f"step: {evil}\n== {evil}\nok   {evil}\nFAIL {evil}\n", repo, working_tree(repo))
    add_rows(repo, f"| H1 | AC1 | {evil} | hostile | + | none | PASS | [output]({evidence}/H1.txt) |\n",
             header="| # | AC | Check | Level | Case | Expected | Result | Evidence |")
    write(repo, "review", REVIEW + LENSES + f"| {evil} | {evil} | {evil} | 1 | {evil} |\n")
    put(ship_path(repo), f"## Deploy\n\n1. {evil} (inferred: {evil})\n\n| a (inferred: x) | b |\n|---|---|\n| {evil} | c |\n")
    _, markup = generate(repo, base)
    assert "<script" not in markup.lower(), "a script tag reached the page"
    for tag, attrs in tags(markup):
        assert tag not in ("script", "img") or tag == "img" and dict(attrs).get("src", "").startswith("data:image/"), (tag, attrs)
        for name, value in attrs:
            assert not name.startswith("on"), (tag, name, value)
    words = text(markup)
    assert words.count("<script>alert(20)</script>") >= 5, words.count("<script>alert(20)</script>")


def a_ship_md_marker_in_a_table_header_keeps_its_tags(base):
    repo, _ = full_branch(base)
    put(ship_path(repo), "## Deploy\n\n| Step (inferred: Makefile) | Who |\n|---|---|\n| make deploy | you |\n\n"
                         "```\n## Build\nmake build\n```\n\n2. Run `(inferred: package.json)` once.\n\n"
                         "## Smoke checks\n\n`unknown`\n")
    _, markup = generate(repo, base)
    four = section(markup, 4)
    deploy = re.search(r'id="ship-deploy".*?(?=id="ship-smoke)', four, re.S).group(0)
    for _, attrs in tags(deploy):
        for _, value in attrs:
            assert "<" not in (value or ""), value
    assert text(deploy).count("unconfirmed") == 2, text(deploy)
    assert "make build" in text(deploy), "a fenced ## Build ended the Deploy section"
    smoke = text(re.search(r'id="ship-smoke.*?(?=id="ship-rollback")', four, re.S).group(0))
    assert "unconfirmed" in smoke, smoke


def unrun_optional_steps_are_decisions(base):
    repo, _ = full_branch(base)
    write(repo, "checks-guide", GUIDE.replace("## After the merge", "### M0. Setup\n- **Where:** you\n\n### M3. Try it in Safari\n"
                                              "- **Where:** you, on a Mac\n\n### S0. Staging access\n- **Where:** you\n\n"
                                              "### S1. Load staging\n- **Where:** you, on staging\n\n## After the merge"))
    _, markup = generate(repo, base)
    items = " | ".join(listed(markup, "decisions"))
    assert "M3 Try it in Safari" in items and "S1 Load staging" in items, items
    assert "M0" not in items and "S0" not in items, items


def a_failed_step_after_the_merge_is_a_blocker(base):
    repo, _ = full_branch(base)
    write(repo, "checks-guide", GUIDE.replace("| M2 | PASS | refund seen |", "| M2 | PASS | refund seen |\n| P1 | FAIL | 3 errors |"))
    _, markup = generate(repo, base)
    assert any("P1" in b and "FAIL" in b for b in listed(markup, "blockers")), listed(markup, "blockers")
    assert is_open(markup, 5), "a FAIL step of the guide leaves its section closed"


def test_types_heading_and_ran_in_any_form(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", report.replace("## Test types", "## Test Types").replace(
        f"| unit | yes | [A1]({evidence}/A1.txt) |", f"| unit | yes | [A1]({evidence}/A1.txt) |\n| a11y | `no` | no screen reader |"))
    _, markup = generate(repo, base)
    line = summary_row(markup, "tests")
    assert "unit 1/1" in line and "a11y not run: no screen reader" in line, line


def levels_map_aliases_and_stay_bounded(base):
    repo, evidence = full_branch(base)
    rows = "".join(f"| X{n} | AC1 | lvl{n} | many | + | ok | PASS | none |\n" for n in range(500))
    add_rows(repo, "| Y1 | AC1 | Accessibility | axe | + | ok | PASS | none |\n| Y2 | AC1 | visual | shots | + | ok | PASS | none |\n"
                   "| Y3 | AC1 | **Query budget** | queries | + | ok | PASS | none |\n" + rows,
             header="| # | AC | Level | Check | Case | Expected | Result | Evidence |")
    _, markup = generate(repo, base)
    groups = re.findall(r'<h3 id="level-([^"]+)">', section(markup, 2))
    assert len(groups) <= 14 and "other" in groups, groups
    two = section(markup, 2)
    for check, level in (("Y1", "a11y"), ("Y2", "design"), ("Y3", "query-budget"), ("X7", "other")):
        after = two[two.index(f'id="level-{level}"'):]
        assert f'id="check-{check}"' in after[:after.find('<h3 id="level-', 10) if '<h3 id="level-' in after[10:] else None], (check, level)


def a_shared_suite_output_counts_once(base):
    repo, evidence = full_branch(base)
    output(evidence, "U1.txt", "tests/run.sh", "== cart\nok   coupon_once\nFAIL tampered_total\nok   refund_issued\n",
           repo, working_tree(repo), exit_code=1)
    add_rows(repo, f"| U1 | AC1 | unit | the suite | + | all ok | FAIL | [output]({evidence}/U1.txt) |\n"
                   f"| U2 | AC2 | unit | the suite again | + | all ok | FAIL | [output]({evidence}/U1.txt) |\n",
             header="| # | AC | Level | Check | Case | Expected | Result | Evidence |")
    _, markup = generate(repo, base)
    assert "(3 tests in the runner output, 1 FAIL)" in summary_row(markup, "tests"), summary_row(markup, "tests")
    second = check_block(markup, "U2")
    assert 'class="runner"' not in second and 'href="#check-U1"' in second, second[:800]


def steps_past_the_total_are_linked(base):
    repo, evidence = full_branch(base)
    rows = ""
    for n in range(11):
        put(os.path.join(evidence, f"img{n:02d}.png"), PNG + bytes([n]) * (1900 * 1024))
        rows += f"| B{n} | AC1 | image {n} | + | an image | PASS | [img]({evidence}/img{n:02d}.png) |\n"
    for n in range(20):
        output(evidence, f"st{n}.txt", "make st", "".join(f"step: s{n} {k:06d} {'w' * 60}\n" for k in range(2500)),
               repo, working_tree(repo))
        rows += f"| T{n} | AC1 | steps {n} | + | ok | PASS | [output]({evidence}/st{n}.txt) |\n"
    add_rows(repo, rows)
    _, markup = generate(repo, base)
    steps = text(re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", check_block(markup, "T19"), re.S).group(1))
    assert "past the 20 MB" in steps, steps[:300]


def the_notes_count_toward_the_total(base):
    repo, evidence = full_branch(base)
    write(repo, "decision-notes", NOTES + "\n" + "n" * (1900 * 1024) + "\n")
    rows = ""
    for n in range(11):
        put(os.path.join(evidence, f"img{n:02d}.png"), PNG + bytes([n]) * (1900 * 1024))
        rows += f"| B{n} | AC1 | image {n} | + | an image | PASS | [img]({evidence}/img{n:02d}.png) |\n"
    add_rows(repo, rows)
    _, markup = generate(repo, base)
    embedded = [n for n in range(11) if "data:image/png" in check_block(markup, f"B{n}")]
    assert len(embedded) <= 8, embedded


def a_comment_before_the_first_paragraph_is_skipped(base):
    repo, _ = full_branch(base)
    write(repo, "decision-notes", NOTES.replace("## What the task must do\n",
                                                "## What the task must do\n<!-- one paragraph\nthat works alone -->\n"))
    _, markup = generate(repo, base)
    task = summary_row(markup, "task")
    assert task.startswith("Task The task must stop a second discount"), task


def triage_lens_names_get_their_brief(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW.replace("lenses: correctness, tests", "lenses: correctness, ux-a11y-i18n (added: forms, labels)"))
    _, markup = generate(repo, base)
    rows = [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
            for r in re.findall(r"<tr>(.*?)</tr>", re.search(r'aria-label="Table: Lens, What it checks[^"]*"><table>.*?<tbody>(.*?)</tbody>',
                                                              section(markup, 3), re.S).group(1), re.S)]
    assert [r[0] for r in rows] == ["correctness", "ux-a11y-i18n (added: forms, labels)"], rows
    write(repo, "review", REVIEW.replace("lenses: correctness, tests", "lenses: correctness, ux-a11y-i18n"))
    _, markup = generate(repo, base)
    rows = re.findall(r'<tr><th scope="row">ux-a11y-i18n</th><td>(.*?)</td>', section(markup, 3))
    assert rows and rows[0] == "Use only when the diff changes user-facing UI or copy.", rows


def coverage_without_an_ac_column_is_not_recorded(base):
    repo, evidence = full_branch(base)
    write(repo, "validation", f"# Validation\n\n| # | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|\n"
                              f"| A1 | Coupon applied once | + | one line | PASS | [output]({evidence}/A1.txt) |\n")
    _, markup = generate(repo, base)
    meta = text(re.search(r'id="s01".*?<span class="meta">(.*?)</span>', markup, re.S).group(1))
    assert meta == "3 criteria · coverage not recorded", meta


def a_merge_order_item_is_not_the_merge_note(base):
    repo, _ = full_branch(base)
    write(repo, "decision-notes", NOTES.replace("Decide if one FAIL", "- Merge order: #6 before this one.\n- Decide if one FAIL"))
    _, markup = generate(repo, base)
    items = listed(markup, "decisions")
    assert "recommendation not recorded" in items[0] and any("Merge order" in i for i in items[1:]), items

def a_check_id_with_punctuation_gets_its_level(base):
    repo, evidence = full_branch(base)
    add_rows(repo, "| A1.2 | AC1 | dotted | + | ok | PASS | none |\n")
    report = open(path_of(repo, "validation")).read()
    write(repo, "validation", report.replace("| unit | yes | [A1]", "| e2e | yes | A1.2. |\n|  | yes | A1 |\n| unit | yes | [A1]"))
    _, markup = generate(repo, base)
    groups = re.findall(r'<div class="level[^"]*"><h3 id="level-([^"]+)">(.*?)</div>', section(markup, 2), re.S)
    assert [level for level, body in groups if "A1.2" in body] == ["e2e"], [(level, "A1.2" in body) for level, body in groups]
    assert [level for level, body in groups if 'id="check-A1"' in body] == ["unit"], [level for level, _ in groups]



# v2 AC7: every reference the page can open is a link

def references_link_to_github_and_local_files(base):
    repo, evidence = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Cart", "headRefOid": head, "state": "OPEN"}
    page, markup = generate(repo, base, {"pr": pr})
    hrefs = [dict(attrs).get("href") for tag, attrs in tags(markup) if tag == "a"]
    for wanted in ("https://github.com/o/shop/pull/7", f"https://github.com/o/shop/commit/{head}",
                   f"https://github.com/o/shop/blob/{head}/src/cart.py#L2", "https://github.com/o/shop/pull/7#c123",
                   "file://" + urllib.parse.quote(os.path.realpath(path_of(repo, "review")))):
        assert wanted in hrefs, (wanted, [h for h in hrefs if h and not h.startswith("#")])
    foot = footer(markup)
    assert f'href="https://github.com/o/shop/commit/{head}"' in foot and 'href="https://github.com/o/shop/pull/7"' in foot, foot
    assert f'href="file://{urllib.parse.quote(os.path.realpath(page))}"' in foot, "the page doesn't link itself"
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    assert f"/commit/{tree}" not in markup, "a tree id was linked as a commit"
    assert all(re.search(r' rel="noreferrer noopener"', a) for a in re.findall(r'<a href="https:[^>]*>', markup)), "a remote link without rel"
    _, markup = generate(repo, base)
    assert "github.com/o/shop/commit" not in markup and "/blob/" not in markup, "no PR, yet a GitHub link"
    assert "file://" + urllib.parse.quote(os.path.realpath(path_of(repo, "review"))) in markup, "local reports stop linking without a PR"


def a_missing_verify_report_says_how_to_make_it(base):
    repo, evidence = full_branch(base)
    os.remove(path_of(repo, "verify"))
    put(os.path.join(evidence, "suite.txt"), "# in shop, exit 0\nALL PASSED\n")
    _, markup = generate(repo, base)
    blocker = [b for b in listed(markup, "blockers") if b.startswith("Verify")]
    assert blocker and "stop_checks.py verify" in blocker[0] and "suite.txt" in blocker[0], listed(markup, "blockers")
    assert "stop_checks.py verify" in footer_value(markup, "Verify"), footer_value(markup, "Verify")
    assert 'href="file://' + urllib.parse.quote(os.path.realpath(os.path.join(evidence, "suite.txt"))) + '"' in summary(markup)


def an_autolink_takes_linear_time(base):
    repo, _ = full_branch(base)
    write(repo, "decision-notes", NOTES.replace("A shopper sees", "https://x" + "/a" * 200_000 + " " + "a/" * 200_000 + ":" + " A shopper sees"))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for 800 KB of near-links"


def references_link_only_what_the_pr_head_holds(base):
    repo, evidence = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Cart", "headRefOid": head, "state": "OPEN"}
    put(os.path.join(repo, "untracked.py"), "x\n")
    put(os.path.join(evidence, "notes"), "<script>alert(1)</script>\n")
    hostile = 'https://x.test/a"onmouseover="alert(15)'
    review = os.path.realpath(path_of(repo, "review"))
    write(repo, "decision-notes", NOTES.replace("A shopper sees", f"See {repo}/src/cart.py:2, untracked.py:1, {evidence}/notes "
                                                                  f"and {hostile}. Report {review} here. A shopper sees"))
    _, markup = generate(repo, base, {"pr": pr})
    assert re.search(r'<a href="https://github\.com/o/shop/blob/[0-9a-f]+/src/cart\.py#L2"[^>]*>' + re.escape(repo), markup), \
        "an absolute repository path:line isn't linked to GitHub"
    assert re.search(r'<a href="file://[^"]*"[^>]*>' + re.escape(review) + "</a>", markup), "a report path in the text lost its link"
    hrefs = [dict(attrs).get("href") or "" for tag, attrs in tags(markup) if tag == "a"]
    assert not [h for h in hrefs if "untracked.py" in h or h.endswith("/notes")], "linked a file the PR head lacks, or an extensionless file"
    assert not [name for _, attrs in tags(markup) for name, _ in attrs if name.startswith("on")], "a quote in a URL broke out of its attribute"


def a_filename_git_holds_in_any_bytes_keeps_the_page(base):
    repo, _ = full_branch(base)
    put(os.path.join(base, "blob.txt"), "x\n")
    blob = git(repo, "hash-object", "-w", os.path.join(base, "blob.txt"))
    out = subprocess.run(["git", "update-index", "--add", "--cacheinfo", b"100644," + blob.encode() + b",bad\xffname"], cwd=repo,
                         capture_output=True)  # macOS can't hold the file itself; git's tree can
    assert out.returncode == 0, out
    git(repo, "commit", "-qm", "a non-UTF-8 name")
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Cart", "headRefOid": head, "state": "OPEN"}
    _, markup = generate(repo, base, {"pr": pr})
    assert f"/blob/{head}/src/cart.py#L" in markup, "a path:line stopped linking next to a non-UTF-8 name"


def switching_verify_off_raises_no_verify_blocker(base):
    repo, _ = full_branch(base)
    os.remove(path_of(repo, "verify"))
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "verify")
    assert out.returncode == 0, out
    _, markup = generate(repo, base)
    assert not [b for b in listed(markup, "blockers") if b.startswith("Verify")], listed(markup, "blockers")


def the_summary_parsers_take_linear_time(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + "Open:      (" + "nit " * 20_000 + "\n")
    put(path_of(repo, "spec"), SPEC.replace("1. A coupon applies once.", "1. A" + " " * 80_000 + "coupon applies once."))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for a long Nits entry and a long criterion"


def open_findings_hold_no_link_inside_a_link(base):
    repo, _ = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Cart", "headRefOid": head, "state": "OPEN"}
    write(repo, "review", REVIEW + "Open:      a bug at src/cart.py:2 — decide\nOpen:      another at src/cart.py:1 — decide\n")
    _, markup = generate(repo, base, {"pr": pr})
    assert "/blob/" in markup and not re.search(r"<a [^>]*>(?:(?!</a>).)*<a ", markup, re.S), "a link nested in a link"


# v2 AC8: the testing pyramid

PYRAMID_ROWS = """# Validation

| # | AC | Level | Check | Case | Expected | Result | Evidence |
|---|---|---|---|---|---|---|---|
| A1 | AC1 | unit | one | + | ok | PASS | none |
| A2 | AC1 | a11y | two | + | ok | PASS | none |
| A3 | AC2 | e2e | three | + | ok | FAIL | none |
| A4 | AC1 | integration | four | + | ok | PASS | none |
| A5 | AC3 | unit | five | + | ok | PASS | none |
"""


def the_tests_follow_the_pyramid(base):
    repo, _ = full_branch(base)
    write(repo, "validation", PYRAMID_ROWS)
    _, markup = generate(repo, base)
    two = section(markup, 2)
    assert re.findall(r'<h3 id="level-([^"]+)">', two) == ["e2e", "integration", "unit", "a11y"], re.findall(r'<h3 id="level-([^"]+)">', two)
    tiers = [text(m) for m in re.findall(r'<label for="f-lvl-[a-z0-9]+" class="tier t\d">(.*?)</label>', two, re.S)]
    assert tiers == ["e2e 1", "integration 1", "unit 2"], tiers
    assert re.search(r'<label for="f-lvl-all" class="tier-all">All levels <span class="n">5</span>', two), "no All choice"
    assert re.search(r'id="f-lvl-all" checked', two), "All isn't the default"
    for level in ("e2e", "integration", "unit"):
        assert f".filter:has(#f-lvl-{level}:checked) .level:not(.lvl-{level})" in markup, level
        assert f'<div class="level lvl-{level}">' in two, level
    head = re.search(r"<thead>(.*?)</thead>", section(markup, 1), re.S)
    assert text(head.group(1)) == "AC Criterion e2e integration unit a11y", text(head.group(1))
    line = summary_row(markup, "tests")
    assert line.index("e2e") < line.index("integration") < line.index("unit") < line.index("a11y"), line


def a_level_without_checks_is_not_a_choice(base):
    repo, _ = full_branch(base)
    write(repo, "validation", PYRAMID_ROWS.replace("| e2e |", "| unit |").replace("| integration |", "| unit |"))
    _, markup = generate(repo, base)
    two = section(markup, 2)
    assert re.search(r'id="f-lvl-e2e" disabled>', two) and re.search(r'id="f-lvl-integration" disabled>', two), two[:1500]
    assert not re.search(r'id="f-lvl-unit" disabled>', two)


def the_coverage_shows_each_criterion_short(base):
    repo, _ = full_branch(base)
    put(path_of(repo, "spec"), SPEC.replace("1. A coupon applies once. — verify: test.",
                                            "1. A coupon applies once. A refresh never adds a second one, nor does `a.b()`. — verify: test."))
    _, markup = generate(repo, base)
    row = re.search(r'<tr[^>]*id="ac-1">(.*?)</tr>', section(markup, 1), re.S).group(1)
    cell = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)[0]
    assert text(cell.split('<details class="crit">')[0]) == "A coupon applies once.", cell
    fold = re.search(r'<details class="crit"><summary>(.*?)</summary>(.*?)</details>', cell, re.S)
    assert fold and text(fold.group(2)).startswith("A refresh never adds a second one, nor does a.b()"), cell
    assert re.search(r'<a class="chip" href="#check-A1"><code>A1</code>', row), row
    short = re.search(r'<tr[^>]*id="ac-3">(.*?)</tr>', section(markup, 1), re.S).group(1)
    assert '<summary>the rest</summary>' in row and "<summary>how it&#x27;s checked</summary>" in short, short


# v2 AC9: the summary says each thing once

def a_level_not_run_shows_once(base):
    repo, evidence = full_branch(base)
    add_rows(repo, "| A9 | AC3 | screen reader | + | read | NOT RUN | no screen reader here |\n")
    report = open(path_of(repo, "validation")).read()
    write(repo, "validation", report.replace("| unit | yes | [A1]", "| a11y | no | no screen reader here (A9) |\n| unit | yes | [A1]"))
    _, markup = generate(repo, base)
    line = summary_row(markup, "tests")
    assert line.count("a11y") == 1 and "a11y not run (1 check): no screen reader here" in line, line
    skipped = "| e2e | no | no browser here |\n"
    write(repo, "validation", report.replace("| unit | yes | [A1]", skipped * 2 + "| unit | yes | [A1]"))
    line = summary_row(generate(repo, base)[1], "tests")
    assert line.count("e2e not run") == 1 and line.index("e2e") < line.index("unit"), line


def the_review_line_counts_lenses_and_rounds_apart(base):
    repo, _ = full_branch(base)
    review = REVIEW.replace("re-check rounds: 1", "re-check rounds: 2").replace(
        "Not run:   security lens — no auth code touched", "Not run:   none (every lens ran)")
    write(repo, "review", review + LENSES + "| correctness-recheck | codex | 1 | 1 | fixed=1 |\n")
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "claude-review")
    assert out.returncode == 0, out
    _, markup = generate(repo, base)
    line = summary_row(markup, "review")
    assert "recheck" not in line and "2 re-check rounds" in line, line
    assert "none" not in line and "every lens ran" not in line, line
    assert "cross-model: codex" in line and "claude-review off by you" in line, line
    write(repo, "review", review.replace("Not run:   none (every lens ran)", "Not run:   cross-model, claude-review switched off by the user"))
    line = summary_row(generate(repo, base)[1], "review")
    assert "switched off" not in line and "claude-review off by you" in line, line
    write(repo, "review", review + LENSES + "| correctness | claude | not run: claude-review switched off by the user | | |\n")
    line = summary_row(generate(repo, base)[1], "review")
    assert "switched off" not in line and "claude-review off by you" in line, line


def a_merge_item_in_the_notes_is_the_recommendation(base):
    repo, _ = full_branch(base)
    for item in ("- Merge with CI green: the risk is low.", "- Merge: with CI green, the risk is low.",
                 "- Merge if CI is green: the risk is low.", "- Merge — CI is green, the risk is low."):
        write(repo, "decision-notes", NOTES.replace("Decide if one FAIL", item + "\n- Decide if one FAIL"))
        _, markup = generate(repo, base)
        items = listed(markup, "decisions")
        merge = [i for i in items if "the risk is low" in i]
        assert len(merge) == 1 and "recommendation not recorded" not in " ".join(items), items


def several_nits_show_one_per_line(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + "Open:      (3 Nits kept) a typo in the help; a long line; a stale comment — decide later\n")
    _, markup = generate(repo, base)
    three = section(markup, 3)
    entry = re.search(r'<li class="entry"[^>]*><details><summary>(?:(?!</summary>).)*3 Nits kept.*?</details>', three, re.S)
    assert entry, three[:3000]
    parts = [text(p) for p in re.findall(r'<[^>]*class="part"[^>]*>(.*?)</span>', entry.group(0), re.S)]
    assert any(p.startswith("a typo in the help") for p in parts) and any(p.startswith("a long line") for p in parts), parts


def lens_names_split_in_linear_time(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW.replace("lenses: correctness, tests", "lenses: correctness, tests (" + "a," * 300_000))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for a lenses field of 300,000 commas"


def report_copies_past_the_total_leave_nothing_embedded(base):
    repo, evidence = full_branch(base)
    wide = "\n" + "語" * (1900 * 1024) + "\n"
    write(repo, "decision-notes", NOTES + wide)
    write(repo, "review", REVIEW + wide)
    write(repo, "pr-body", "A body." + wide)
    put(os.path.join(evidence, "img.png"), PNG + b"x" * 1024)
    add_rows(repo, f"| B1 | AC1 | an image | + | an image | PASS | [img]({evidence}/img.png) |\n")
    _, markup = generate(repo, base)
    assert "data:image/png" not in check_block(markup, "B1"), "AC6: the copies passed 20 MB and an image was still embedded"


QUALITY_LOG = os.path.expanduser("~/.agents/logs/quality.jsonl")  # quality_log() under the suite: this HOME's


def signals_pr(repo, url="https://github.com/O/shop/pull/7"):
    return {"pr": {"number": 7, "url": url, "title": "Apply a coupon once", "headRefOid": git(repo, "rev-parse", "HEAD"),
                   "state": "OPEN"}}


def after_head():
    """A ts the way quality-log writes it, an hour after the commit new_repo just made."""
    return (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S%z")


def pr_line(**fields):
    line = {"ts": after_head(), "kind": "pr", "name": 7, "repo": "shop", "branch": "feature/cart", "harness": "claude",
            "tier": "standard", "host": "github.com", "remote": "o/shop", "added": 40, "deleted": 3, "files": 5,
            "test_files": 2, "tests_deleted": 0, "verify": "pass", "verify_current": True, "red_check": "passed",
            "coverage": {"covered": 9, "measured": 12},
            "tampered": {"removed": 0, "loosened": 0, "changed": 0, "skipped_or_focused": 0}}
    return json.dumps({**line, **fields})


def put_log(*lines, tail="\n"):
    put(QUALITY_LOG, "\n".join(lines) + tail)


def signals_text(markup):
    """The Signals row as text, without the space text() leaves where a state span closed before a comma."""
    return text(summary_row_markup(markup, "signals")).replace(" ,", ",")


def tone(row, words):
    """The tone of the state span holding exactly these words, or None."""
    found = re.search(rf'<span class="st (\w+)">{re.escape(html.escape(words, quote=True))}</span>', row)
    return found and found.group(1)


def signals_show_the_latest_usable_matching_line(base):
    repo, _ = full_branch(base)
    # The later line in the file wins even with the earlier ts; quality-log keeps the remote's case as GitHub gives it.
    put_log(pr_line(added=1, ts="2030-01-02T10:00:00+0000"),
            pr_line(added=40, ts="2030-01-01T09:00:00+0000", host="GitHub.com", remote="O/Shop"),
            pr_line(added=444, name=8),
            json.dumps({"kind": "lens", "name": 7, "host": "github.com", "remote": "o/shop", "added": 333}),
            pr_line(added=999, remote="o/other"),
            pr_line(added=888, host="github.example.com"),
            json.dumps({"kind": "lens", "name": "tests", "findings": 1}),
            pr_line(added=777, remote=["o/shop"]),
            pr_line(added=666, name="7"),
            pr_line(added=555)[:60], tail="")
    _, markup = generate(repo, base, signals_pr(repo))
    assert re.search(r'id="sum-gates".*?</div><div class="row"><dt id="sum-signals">Signals</dt>', summary(markup), re.S), \
        "AC1: no Signals row right after Gates"
    row = signals_text(markup)
    assert "recorded 2030-01-01T09:00:00+0000" in row, row
    assert "40 lines added, 3 deleted, 5 files" in row, f"AC1: the later matching line didn't win: {row}"
    for decoy in ("999", "888", "777", "666", "555", "444", "333", " 1 lines"):
        assert decoy not in row, f"AC1: a decoy or unusable line ({decoy}) was read: {row}"
    for words in ("2 test files touched", "0 test files deleted", "verify pass, current when recorded", "red check passed",
                  "coverage 9/12 lines (75%)", "weakened tests: 0 removed, 0 loosened, 0 changed, 0 skipped or focused"):
        assert words in row, (words, row)



def signals_show_the_structure_facts(base):
    """Each part of structure-facts' numbers on its own, a measured 0 as 0, and one not measured said in words: never
    unknown, never a warning, as these are facts with no threshold."""
    repo, _ = full_branch(base)
    complexity = {"added": 12, "removed": 4, "max_function": 7, "functions": 3, "complete": True}
    clones = {"count": 2, "complete": True}
    zeros = {"complexity": {"added": 0, "removed": 0, "max_function": 0, "functions": 0, "complete": True},
             "new_clones": {"count": 0, "complete": True}}
    for line, shown in (
            (pr_line(structure={"complexity": complexity, "new_clones": clones}), "complexity +12 \u22124 (largest +7) · 2 new clones"),
            (pr_line(structure=zeros), "complexity +0 \u22120 (largest +0) · 0 new clones"),
            (pr_line(structure={"complexity": complexity, "new_clones": None}), "complexity +12 \u22124 (largest +7) · clones not measured"),
            (pr_line(structure={"complexity": None, "new_clones": clones}), "complexity not measured · 2 new clones"),
            (pr_line(structure={"complexity": None, "new_clones": None}), "complexity not measured · clones not measured"),
            (pr_line(structure=None), "structure not measured"),
            (pr_line(), "structure not measured"),  # a line from before the kit recorded structure
            (pr_line(structure={"complexity": {**complexity, "complete": False}, "new_clones": {"count": 0, "complete": False}}),
             "complexity +12 \u22124 (largest +7) (partial) · 0 new clones (partial)"),
            (pr_line(structure={"complexity": complexity, "new_clones": {"count": 0, "complete": False}}),
             "complexity +12 \u22124 (largest +7) · 0 new clones (partial)"),
            (pr_line(structure={"complexity": {**complexity, "complete": False}, "new_clones": clones}),
             "complexity +12 \u22124 (largest +7) (partial) · 2 new clones"),
            (pr_line(structure={"complexity": {**complexity, "added": "12"}, "new_clones": clones}),
             "complexity not measured · 2 new clones")):  # hand-edited: read as not measured, never a crash
        put_log(line)
        _, markup = generate(repo, base, signals_pr(repo))
        row = signals_text(markup)
        assert row.endswith(f"0 skipped or focused · {shown}"), f"AC5: expected {shown!r} at the end: {row}"
        markup_row = summary_row_markup(markup, "signals")
        assert "st need" not in markup_row and markup_row.count('<span class="st dim">unknown</span>') == 0, markup_row
        for words in ("complexity not measured", "clones not measured", "structure not measured"):
            assert words not in shown or tone(markup_row, words) == "dim", f"AC5: {words!r} isn't dim: {markup_row}"

def signal_warnings_carry_the_need_tone(base):
    repo, _ = full_branch(base)
    put_log(pr_line(ts="2020-01-01T00:00:00+0000", tests_deleted=2, verify="fail", verify_current=False, red_check="failed",
                    tampered={"removed": 1, "loosened": 2, "changed": 3, "skipped_or_focused": 4}))
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row_markup(markup, "signals")
    for words in ("recorded before the last commit", "2 test files deleted", "fail", "stale when recorded", "failed",
                  "1 removed", "2 loosened", "3 changed", "4 skipped or focused"):
        assert tone(row, words) == "need", f"AC2: {words!r} isn't need: {row}"
    for verdict in ("empty", "none"):
        put_log(pr_line(verify=verdict))
        _, markup = generate(repo, base, signals_pr(repo))
        assert tone(summary_row_markup(markup, "signals"), verdict) == "need", f"AC2: verify {verdict} isn't need"
    put_log(pr_line(verify=None, verify_current=None, coverage=None, tampered=None, red_check=None))
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row_markup(markup, "signals")
    assert "st need" not in row, f"AC2: a null value carries need: {row}"
    assert row.count('<span class="st dim">unknown</span>') == 5, f"AC2: null values aren't dim unknown: {row}"
    assert "verify unknown, unknown · red check unknown · coverage unknown · weakened tests: unknown" in signals_text(markup), \
        signals_text(markup)
    put_log(pr_line())
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row_markup(markup, "signals")
    assert "st need" not in row, f"AC2: a clean line carries need: {row}"
    for words in ("0 test files deleted", "pass", "current when recorded", "passed", "0 removed", "0 skipped or focused"):
        assert tone(row, words) == "ok", f"AC2: clean {words!r} isn't ok: {row}"


def coverage_shows_a_percentage_only_when_measured(base):
    repo, _ = full_branch(base)
    for coverage, shown in (({"covered": 1, "measured": 3}, "coverage 1/3 lines (33%)"),
                            ({"covered": 0, "measured": 0}, "coverage 0 lines measured"), (None, "coverage unknown")):
        put_log(pr_line(coverage=coverage))
        _, markup = generate(repo, base, signals_pr(repo))
        row = summary_row(markup, "signals")
        assert shown in row, f"AC3: {coverage} shows {row}"
        assert coverage and coverage["measured"] or "%" not in row, f"AC3: a percentage without measured lines: {row}"


def signals_without_a_line_say_why(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    assert summary_row(markup, "signals") == "Signals no PR yet", summary_row(markup, "signals")
    _, markup = generate(repo, base, {"error": True})
    row = summary_row(markup, "signals")
    assert "unknown" in row and "error connecting to api.github.com" in row, row
    _, markup = generate(repo, base, signals_pr(repo))
    assert summary_row(markup, "signals") == "Signals not recorded", f"AC4: no log file: {summary_row(markup, 'signals')}"
    put_log("null", "[1, 2]", "not json", '"a string"', pr_line(host=None), pr_line(name=True),
            pr_line(added=5)[:-20], tail="")
    _, markup = generate(repo, base, signals_pr(repo))
    assert summary_row(markup, "signals") == "Signals not recorded", f"AC4: an unusable line was read: {summary_row(markup, 'signals')}"
    put_log(pr_line(added="40", files=None, test_files=-1, verify=3, red_check=3, verify_current="yes",
                    coverage={"covered": "9", "measured": 12}, tampered={"removed": 1.5}))
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row_markup(markup, "signals")
    assert "st need" not in row, f"AC4: a wrong-typed field carries need: {row}"
    assert ("unknown lines added, 3 deleted, unknown files · unknown test files touched · 0 test files deleted · verify unknown, "
            "unknown · red check unknown · coverage unknown · weakened tests: unknown removed, unknown loosened, unknown changed, "
            "unknown skipped or focused") in signals_text(markup), f"AC4: wrong types don't show unknown: {signals_text(markup)}"
    if os.geteuid() != 0:  # root reads a file whatever its mode
        put_log(pr_line())
        os.chmod(QUALITY_LOG, 0o000)
        _, markup = generate(repo, base, signals_pr(repo))
        row = summary_row_markup(markup, "signals")
        assert tone(row, "unavailable") == "need" and "Permission denied" in text(row), f"AC4: chmod 000: {text(row)}"
    if os.path.lexists(QUALITY_LOG):
        os.remove(QUALITY_LOG)
    os.makedirs(QUALITY_LOG)
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row_markup(markup, "signals")
    assert tone(row, "unavailable") == "need" and "directory" in text(row), f"AC4: a log that is a directory: {text(row)}"
    os.rmdir(QUALITY_LOG)
    _, markup = generate(repo, base, signals_pr(repo, url="https://github.com/O/shop/issues/7"))
    assert "names no repository" in summary_row(markup, "signals"), summary_row(markup, "signals")
    # json.loads turns "\ud800" into a lone surrogate, which once made the page's UTF-8 write fail
    put_log(pr_line(ts="\ud800"))
    _, markup = generate(repo, base, signals_pr(repo))
    assert "recorded unknown" in summary_row(markup, "signals"), summary_row(markup, "signals")
    put_log(pr_line(ts=None))
    _, markup = generate(repo, base, signals_pr(repo))
    assert "recorded unknown" in summary_row(markup, "signals"), summary_row(markup, "signals")
    put_log(pr_line(verify="\ud800", red_check="\ud800"))
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row(markup, "signals")
    assert "verify unknown" in row and "red check unknown" in row, row


def red_checks_that_didnt_run_are_dim(base):
    repo, _ = full_branch(base)
    for verdict, words in (("skipped", "skipped"), ("not_run", "not run")):
        put_log(pr_line(red_check=verdict))
        _, markup = generate(repo, base, signals_pr(repo))
        assert tone(summary_row_markup(markup, "signals"), words) == "dim", (verdict, summary_row(markup, "signals"))


def signals_read_only_the_last_2_mb(base):
    repo, _ = full_branch(base)
    filler = json.dumps({"kind": "lens", "name": "tests", "findings": 1, "notes": "x" * 200})
    others = [filler] * (2 * 1024 * 1024 // len(filler) + 100)
    put_log(pr_line(added=40), *others)
    _, markup = generate(repo, base, signals_pr(repo))
    row = summary_row(markup, "signals")
    assert row == "Signals not in the last 2 MB of the log", f"AC5: a line before the last 2 MB: {row}"
    put_log(pr_line(added=40), *others, pr_line(added=41))
    _, markup = generate(repo, base, signals_pr(repo))
    assert "41 lines added" in summary_row(markup, "signals"), f"AC5: {summary_row(markup, 'signals')}"
    # The window starts on the matching line's first byte: that line is whole and must be read.
    match = pr_line(added=42)
    empty = json.dumps({"kind": "lens", "name": "tests", "notes": ""})
    big = json.dumps({"kind": "lens", "name": "tests", "notes": "x" * (2 * 1024 * 1024 - len(match) - 1 - len(empty) - 1)})
    put_log(filler, match, big)
    assert os.path.getsize(QUALITY_LOG) - 2 * 1024 * 1024 == len(filler) + 1
    _, markup = generate(repo, base, signals_pr(repo))
    assert "42 lines added" in summary_row(markup, "signals"), f"AC5: a whole first line was dropped: {summary_row(markup, 'signals')}"


# Investigation mode

INVESTIGATION_NOTES = """Mode: investigation

## The question
Why do checkout totals drift by a cent?

## The answer
Rounding runs twice: once per line and once on the total.

Confidence: medium

## Hypotheses

### H1. Rounding runs twice
The cart rounds in two places.
For: the trace shows two round() calls.
Against:

### H2. The currency table is stale
For:
Against: the table matches the bank's rates.

## Options
- Round once at the total: chosen, the bank rounds there.
- Round per line: rejected, it drifts on long carts.

## Not checked
The refund path: it needs the payment sandbox.

## What you must decide
- Round at the total or per line.
  Unblocks: the fix in the cart.
- Keep the old receipts as they are.
"""

FINDINGS_ROWS = """| # | AC | Check | Expected | Result | Evidence |
|---|---|---|---|---|---|
| F1 | AC1 | two round calls in the trace | one call | Supports | [trace](trace.txt) |
| F2 | AC1 | the currency table is stale | rates differ | Refutes | rates match |
| F3 | AC2 | refund rounding | one rounding | Inconclusive | one sample only |
| F4 | AC2 | the refund path | one rounding | NOT RUN | needs the payment sandbox |
| F5 | AC1 | a cart of 100 lines | totals match | FAIL | off by 0.01 |
| F6 | AC1 | a cart of 1 line | totals match | PASS | totals match |
"""


def investigation_branch(base, notes=INVESTIGATION_NOTES, rows=FINDINGS_ROWS):
    """shop on feature/cart, its notes holding the marker and its validation findings rows."""
    repo = new_repo(base)
    evidence = path_of(repo, "evidence")
    put(path_of(repo, "spec"), "# Totals drift\n\nGoal: find why totals drift.\n\n## Acceptance criteria\n"
                               "1. The cause is named. — verify: manual.\n2. The refund is checked. — verify: manual.\n")
    write(repo, "decision-notes", notes)
    output(evidence, "trace.txt", "make trace", "round(a)\nround(total)\n", repo, working_tree(repo))
    write(repo, "validation", f"# Validation: totals drift\n\n## A. Findings\n\n{rows}")
    return repo, evidence


def element(page, element_id):
    """The markup of the element with that id, up to its own closing tag."""
    start = re.search(rf'<(\w+)\b[^>]*\bid="{re.escape(element_id)}"', page)
    assert start, f"no element {element_id}"
    tag, depth = start.group(1), 0
    for found in re.finditer(rf"<(/?){tag}\b[^>]*>", page[start.start():]):
        depth += -1 if found.group(1) else 1
        if depth == 0:
            return page[start.start():start.start() + found.end()]
    raise AssertionError(f"{element_id} is not closed")


def an_investigation_is_chosen_by_the_marker_only(base):
    repo, _ = investigation_branch(base)
    _, markup = generate(repo, base)
    assert "Investigation page for" in text(markup) and 'id="sum-question"' in markup, "AC1: the marker gave no investigation page"
    assert footer_value(markup, "Changed files").startswith("2"), "AC1: the footer doesn't count the diff's files"
    write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Mode: investigation", "MODE:  Investigation \t"))
    _, markup = generate(repo, base)
    assert "Investigation page for" in text(markup), "AC1: the marker is matched in any case"
    for notes in (INVESTIGATION_NOTES.replace("Mode: investigation\n", ""),
                  INVESTIGATION_NOTES.replace("Mode: investigation", "```\nMode: investigation\n```"),
                  INVESTIGATION_NOTES.replace("Mode: investigation", "Mode: investigation, maybe")):
        write(repo, "decision-notes", notes)
        _, markup = generate(repo, base)
        assert "Investigation page for" not in text(markup) and "Decision page for" in text(markup), notes[:60]
        sections = [h.strip() for h in re.findall(r'<h2[^>]*><span class="num">\d\d</span>\s*([^<]+)</h2>', markup)]
        assert sections == HEADINGS, sections


def an_empty_diff_only_suggests_the_mode(base):
    repo = new_repo(base)
    _, markup = generate(repo, base)
    assert not any("Mode: investigation" in item for item in listed(markup, "warnings")), "a branch with a diff is told of the mode"
    git(repo, "checkout", "-qb", "feature/empty", "main")
    write(repo, "decision-notes", NOTES)
    _, markup = generate(repo, base)
    assert "Decision page for" in text(markup), "AC1: an empty diff switched the mode by itself"
    named = [item for item in listed(markup, "warnings") if "Mode: investigation" in item]
    assert len(named) == 1, f"AC1: no warning names the marker: {listed(markup, 'warnings')}"


def the_investigation_page_shows_each_section(base):
    repo, _ = investigation_branch(base)
    _, markup = generate(repo, base)
    assert "Why do checkout totals drift by a cent?" in summary_row(markup, "question")
    answer = summary_row(markup, "answer")
    assert "Rounding runs twice" in answer and "medium confidence" in answer and "Confidence:" not in answer, answer
    decisions = listed(markup, "decisions")
    assert len(decisions) == 2, decisions
    assert "Round at the total or per line." in decisions[0] and "Unblocks: the fix in the cart." in decisions[0], decisions
    assert "Keep the old receipts" in decisions[1] and "Unblocks: not recorded" in decisions[1], decisions
    assert listed(markup, "blockers") == [], listed(markup, "blockers")
    assert "2 decisions · 0 blockers" in text(re.search(r'<p class="strip">.*?</p>', markup, re.S).group(0))
    hypotheses = section(markup, 1)
    assert "H1. Rounding runs twice" in text(element(hypotheses, "hyp-1")) and "The cart rounds in two places." in text(element(hypotheses, "hyp-1"))
    assert "two round() calls" in text(element(hypotheses, "hyp-1-for"))
    assert "none recorded" in text(element(hypotheses, "hyp-1-against")), "AC2: an empty Against isn't none recorded"
    assert "none recorded" in text(element(hypotheses, "hyp-2-for")) and "bank's rates" in text(element(hypotheses, "hyp-2-against"))
    assert "Round per line: rejected" in text(section(markup, 2)) and "Round per line" not in text(hypotheses)
    unchecked = section(markup, 3)
    assert "The refund path: it needs the payment sandbox." in text(element(unchecked, "not-checked-notes"))
    not_run = element(unchecked, "not-run")
    assert 'href="#check-F4"' in not_run and "needs the payment sandbox" in text(not_run) and "F3" not in text(not_run), not_run
    findings = section(markup, 4)
    assert all(f'id="check-F{n}"' in findings for n in range(1, 7)), "AC2: a check row isn't under Findings"
    for kind in ("spec", "decision-notes", "validation"):
        assert f'id="annex-{kind}"' in section(markup, 5), kind
    assert footer_value(markup, "Repository") == "shop"
    base_sha = git(repo, "merge-base", "HEAD", "main")
    assert base_sha in footer_value(markup, "Base") and "main" in footer_value(markup, "Base"), footer_value(markup, "Base")
    assert git(repo, "rev-parse", "HEAD") in footer_value(markup, "HEAD")
    assert footer_value(markup, "Working tree") == "clean" and footer_value(markup, "Branch") == "feature/cart"
    assert re.search(r"\d{4}-\d\d-\d\d \d\d:\d\d", footer_value(markup, "Generated")) and "decision-" in footer_value(markup, "Page")
    put(os.path.join(repo, "notes.txt"), "draft\n")
    _, markup = generate(repo, base)
    assert footer_value(markup, "Working tree") == "dirty", footer_value(markup, "Working tree")
    write(repo, "decision-notes", INVESTIGATION_NOTES.split("## What you must decide")[0] + "## What you must decide\n- None\n")
    _, markup = generate(repo, base)
    assert listed(markup, "decisions") == [] and "0 decisions" in text(markup), "AC2: an item None asks a decision"


def missing_investigation_sections_are_blockers(base):
    repo, _ = investigation_branch(base, notes="Mode: investigation\n\n## Options\n\n## Hypotheses\n### H1. A guess\nFor:\nAgainst:\n")
    _, markup = generate(repo, base)
    blockers = listed(markup, "blockers")
    for name in ("The question", "The answer", "What you must decide"):
        assert sum(name in item for item in blockers) == 1, (name, blockers)
    assert len(blockers) == 3, f"AC2: an empty part became a blocker: {blockers}"
    assert "none recorded" in text(section(markup, 2)) and "none recorded" in text(element(markup, "hyp-1-for"))
    for line, word in (("", "missing"), ("Confidence: certain", "unknown")):
        write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Confidence: medium", line))
        _, markup = generate(repo, base)
        blockers = listed(markup, "blockers")
        assert len(blockers) == 1 and "Confidence" in blockers[0] and word in blockers[0], (line, blockers)
    write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Confidence: medium", "Confidence: Medium — the refund path wasn't run"))
    _, markup = generate(repo, base)
    answer = summary_row(markup, "answer")
    assert listed(markup, "blockers") == [], f"AC2: a reason after the level made it unknown: {listed(markup, 'blockers')}"
    assert "medium confidence" in answer and "the refund path wasn" in answer and "Confidence:" not in answer, answer


def blocks_that_need_a_change_do_not_apply(base):
    repo, _ = investigation_branch(base)
    calls = os.path.join(base, "calls.log")
    for off in ((), ("validate",)):
        for phase in off:
            out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", phase)
            assert out.returncode == 0, out
        _, markup = generate(repo, base, {"error": True})
        assert not os.path.exists(calls), f"AC3: gh was called: {open(calls).read()}"
        page = text(markup)
        line = summary_row(markup, "gates")
        for block in ("PR", "CI", "verify", "review stamp", "validate stamp", "checks guide", "ship steps"):
            assert block in line, (block, line)
        assert page.count("does not apply") == 1 and "does not apply" in line, line
        for word in ("no PR yet", "missing", "unavailable", "phase off"):
            assert word not in page, (off, word)
        assert all(f'id="check-F{n}"' in section(markup, 4) for n in range(1, 7)), f"AC3: {off} hid a finding"
        assert listed(markup, "blockers") == [], listed(markup, "blockers")


def findings_are_not_failures(base):
    import sys
    sys.path.insert(0, os.path.join(KIT, "hooks"))
    import review_stamp
    legacy = FINDINGS_ROWS.split("\n")[:2] + [r for r in FINDINGS_ROWS.split("\n") if re.search(r"\| (PASS|FAIL|NOT RUN) \|", r)]
    for rows, ids in ((FINDINGS_ROWS, range(1, 7)), ("\n".join(legacy) + "\n", (4, 5, 6)),
                      (FINDINGS_ROWS.replace("| Supports |", "| **Supports** |"), range(1, 7))):
        repo, _ = investigation_branch(base, rows=rows)
        _, markup = generate(repo, base)
        words = {n: re.search(r'<span class="st [^"]*">([^<]+)</span>', check_block(markup, f"F{n}")).group(1) for n in ids}
        expected = {1: "Supports", 2: "Refutes", 3: "Inconclusive", 4: "NOT RUN", 5: "not as expected", 6: "as expected"}
        assert words == {n: expected[n] for n in ids}, words
        for n in ids:
            block = check_block(markup, f"F{n}")
            assert 'class="st need"' not in block and "FAIL" not in text(block), block
            assert "validation-shop-feature~cart.md:" in text(block), "AC4: a row lost its source line"
        assert listed(markup, "blockers") == [], f"AC4: a finding is a blocker: {listed(markup, 'blockers')}"
        if 1 in ids:
            assert "round(total)" in check_block(markup, "F1"), "AC4: a row lost its evidence"
        shutil.rmtree(repo)
    lines = FINDINGS_ROWS.split("\n")
    assert [row[1] for row in review_stamp.result_rows(lines, None)] == ["NOT RUN", "FAIL", "PASS"], \
        "AC4: the gates' parser reads findings words"
    guide = "### M1. Look\n- **Where:** you\n\n## Results\n\n| Step | Result | Evidence |\n|---|---|---|\n| M1 | Supports | seen |\n"
    assert review_stamp.guide_steps(guide)["M1"]["result"] is None, "AC4: the checks guide reads a findings word"


def the_notes_embed_their_evidence(base):
    outside = os.path.join(base, "secret.txt")
    put(outside, "SECRET-TOKEN-1\n")
    put(os.path.join(base, "secret2.txt"), "SECRET-TOKEN-2\n")
    notes = (INVESTIGATION_NOTES
             .replace("once on the total.", "once on the total. ![diagram](diagram.png)")
             .replace("two round() calls.", "two round() calls in `big.txt`.")
             .replace("Against:\n\n### H2", f"Against: [out]({outside})\n\n### H2")
             .replace("For:\nAgainst: the table", "For: `link.txt`\nAgainst: the table")
             .replace("the bank rounds there.", "the bank rounds there, `page.html`.")
             .replace("needs the payment sandbox.\n", "needs the payment sandbox, ![big](huge.png).\n")
             .replace("Keep the old receipts as they are.", "Keep the old receipts as they are, see [trace](gone.txt)."))
    repo, evidence = investigation_branch(base, notes=notes)
    put(os.path.join(evidence, "diagram.png"), PNG)
    put(os.path.join(evidence, "big.txt"), "FIRST LINE\n" + "".join(f"middle {n:07d}\n" for n in range(20000)) + "LAST LINE\n")
    os.symlink(os.path.join(base, "secret2.txt"), os.path.join(evidence, "link.txt"))
    put(os.path.join(evidence, "page.html"), "<script>alert(1)</script>")
    put(os.path.join(evidence, "huge.png"), PNG + b"\0" * (2 * 1024 * 1024 + 10))
    _, markup = generate(repo, base)
    assert 'src="data:image/png' in summary_row_markup(markup, "answer"), "AC5: the answer's image isn't embedded under it"
    big = element(markup, "hyp-1-for")
    assert "FIRST LINE" in big and "LAST LINE" in big and "middle 0010000" not in big and re.search(r"\bcut\b", text(big)), \
        "AC5: a text over 200 KB isn't embedded head and tail"
    assert "<details" in big, "AC5: a text file isn't folded"
    assert "SECRET-TOKEN" not in markup, "AC5: a file outside the evidence directory was read"
    for name in ("secret.txt", "secret2.txt"):
        assert "file://" + urllib.parse.quote(os.path.join(os.path.realpath(base), name)) not in markup, name
    assert "outside the evidence directory" in text(element(markup, "hyp-1-against")) and \
        "outside the evidence directory" in text(element(markup, "hyp-2-for")), "AC5: an outside file isn't named as not read"
    options = section(markup, 2)
    assert "page.html" in text(options) and "not linked" in text(options) and "page.html\"" not in options, "AC5: an .html file is linked"
    huge = section(markup, 3)
    assert "huge.png" in text(huge) and "over 2 MB" in text(huge) and "data:image" not in huge, "AC5: an image over 2 MB is embedded"
    blockers = listed(markup, "blockers")
    assert len(blockers) == 1 and "gone.txt" in blockers[0] and "missing" in blockers[0], f"AC5: {blockers}"


def brief_counts_findings(base):
    repo, _ = investigation_branch(base)
    git(repo, "checkout", "-qb", "feature/change")
    write(repo, "decision-notes", NOTES)
    write(repo, "validation", "| # | Check | Result | Evidence |\n|---|---|---|---|\n| A1 | a | PASS | e |\n"
                              "| A2 | b | FAIL | e |\n| A3 | c | NOT RUN | e |\n| A4 | d | Supports | e |\n")
    findings = "findings: 1 Supports, 1 Refutes, 1 Inconclusive, 1 NOT RUN, 1 as expected, 1 not as expected"
    change = "1 PASS, 1 FAIL, 1 NOT RUN"

    def brief(*args):
        out = sh(repo, REPORTS, *args, "brief")
        assert out.returncode == 0, out
        return out.stdout
    for branch, other in (("feature/cart", "feature/change"), ("feature/change", "feature/cart")):
        git(repo, "checkout", "-q", branch)
        for shown, name in ((brief(), branch), (brief("--branch", other), other)):
            if name == "feature/cart":
                assert findings in shown and "FAIL" not in shown and "mode: investigation" in shown, (branch, shown)
            else:
                assert change in shown and "findings:" not in shown and "mode:" not in shown, (branch, shown)
    git(repo, "checkout", "-q", "feature/cart")
    write(repo, "validation", "\n".join(r for r in FINDINGS_ROWS.split("\n") if not re.search(r"\| (PASS|FAIL) \|", r)))
    shown = brief()
    assert "findings: 1 Supports, 1 Refutes, 1 Inconclusive, 1 NOT RUN\n" in shown and "as expected" not in shown, shown
    rows = FINDINGS_ROWS.split("\n")
    write(repo, "validation", "\n".join(rows[:2] + [rows[2]] * 3 + [rows[5]] * 2 + [rows[6]] * 4) + "\n")
    shown = brief()
    assert "findings: 3 Supports, 0 Refutes, 0 Inconclusive, 2 NOT RUN, 0 as expected, 4 not as expected\n" in shown, shown


def the_skills_describe_the_investigation_notes(base):
    def read(*parts):
        with open(os.path.join(KIT, *parts)) as fh:
            return fh.read()
    ship = read("skills", "kit-ship", "SKILL.md")
    for needle in ("`Mode: investigation`", "`## The question`", "`## The answer`", "`Confidence: high`", "`## Hypotheses`",
                   "`For:`", "`Against:`", "`## Options`", "`## Not checked`", "`Unblocks:`", "without opening a pull request"):
        assert needle in ship, needle
    usage = "".join(line for line in read("bin", "reports").split("\n", 12)[:12] if line.startswith("#"))
    assert "investigation" in usage and "findings:" in usage, "bin/reports' usage doesn't describe the mode"
    assert "investigation" in read("bin", "decision-report").split('"""')[1], "the docstring doesn't describe the investigation page"


def a_marker_on_an_empty_diff_gives_the_investigation_page(base):
    repo, _ = investigation_branch(base)
    git(repo, "checkout", "-qb", "feature/empty", "main")
    write(repo, "decision-notes", INVESTIGATION_NOTES)
    write(repo, "validation", "# Validation\n\n" + "\n".join(FINDINGS_ROWS.split("\n")[:5]) + "\n")
    _, markup = generate(repo, base)
    assert "Investigation page for" in text(markup), "AC1: an empty diff with the marker isn't an investigation"
    assert "<dt>Changed files</dt>" not in footer(markup), "AC1: the footer counts files on a branch without a diff"
    assert "Mode: investigation" not in text(summary(markup)), "the investigation page suggests its own mode"
    words = [re.search(r'<span class="st [^"]*">([^<]+)</span>', check_block(markup, f"F{n}")).group(1) for n in (1, 2, 3)]
    assert words == ["Supports", "Refutes", "Inconclusive"], f"AC4: a findings-only table: {words}"


def the_investigation_page_keeps_its_csp_and_renders_hostile_text_inert(base):
    notes = (INVESTIGATION_NOTES.replace("### H1. Rounding runs twice", "### H1. <script>alert('h')</script>")
             .replace("Confidence: medium", "Confidence: medium — <img src=x onerror=alert('c')>"))
    repo, _ = investigation_branch(base, notes=notes)
    _, markup = generate(repo, base)
    metas = [dict(attrs) for tag, attrs in tags(markup) if tag == "meta" and dict(attrs).get("http-equiv")]
    assert [m["content"] for m in metas if m["http-equiv"] == "Content-Security-Policy"] == [CSP], metas
    assert not any(tag in ("script", "img") and "alert" in str(attrs) for tag, attrs in tags(markup)), "hostile text became markup"
    assert "<script>alert('h')</script>" in text(element(markup, "hyp-1")), "the title isn't shown as text"
    assert "onerror=alert('c')" in text(summary_row_markup(markup, "answer")), "the confidence reason isn't shown as text"


def hypotheses_keep_their_lists_and_subheadings(base):
    notes = INVESTIGATION_NOTES.replace(
        "The cart rounds in two places.\nFor: the trace shows two round() calls.\nAgainst:\n",
        "The cart rounds in two places.\n\n#### Where\nIn the cart and the receipt.\n\n- For:\n  - the trace shows two round() calls\n"
        "  - the receipt rounds too\n- Against: one cart in a hundred\n\n#### Second trace\nFor: the refund rounds twice too\n")
    repo, _ = investigation_branch(base, notes=notes)
    _, markup = generate(repo, base)
    first = element(markup, "hyp-1")
    assert "In the cart and the receipt." in text(first) and 'id="hyp-2"' in markup and "hyp-3" not in markup, \
        "a #### heading split its hypothesis"
    assert "two round() calls" in text(element(markup, "hyp-1-for")) and "receipt rounds too" in text(element(markup, "hyp-1-for"))
    assert "one cart in a hundred" in text(element(markup, "hyp-1-against")), "a listed Against: was dropped"
    assert "two round() calls" in text(element(markup, "hyp-1-for")) and "refund rounds twice" in text(element(markup, "hyp-1-for")), \
        "a second For: replaced the first"


def the_notes_embed_each_file_under_its_first_part(base):
    notes = (INVESTIGATION_NOTES
             .replace("drift by a cent?", "drift by a cent? See [the report](q.txt).")
             .replace("Confidence: medium", "Confidence: medium — one trace only, `checks/trace.txt:3`")
             .replace("Rounding runs twice:", "Rounding runs twice (`shared.txt`):")
             .replace("### H1. Rounding runs twice", "### H1. Rounding runs twice, `title.txt`")
             .replace("two round() calls.", "two round() calls in `shared.txt`, as `client.py:42` does.")
             .replace("Round at the total or per line.", "Round at the total or per line, `decide.txt`.")
             )
    repo, evidence = investigation_branch(base, notes=notes)
    for name in ("q.txt", "shared.txt", "title.txt", "decide.txt", "checks/trace.txt"):
        put(os.path.join(evidence, name), f"CONTENT OF {name}\n")
    _, markup = generate(repo, base)
    assert listed(markup, "blockers") == [], f"AC5: a source file the notes cite became a blocker: {listed(markup, 'blockers')}"
    assert "CONTENT OF q.txt" in summary_row_markup(markup, "question"), "AC5: the question's link isn't embedded"
    answer = summary_row_markup(markup, "answer")
    assert "CONTENT OF shared.txt" in answer and "CONTENT OF checks/trace.txt" in answer, "AC5: the answer's files aren't embedded"
    assert "CONTENT OF shared.txt" not in element(markup, "hyp-1-for") and "shown above" in element(markup, "hyp-1-for"), \
        "AC5: a file is embedded twice, or not where it first shows"
    assert "CONTENT OF title.txt" in element(markup, "hyp-1"), "AC5: a hypothesis title's file isn't embedded"
    assert "CONTENT OF decide.txt" in " ".join(listed(markup, "decisions")), "AC5: a decision's file isn't embedded"
    assert "client.py:42" in text(element(markup, "hyp-1-for")), "a cited source file isn't kept as text"


def the_answer_needs_more_than_its_confidence(base):
    repo, _ = investigation_branch(base, notes=INVESTIGATION_NOTES.replace(
        "Rounding runs twice: once per line and once on the total.\n\nConfidence: medium", "Confidence: medium"))
    _, markup = generate(repo, base)
    blockers = listed(markup, "blockers")
    assert len(blockers) == 1 and "The answer" in blockers[0] and "only its" in blockers[0], blockers
    write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Confidence: medium", "**Confidence:** **high**."))
    _, markup = generate(repo, base)
    assert listed(markup, "blockers") == [] and "high confidence" in summary_row(markup, "answer"), listed(markup, "blockers")
    write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Confidence: medium", "Confidence: " + "*" * 50_000 + "x"))
    before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    generate(repo, base)
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
    assert spent < 5, f"{spent:.1f} s of CPU for a long Confidence line"


def a_marker_inside_a_longer_fence_is_not_the_mode(base):
    repo, _ = investigation_branch(base)
    write(repo, "decision-notes", NOTES + "\n````\n```\nMode: investigation\n````\n")
    _, markup = generate(repo, base)
    assert "Decision page for" in text(markup), "AC1: a marker inside a four-backtick block switched the page"
    shown = sh(repo, REPORTS, "brief")
    assert shown.returncode == 0 and "mode: investigation" not in shown.stdout, shown


def a_kept_script_the_evidence_names_shows_once(base):
    repo, evidence = investigation_branch(base, rows=FINDINGS_ROWS.replace("[trace](trace.txt)", "`checks/F1.sh`"))
    put(os.path.join(evidence, "checks", "F1.py"), "print('ONLY-ONCE')\n")
    os.symlink("F1.py", os.path.join(evidence, "checks", "F1.sh"))
    _, markup = generate(repo, base)
    assert check_block(markup, "F1").count("ONLY-ONCE") == 1, "a script the row names and keeps shows twice"


for test in (writes_the_page_with_its_csp_and_prints_its_path, bounds_what_it_embeds,
             reads_nothing_outside_the_evidence_directory, a_long_last_line_keeps_its_tail,
             unmatched_brackets_take_linear_time, a_long_check_table_takes_linear_time, a_failed_generation_leaves_no_page, the_older_page_is_gone_while_it_builds,
             sections_come_in_order_with_the_notes, a_branch_without_notes_or_pr_body_says_not_recorded,
             falls_back_to_the_pr_body_first_paragraph, decide_names_every_item, a_symlink_to_a_file_inside_keeps_its_freshness, decide_keeps_what_later_sources_drop,
             ci_is_read_for_the_pr_head_only,
             no_pr_and_gh_failing_are_named, missing_or_malformed_reports_are_unavailable,
             each_check_row_carries_its_evidence, evidence_named_in_code_spans_and_old_headers,
             a_report_from_before_pr_a_says_not_recorded,
             every_review_entry_and_guide_step_in_order, hostile_text_renders_inert,
             ci_states_and_phases_off, stamps_verify_and_validation_states, prose_names_no_file_it_lacks,
             a_template_heading_is_not_the_first_paragraph, the_validation_notes_keep_their_code,
             the_skills_regenerate_and_link_the_page, refuses_another_branch_and_a_detached_head,
             the_summary_comes_first_in_order, the_summary_falls_back_to_the_spec_and_pr_body, the_three_lists_hold_their_items,
             sections_fold_unless_they_hold_a_failure, the_review_line_names_a_skipped_reviewer, the_tests_line_counts_each_level,
             levels_come_from_the_level_column, levels_fall_back_to_test_types, a_check_opens_to_steps_expected_actual_evidence,
             a_long_log_shows_its_tail, a_video_plays_inline, a_suite_output_lists_its_tests, the_review_shows_each_lens,
             a_review_without_lenses_says_not_recorded, merge_and_ship_reads_ship_md, a_partial_or_missing_ship_md,
             reports_are_annexes_without_built_from, the_skills_ask_for_the_new_formats,
             runner_lines_take_linear_time, hostile_text_in_the_new_parts_renders_inert,
             a_ship_md_marker_in_a_table_header_keeps_its_tags, unrun_optional_steps_are_decisions,
             a_failed_step_after_the_merge_is_a_blocker, test_types_heading_and_ran_in_any_form, levels_map_aliases_and_stay_bounded,
             a_shared_suite_output_counts_once, steps_past_the_total_are_linked, the_notes_count_toward_the_total,
             a_comment_before_the_first_paragraph_is_skipped, triage_lens_names_get_their_brief,
             coverage_without_an_ac_column_is_not_recorded, a_merge_order_item_is_not_the_merge_note,
             a_check_id_with_punctuation_gets_its_level, lens_names_split_in_linear_time,
             report_copies_past_the_total_leave_nothing_embedded, references_link_to_github_and_local_files,
             a_missing_verify_report_says_how_to_make_it, an_autolink_takes_linear_time, the_tests_follow_the_pyramid,
             a_level_without_checks_is_not_a_choice, the_coverage_shows_each_criterion_short, a_level_not_run_shows_once,
             the_review_line_counts_lenses_and_rounds_apart, a_merge_item_in_the_notes_is_the_recommendation,
             several_nits_show_one_per_line, references_link_only_what_the_pr_head_holds,
             switching_verify_off_raises_no_verify_blocker, the_summary_parsers_take_linear_time,
             open_findings_hold_no_link_inside_a_link, a_filename_git_holds_in_any_bytes_keeps_the_page,
             signals_show_the_latest_usable_matching_line, signals_show_the_structure_facts, signal_warnings_carry_the_need_tone,
             coverage_shows_a_percentage_only_when_measured, signals_without_a_line_say_why, red_checks_that_didnt_run_are_dim,
             signals_read_only_the_last_2_mb, an_investigation_is_chosen_by_the_marker_only,
             an_empty_diff_only_suggests_the_mode, the_investigation_page_shows_each_section,
             missing_investigation_sections_are_blockers, blocks_that_need_a_change_do_not_apply,
             findings_are_not_failures, the_notes_embed_their_evidence, brief_counts_findings,
             the_skills_describe_the_investigation_notes, a_marker_on_an_empty_diff_gives_the_investigation_page,
             the_investigation_page_keeps_its_csp_and_renders_hostile_text_inert, hypotheses_keep_their_lists_and_subheadings,
             the_notes_embed_each_file_under_its_first_part, the_answer_needs_more_than_its_confidence,
             a_marker_inside_a_longer_fence_is_not_the_mode, a_kept_script_the_evidence_names_shows_once):
    base = tempfile.mkdtemp(prefix="agents-test-decision-report-")
    try:
        test(os.path.realpath(base))
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {str(error)[:600]}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(os.path.expanduser("~/.agents/repos/shop"), ignore_errors=True)
        if os.path.isdir(QUALITY_LOG):
            os.rmdir(QUALITY_LOG)
        elif os.path.lexists(QUALITY_LOG):
            os.remove(QUALITY_LOG)

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
shutil.rmtree(os.environ["HOME"], ignore_errors=True)
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
