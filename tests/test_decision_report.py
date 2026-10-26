#!/usr/bin/env python3
"""bin/reports html: the branch's decision page, built from its spec, reports and evidence."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import html.parser
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.parse

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
# A HOME of our own: the page reads the user's phase switches, and the real ones would change it.
os.environ["HOME"] = tempfile.mkdtemp(prefix="agents-test-decision-report-home-")
os.makedirs(os.path.expanduser("~/.agents"))
for entry in set(os.listdir(KIT)) - {"approvals", "logs"}:
    os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
REPORTS = os.path.join(KIT, "bin", "reports")
RESULTS = []
CSP = ("default-src 'none'; img-src file: data:; media-src file: data:; style-src 'unsafe-inline'")
HEADINGS = ["What the task must do", "What the change does", "What it changes for users", "What we tested",
            "How to test it yourself", "What you must decide"]
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
    found = re.search(rf'<section id="s0{number}".*?</section>', page, re.S)
    assert found, f"no section 0{number}"
    return found.group(0)


def footer(page):
    found = re.search(r"<footer.*?</footer>", page, re.S)
    assert found, "no footer"
    return found.group(0)


def footer_value(page, name):
    found = re.search(rf"<dt>{name}</dt>\s*<dd[^>]*>(.*?)</dd>", footer(page), re.S)
    assert found, f"no {name} in the footer"
    return text(found.group(1))


def check_block(page, check_id):
    found = re.search(rf'<li class="check[^"]*" id="check-{check_id}">.*?</li>', page, re.S)
    assert found, f"no block for {check_id}"
    return found.group(0)


def decide_items(page):
    found = re.search(r'<ol class="decide">(.*?)</ol>', section(page, 6), re.S)
    assert found, "no list of items in 06"
    return [text(item) for item in re.findall(r"<li[^>]*>(.*?)</li>", found.group(1), re.S)]


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
            assert not (name in ("href", "src", "action", "poster", "srcset") and re.match(r"\s*(https?:)?//", value or "")), \
                (tag, name, value)
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
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Bounds\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B90 | AC1 | big output | + | all rows | PASS | [output]({evidence}/big.txt) |\n"
                 f"| B91 | AC1 | huge image | + | an image | PASS | [img]({evidence}/huge.png) |\n" + rows)
    _, markup = generate(repo, base)
    block = check_block(markup, "B90")
    assert "FIRST LINE" in block and "LAST LINE" in block, "the head or tail of an oversize output is gone"
    assert "middle 0010000" not in markup, "an oversize output was embedded whole"
    assert re.search(r"\bcut\b", text(block)), text(block)[:400]
    assert f'href="file://{urllib.parse.quote(os.path.realpath(evidence))}/big.txt"' in block, "no link to the full output"
    huge = check_block(markup, "B91")
    assert "data:image" not in huge and f"/huge.png\"" in huge, huge[:600]
    embedded = [n for n in range(11) if "data:image/png" in check_block(markup, f"B{n}")]
    linked = [n for n in range(11) if n not in embedded and f"/img{n:02d}.png\"" in check_block(markup, f"B{n}")]
    assert embedded and linked and embedded == list(range(len(embedded))) and len(embedded) + len(linked) == 11, (embedded, linked)
    assert len(embedded) * 1900 * 1024 <= 20 * 1024 * 1024, embedded


def reads_nothing_outside_the_evidence_directory(base):
    repo, evidence = full_branch(base)
    outside = os.path.join(base, "secret.txt")
    put(outside, "SECRET-TOKEN-1\n")
    put(os.path.join(base, "secret2.txt"), "SECRET-TOKEN-2\n")
    os.symlink(outside, os.path.join(evidence, "link.txt"))
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## C. Scope\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| C1 | AC1 | through a symlink | − | nothing read | PASS | [out]({evidence}/link.txt) |\n"
                 f"| C2 | AC1 | an absolute path | − | nothing read | PASS | [out]({base}/secret2.txt) and {base}/secret2.txt |\n")
    _, markup = generate(repo, base)
    assert "SECRET-TOKEN" not in markup, "a file outside the evidence directory was read"
    assert f"{base}/secret2.txt" in text(check_block(markup, "C2")), "the outside path isn't shown as text"
    assert "file://" + urllib.parse.quote(os.path.realpath(outside)) not in markup


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


# AC2

def sections_come_in_order_with_the_notes(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    found = [(n, h) for n, h in re.findall(r'<h2[^>]*><span class="num">(\d\d)</span>\s*([^<]+)</h2>', markup)]
    assert found == [(f"0{i + 1}", h) for i, h in enumerate(HEADINGS)], found
    for number, line in ((1, "The task must stop a second discount"), (2, "The change adds a guard in"),
                         (3, "A shopper sees one discount line"), (6, "Decide if one FAIL on the phone check")):
        part = section(markup, number)
        assert line in text(part), (number, text(part)[:300])
        assert "written by the agent" in text(part), number
        assert path_of(repo, "decision-notes") in text(part), number
    assert re.search(r"<pre[^>]*>(?:<code[^>]*>)?[^<]*└── apply_coupon", section(markup, 2)), "the notes' figure isn't preformatted"
    one = section(markup, 1)
    criteria = re.findall(r'<tr id="ac-(\d+)">(.*?)</tr>', one, re.S)
    assert [n for n, _ in criteria] == ["1", "2", "3"], criteria
    first = text(criteria[0][1])
    assert "A1 PASS" in first and "A2 FAIL" in first and "A3" not in first, first
    assert "no check" in text(criteria[2][1]) and "no check" not in first, criteria
    assert "Goal: a shopper gets one discount per coupon" in text(one) or "a shopper gets one discount per coupon" in text(one)
    assert path_of(repo, "spec") in text(one)
    two = text(section(markup, 2))
    assert "Second paragraph with detail." in two and path_of(repo, "pr-body") in two, two[:500]
    assert "src/ 1 +3 -1" in two and "(top level) 1 +1 -0" in two, two
    four = text(section(markup, 4))
    for kind in ("validation", "review", "verify"):
        assert path_of(repo, kind) in four, kind
    assert path_of(repo, "checks-guide") in text(section(markup, 5))
    listing = sh(repo, REPORTS)
    assert "decision-notes" not in listing.stdout, "the notes are listed with the reports"


def a_branch_without_notes_or_pr_body_says_not_recorded(base):
    repo, _ = full_branch(base, notes=False, pr_body=False)
    _, markup = generate(repo, base)
    for number in (1, 2, 3, 6):
        assert "not recorded" in text(section(markup, number)), number
        assert "written by the agent" not in text(section(markup, number)), number
    assert "a shopper gets one discount per coupon" in text(section(markup, 1)), "no fallback to the spec's goal"
    two = text(section(markup, 2))
    assert "kit-ship" in two, "doesn't say what would write the missing PR body"


def falls_back_to_the_pr_body_first_paragraph(base):
    repo, _ = full_branch(base, notes=False)
    _, markup = generate(repo, base)
    two = text(section(markup, 2))
    assert "not recorded" in two and "The cart applies a coupon once." in two, two[:400]


# AC3

def decide_names_every_item(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    items = decide_items(markup)
    joined = "\n".join(items)
    for name in ("totals in two currencies collapse", "A2", "A3", "A2.txt", "environment.txt", "gone.txt", "M1", "P1", "P2",
                 "c123"):
        assert name in joined, (name, items)
    assert not any(re.search(r"\bM2\b", item) for item in items), items
    assert any("M1" in item and "optional" in item for item in items), items
    assert any("P1" in item and "left" in item for item in items), items
    assert any("environment.txt" in item and "doesn't have" in item for item in items), items
    assert any("A2.txt" in item and "src/cart.py" in item for item in items), items
    six = text(section(markup, 6))
    for count in ("Fixed 2", "Rejected 1", "Pre-existing 1", "Open 1", "Not run 1", "PASS 2", "FAIL 1", "NOT RUN 1"):
        assert count in six, (count, six[:900])
    assert "Goal: a shopper gets one discount per coupon, even on refresh." in six
    assert "The cart applies a coupon once. A refresh no longer adds a second discount." in six
    assert "review missing" in six and "validate missing" in six, six[:900]
    assert "PASS" in six and "tests/run.sh cart" in six
    links = re.findall(r'<ol class="decide">.*?</ol>', section(markup, 6), re.S)[0]
    for target in re.findall(r'href="#([^"]+)"', links):
        assert f'id="{target}"' in markup, target
    assert 'href="#check-A2"' in links and 'href="#s06"' in markup


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
    assert "Apply a coupon once" in text(section(markup, 2))
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
    six = text(section(markup, 6))
    assert re.search(r"Validation: unavailable", six) and re.search(r"Review: unavailable", six), six[:900]
    assert not re.search(r"\b(PASS|FAIL|NOT RUN|Fixed|Open) 0\b", six), six[:900]
    items = "\n".join(decide_items(markup))
    assert "validation" in items.lower() and "unavailable" in items, items


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
    assert "php 8.3" in text(section(markup, 4)), "environment.txt isn't shown"
    assert "missing" in text(check_block(markup, "A4")), text(check_block(markup, "A4"))
    assert "missing" not in text(block)


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
    four = text(section(markup, 4))
    assert re.search(r"Environment:? not recorded", four), four[:900]


# AC5

def every_review_entry_and_guide_step_in_order(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    four = section(markup, 4)
    for outcome, entry, where in (("Fixed", "a coupon applied twice on refresh", "src/cart.py:40"),
                                  ("Fixed", "the total ignored tax", "src/cart.py:52"),
                                  ("Rejected", "a race on checkout", "handled at src/cart.py:80"),
                                  ("Pre-existing", "the log grows without a bound", "src/log.py:9"),
                                  ("Open", "totals in two currencies collapse", "src/cart.py:77"),
                                  ("Not run", "security lens", "no auth code touched")):
        group = re.search(rf'<div class="outcome" id="review-{outcome.lower().replace(" ", "-")}">(.*?)</div>', four, re.S)
        assert group and entry in text(group.group(1)) and where in text(group.group(1)), (outcome, entry)
    assert "codex on correctness" in text(four) and "re-check rounds 1" in text(four).replace(":", ""), text(four)[:600]
    five = section(markup, 5)
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
    _, markup = generate(repo, base)
    assert "<script" not in markup.lower(), "a script tag reached the page"
    for tag, attrs in tags(markup):
        assert tag not in ("script", "iframe", "object", "embed", "base", "form"), tag
        for name, value in attrs:
            assert not name.startswith("on"), (tag, name, value)
            assert not (value or "").strip().lower().startswith(("javascript:", "http:", "https:")), (tag, name, value)
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


for test in (writes_the_page_with_its_csp_and_prints_its_path, bounds_what_it_embeds,
             reads_nothing_outside_the_evidence_directory, a_failed_generation_leaves_no_page,
             sections_come_in_order_with_the_notes, a_branch_without_notes_or_pr_body_says_not_recorded,
             falls_back_to_the_pr_body_first_paragraph, decide_names_every_item, ci_is_read_for_the_pr_head_only,
             no_pr_and_gh_failing_are_named, missing_or_malformed_reports_are_unavailable,
             each_check_row_carries_its_evidence, a_report_from_before_pr_a_says_not_recorded,
             every_review_entry_and_guide_step_in_order, hostile_text_renders_inert,
             the_skills_regenerate_and_link_the_page, refuses_another_branch_and_a_detached_head):
    base = tempfile.mkdtemp(prefix="agents-test-decision-report-")
    try:
        test(os.path.realpath(base))
        RESULTS.append((test.__name__, None))
    except Exception as error:  # noqa: BLE001 - report every failure, keep running the rest
        RESULTS.append((test.__name__, f"{type(error).__name__}: {str(error)[:600]}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

for name, error in RESULTS:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
shutil.rmtree(os.environ["HOME"], ignore_errors=True)
raise SystemExit(1 if any(error for _, error in RESULTS) else 0)
