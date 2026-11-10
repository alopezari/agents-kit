#!/usr/bin/env python3
"""bin/reports html: the branch's decision page, built from its spec, reports and evidence."""
import kit_home  # noqa: F401  (first: refuses to test another checkout)
import base64
import concurrent.futures
import datetime
import html
import html.parser
import json
import multiprocessing
import os
import re
import resource
import shutil
import subprocess
import tempfile
import urllib.parse

KIT = os.path.realpath(os.path.expanduser("~/.agents"))
SUITE_DIR = tempfile.mkdtemp(prefix="agents-test-decision-report-home-")


def make_own_home():
    """A HOME of our own: the page reads the user's phase switches, and the real ones would change it. One per worker,
    as the tests write to the same places under it (the quality log, a repo's ship.md)."""
    global QUALITY_LOG
    os.environ["HOME"] = tempfile.mkdtemp(dir=SUITE_DIR)
    os.makedirs(os.path.expanduser("~/.agents"))
    for entry in set(os.listdir(KIT)) - {"approvals", "logs", "repos"}:
        os.symlink(os.path.join(KIT, entry), os.path.expanduser(f"~/.agents/{entry}"))
    os.makedirs(os.path.expanduser("~/.agents/repos"))  # a ship.md written here must not land in the kit's own overlays
    QUALITY_LOG = os.path.expanduser("~/.agents/logs/quality.jsonl")  # quality_log() under the suite: this HOME's


REPORTS = os.path.join(KIT, "bin", "reports")
CSP = ("default-src 'none'; img-src file: data:; media-src file:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
CHANGE_PARTS = ["problem", "fix", "testing", "review", "limits", "decide"]
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
if args[:2] == ["api", "graphql"] and "no_ci" in state:
    print(json.dumps({"data": {"repository": state["no_ci"]}}))
    sys.exit(0)
if args[:1] == ["api"] and found:
    key = "check_runs" if found.group(2) == "check-runs" else "statuses"
    for item in state.get("checks", {}).get(found.group(1), {}).get(key, []):
        print(json.dumps(item))
    sys.exit(0)
print("fake gh: unexpected " + " ".join(args), file=sys.stderr)
sys.exit(2)
'''


def sh(cwd, *cmd, env=None):
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env, timeout=300)  # a hung child would hold the whole pool with no output


def in_order(where, *wanted):
    """Each text is there, and after the one before it: comparing bare .find() results passes when one is missing."""
    at = -1
    for part in wanted:
        at = where.find(part, at + 1)
        assert at >= 0, f"{part!r} missing, or out of order, in {wanted}"


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


def footer_fold(page, title):
    """One of the change page's footer folds, up to the next one or the footer's own lists."""
    found = re.search(rf"<details><summary>{title}</summary>.*?(?=<details><summary>|<dl class=\"sum\">)", footer(page), re.S)
    assert found, f"no {title} in the footer"
    return found.group(0)


def section(page, number):
    """A section by the number it has on the investigation page, or by the part that took over on the change page: the
    tests and the coverage are Testing, the review is Code review, then the footer's merge steps and its reports."""
    found = re.search(rf'<details class="sec"[^>]* id="s0{number}".*?(?=<details class="sec"|</main>)', page, re.S)
    if found:
        return found.group(0)
    if number in (1, 2):
        return page_part(page, "testing")
    if number == 3:
        return page_part(page, "review")
    return footer_fold(page, {4: "Merge, ship and roll back", 5: "Full reports"}[number])


def summary(page):
    """The investigation page's summary, or the change page's quality signals."""
    found = re.search(r'<section id="summary".*?</section>', page, re.S) or re.search(r'<dl class="sum">.*?</dl>', page, re.S)
    assert found, "no summary"
    return found.group(0)


def summary_row_markup(page, name):
    found = re.search(rf'<div class="row"><dt id="sum-{name}">(.*?)(?=<div class="row">|</dl>)', summary(page), re.S)
    assert found, f"no {name} row in the summary"
    return found.group(1)


def summary_row(page, name):
    return text(summary_row_markup(page, name))


def verdict_items(page, list_id):
    found = re.search(rf'<ul id="{list_id}">(.*?)</ul>', page, re.S)
    return [text(item) for item in re.findall(r"<li>(.*?)</li>", found.group(1), re.S)] if found else []


def listed(page, name):
    """The items of one of the investigation page's summary lists, or on the change page what took over from it: blockers
    are what stops the merge, decisions what needs the owner's call and the decisions they own, warnings the rest."""
    if '<header class="hero">' in page:
        if name == "blockers":
            return verdict_items(page, "stops")
        if name == "warnings":
            return verdict_items(page, "notes")
        return verdict_items(page, "calls") + [text(found) for found in re.findall(r'<article class="decision">(.*?)</article>', page, re.S)]
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
    investigated = re.search(rf'<li class="check[^"]*" id="check-{check_id}">.*?</details></li>', page, re.S)
    if investigated:
        return investigated.group(0)
    anchor = page.find(f'id="check-{check_id}"')
    start = page.rfind('<details class="check', 0, anchor)
    assert anchor >= 0 and start >= 0, f"no block for {check_id}"
    end = re.compile(r'<details class="(?:check|more|sec)|<h3|<h4|<footer').search(page, anchor)
    return page[start:end.start()]


def parts_of(page):
    """The change page's parts, in the order they come."""
    return re.findall(r'<div class="body" id="([\w-]+)"', page)


def merge_note(page):
    return text(re.search(r'<p id="merge-note">(.*?)</p>', page, re.S).group(1))


def annex(page, kind):
    """(the annex's summary, the whole annex) in the footer's Full reports."""
    found = re.search(rf'<details class="annex"><summary>((?:(?!</summary>).)*)</summary>\s*<div class="annex-body" id="annex-{kind}".*?</details>', page, re.S)
    assert found, f"no {kind} annex"
    return found.group(1), found.group(0)


def layer_row(page, name):
    """One layer's row of the test map, as text."""
    found = re.search(rf'<div class="row"><span class="lay">{name}</span><div class="chips">(.*?)</div></div>', page, re.S)
    assert found, f"no {name} row in the test map"
    return text(found.group(1))


def layer_ids(page, name):
    """The ids of the checks on one layer's row of the test map, in order."""
    found = re.search(rf'<div class="row"><span class="lay">{name}</span><div class="chips">(.*?)</div></div>', page, re.S)
    assert found, f"no {name} row in the test map"
    return re.findall(r'<span class="id">(.*?)</span>', found.group(1))


def tally_of(page):
    return text(re.search(r'<p class="tally">.*?</p>', page, re.S).group(0))


def rail_links(page):
    return re.findall(r'href="#([\w-]+)"', re.search(r'<ol class="rail".*?</ol>', page, re.S).group(0))


def headline(page, part):
    """The headline of the problem or the solution card."""
    return text(re.search(r'<p class="hl">(.*?)</p>', page_part(page, part), re.S).group(1))


def layer_groups(page):
    """The layers the tests run for real are grouped under."""
    return re.findall(r'<h4 id="level-([^"]+)">', page_part(page, "testing"))


def layer_chip(page, check_id):
    """The layer next to a test's name."""
    found = re.search(rf'<span class="cid">{re.escape(check_id)}</span>.*?<span class="chip[^"]*">(.*?)</span>', page, re.S)
    assert found, f"no chip on {check_id}"
    return text(found.group(1))


def review_facts(page):
    found = re.search(r'<dl class="facts"><dt>Who read it.*?</dl>', page_part(page, "review"), re.S)
    assert found, "no review facts"
    return found.group(0)


def lens_rows(page):
    table = re.search(r'aria-label="Table: Lens, What it checks[^"]*"><table class="lenses">.*?<tbody>(.*?)</tbody>', page_part(page, "review"), re.S)
    assert table, "no lens table"
    return [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)] for r in re.findall(r"<tr>(.*?)</tr>", table.group(1), re.S)]


def decide_items(page):
    """Every item of the verdict's lists, and the decisions."""
    return listed(page, "decisions") + listed(page, "blockers") + listed(page, "warnings")


class Tags(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))


def evidence_tab(attrs):
    """Whether a tag's attributes are a check's Video or Log tab radio, which switches evidence and moves nothing."""
    return dict(attrs).get("class") in ("t-video", "t-log")


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
    assert parts_of(markup) == CHANGE_PARTS, parts_of(markup)
    problem, fix = page_part(markup, "problem"), page_part(markup, "fix")
    assert "The task must stop a second discount" in text(problem) and "Written by the agent" in text(problem), text(problem)[:300]
    for line in ("The change adds a guard in", "A shopper sees one discount line"):
        assert line in text(fix), (line, text(fix)[:300])
    assert "Written by the agent" in text(fix)
    assert any("Decide if one FAIL on the phone check" in d for d in listed(markup, "decisions"))
    assert path_of(repo, "decision-notes") in text(section(markup, 5))
    assert re.search(r"<h4[^>]*>Where it sits</h4>", fix), "a notes heading skips a level"
    levels = [int(n) for n in re.findall(r"<h(\d)", fix)]
    assert all(b <= a + 1 for a, b in zip(levels, levels[1:])), f"a notes heading skips a level: {levels}"
    assert re.search(r'<pre[^>]*>(?:<code[^>]*>)?(?:[^<]|<span class="ln">|</span>)*└── apply_coupon', fix), "the notes' figure isn't preformatted"
    one = section(markup, 1)
    criteria = re.findall(r'<tr[^>]*id="ac-(\d+)">(.*?)</tr>', one, re.S)
    assert [n for n, _ in criteria] == ["1", "2", "3"], criteria
    first = text(criteria[0][1])
    assert "Passed Coupon applied once A1" in first and "Failed Tampered total refused A2" in first and "A3" not in first, first
    assert "No check" in text(criteria[2][1]) and "No check" not in first, criteria
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
    for part in ("problem", "fix"):
        assert "Not recorded" in text(page_part(markup, part)), part
    assert "Written by the agent" not in text(markup)
    assert 'id="merge-note"' not in markup, "the verdict is the page's answer: a missing recommendation is no gap to report"
    assert "a shopper gets one discount per coupon" in text(page_part(markup, "problem")), "no fallback to the spec's goal"
    assert "a shopper gets one discount per coupon" in text(re.search(r'<p class="lead">.*?</p>', markup, re.S).group(0))
    for kind in ("decision-notes", "pr-body"):
        words = text(annex(markup, kind)[1])
        assert "not recorded" in words and "kit-ship" in words, kind


def falls_back_to_the_pr_body_first_paragraph(base):
    repo, _ = full_branch(base, notes=False)
    _, markup = generate(repo, base)
    solution = text(page_part(markup, "fix"))
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
    assert any("M1" in item and "failed" in item for item in listed(markup, "blockers")), items
    assert any("environment.txt" in item and "doesn't have" in item for item in items), items
    assert any("A2.txt" in item and "src/cart.py" in item for item in items), items
    after = text(section(markup, 4))
    assert re.search(r"P1 Backfill the totals: left", after) and re.search(r"P2 Watch the error log: left", after), after[:900]
    review = text(section(markup, 3))
    assert "2 fixed, 1 rejected, 1 open, 1 already there" in review, review[:600]
    assert "security lens" in text(re.search(r'id="review-not-run">(.*?)</dd>', markup, re.S).group(1))
    assert tally_of(markup).startswith("4 checks in the validation report: 2 passed, 1 failed, 1 did not run."), tally_of(markup)
    stamps = footer_value(markup, "Stamps")
    assert "review stamp missing" in stamps and "validate stamp missing" in stamps, stamps
    assert footer_value(markup, "Verify").startswith("PASS"), footer_value(markup, "Verify")
    assert "tests/run.sh cart" in footer_value(markup, "Verify")
    links = re.findall(r'<ul id="(?:stops|calls|notes)">.*?</ul>', markup, re.S)
    for target in re.findall(r'href="#([^"]+)"', "".join(links)):
        assert f'id="{target}"' in markup, target
    assert 'href="#check-A2"' in "".join(links) and 'href="#decide"' in markup


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
    assert re.search(r"missing.*environment\.txt", joined), joined
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
    assert any("CI couldn't be read" in item and "error connecting to api.github.com" in item for item in decide_items(markup)), decide_items(markup)


def missing_or_malformed_reports_are_unavailable(base):
    repo, _ = full_branch(base)
    os.remove(path_of(repo, "validation"))
    write(repo, "review", "some notes, nothing a review writes\n")
    _, markup = generate(repo, base)
    tests, review = text(page_part(markup, "testing")), text(page_part(markup, "review"))
    assert "unavailable" in tests and "unavailable" in review, (tests[:300], review[:300])
    assert not re.search(r"\b(PASS|FAIL|NOT RUN|Fixed|Open) 0\b", tests + review), (tests[:300], review[:300])
    items = "\n".join(listed(markup, "blockers"))
    assert "the validation is missing" in items and "has no Review line" in items, items
    for verdict in ("   ", " ."):
        write(repo, "verify", f"# Verify:{verdict}\n")
        _, markup = generate(repo, base)
        assert "unavailable" in footer_value(markup, "Verify"), f"verdict {verdict!r}"


def ci_states_and_phases_off(base):
    repo, _ = full_branch(base)
    head = git(repo, "rev-parse", "HEAD")
    pr = {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Apply a coupon once", "headRefOid": head,
          "state": "OPEN"}
    run = lambda **fields: {"check_runs": [{"name": "suite", "details_url": "", **fields}]}
    for checks, word, phrase in ((run(status="completed", conclusion="failure"), "FAIL", "CI failed"),
                                 (run(status="in_progress", conclusion=None), "pending", "CI is still running")):
        _, markup = generate(repo, base, {"pr": pr, "checks": {head: checks}})
        assert footer_value(markup, "CI").startswith(word), footer_value(markup, "CI")
        assert any(phrase in item for item in listed(markup, "blockers")), listed(markup, "blockers")
    _, markup = generate(repo, base, {"pr": pr, "api_error": True})
    ci = footer_value(markup, "CI")
    assert "unknown" in ci and "bad gateway" in ci, ci
    for phase in ("ci", "verify", "review"):
        out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", phase)
        assert out.returncode == 0, out
    _, markup = generate(repo, base, {"pr": pr, "checks": {head: run(status="completed", conclusion="failure")}})
    assert footer_value(markup, "CI").startswith("CI phase off"), footer_value(markup, "CI")
    items = "\n".join(decide_items(markup))
    assert "CI failed" not in items and "Verify" not in items and "totals in two currencies" not in items, items
    assert "review stamp phase off" in footer_value(markup, "Stamps") and "phase off" in footer_value(markup, "Verify")
    assert re.search(r"\bci\b", footer_value(markup, "Phases off")), footer_value(markup, "Phases off")


def stamps_verify_and_validation_states(base):
    repo, evidence = full_branch(base)
    stamp = os.path.join(KIT, "hooks", "review_stamp.py")
    assert sh(repo, "python3", stamp, "write").returncode == 0
    _, markup = generate(repo, base)
    assert "review stamp current" in footer_value(markup, "Stamps"), footer_value(markup, "Stamps")
    assert not any("review stamp" in item for item in decide_items(markup))
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nc = 3\n")
    write(repo, "verify", "# Verify: PASS, but nothing was checked\n\n2026-10-26 · verify in shop\n")
    _, markup = generate(repo, base)
    assert "review stamp stale" in footer_value(markup, "Stamps"), footer_value(markup, "Stamps")
    items = "\n".join(decide_items(markup))
    assert "The review stamp reads stale" in items and "checked nothing" in items, items
    assert footer_value(markup, "Verify").startswith("checked nothing"), footer_value(markup, "Verify")
    git(repo, "checkout", "main", "--", "src/cart.py")
    git(repo, "commit", "-qam", "docs only")
    os.remove(path_of(repo, "validation"))
    _, markup = generate(repo, base)
    assert "validate stamp not needed" in footer_value(markup, "Stamps"), footer_value(markup, "Stamps")
    items = "\n".join(decide_items(markup))
    assert "the validation" not in items and "validate stamp" not in items, items


def prose_names_no_file_it_lacks(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## E. Prose\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 "| E1 | AC1 | prose evidence | + | a 200 | PASS | took ~3s, README.md updated, GET /api/cart 200; see A1.txt and run.webm |\n")
    _, markup = generate(repo, base)
    block = text(check_block(markup, "E1"))
    assert "missing" not in block and "not read" not in block, block
    assert "A1.txt" in block and ("ok 12 rows" in block or "shown above" in block), "a file the prose names and the evidence directory has wasn't shown"
    assert not any(item.startswith("E1") for item in decide_items(markup)), decide_items(markup)


def a_template_heading_is_not_the_first_paragraph(base):
    repo, _ = full_branch(base, notes=False)
    write(repo, "pr-body", "## Summary\n<!-- what it does -->\n\nThe cart applies a coupon once.\n\nSecond paragraph.\n")
    _, markup = generate(repo, base)
    assert headline(markup, "fix") == "The cart applies a coupon once.", headline(markup, "fix")
    write(repo, "pr-body", "## Summary\n\n#123 is fixed here.\nMore.\n")
    _, markup = generate(repo, base)
    assert headline(markup, "fix") == "#123 is fixed here. More.", "an issue reference was taken for a heading"


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
    for part in ("A1", "Coupon applied once", "proves A coupon applies once.", "one discount line", "one line seen", "Passed", "make test-cart",
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
    assert "Not recorded" in block and "missing" not in block, block
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
        shape = {"Open": r'id="review-open".*?</table>', "Not run": r'id="review-not-run">.*?</dd>'}.get(outcome, rf'id="review-{outcome.lower()}">.*?</details>')
        group = re.search(shape, four, re.S)
        assert group and entry in text(group.group(0)) and where in text(group.group(0)), (outcome, entry)
    assert "codex on correctness" in text(four) and "1 re-check round" in text(four), text(four)[:600]
    five = annex(markup, "checks-guide")[1]
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
    for skill, needles in (("kit-ship", ("reports html", "decision-notes", "linked by its absolute path", "don't repeat its tables",
                                  "the error", "each verdict and its counts", "no older file linked as current",
                                  "from a checkout of the PR's head branch", "before `~/.agents/bin/prune-merged` removes it")),
                           ("kit-validate", ("names that checkout: the PR's head branch, never the default branch",)),
                           ("kit-follow-pr", ("reports html", "link the page by its absolute path", "don't repeat its tables", "the error",
                                       "each verdict and its counts", "link no older file as current"))):
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
    assert main.index('<header class="hero"') < main.index('<section class="sec"'), "a section comes before the hero"
    assert list(rail_of(markup)) == ["Spec", "Review", "Validate", "CI", "Your call"], rail_of(markup)
    assert re.findall(r'<ul id="(stops|calls|notes)">', markup) == ["stops", "calls", "notes"]
    assert headline(markup, "problem") == "The task must stop a second discount for one coupon.", headline(markup, "problem")
    fix = text(page_part(markup, "fix"))
    assert "The change adds a guard in src/cart.py:2" in fix, fix[:400]
    assert "Where it sits" in fix and "A shopper sees one discount line" in fix, "the rest of the notes isn't under the solution"
    assert "Written by the agent" in fix
    for href in rail_links(markup):
        assert f'id="{href}"' in markup, href
    assert parts_of(markup) == CHANGE_PARTS, parts_of(markup)


def the_summary_falls_back_to_the_spec_and_pr_body(base):
    repo, _ = full_branch(base, notes=False)
    _, markup = generate(repo, base)
    problem = text(page_part(markup, "problem"))
    assert "a shopper gets one discount per coupon, even on refresh." in problem, problem[:300]
    solution = text(page_part(markup, "fix"))
    assert "The cart applies a coupon once." in solution and "Second paragraph" not in solution, solution[:300]
    assert "Written by the agent" not in text(markup)


def the_three_lists_hold_their_items(base):
    repo, _ = full_branch(base)
    assert sh(repo, "python3", os.path.join(KIT, "hooks", "review_stamp.py"), "write").returncode == 0
    put(os.path.join(repo, "src", "cart.py"), "a = 1\nc = 3\n")  # the review stamp goes stale
    _, markup = generate(repo, base)
    decisions, blockers, warnings = (listed(markup, name) for name in ("decisions", "blockers", "warnings"))
    assert 'id="merge-note"' not in markup, "the verdict is the page's answer: a missing recommendation is no gap to report"
    assert any("Decide if one FAIL on the phone check blocks the merge." in d for d in decisions), decisions
    assert any("totals in two currencies collapse" in d for d in decisions), decisions
    assert not any("M1" in d or "M2" in d for d in decisions), "a guide step with a result is left as a decision"
    assert any("A2" in b and "failed" in b for b in blockers), blockers
    assert any("review stamp" in b and "stale" in b for b in blockers), blockers
    assert any("gone.txt" in d and "missing" in d for d in decisions), decisions
    assert any("M1" in b and "failed" in b for b in blockers), blockers
    assert any("environment.txt" in w for w in warnings), warnings
    assert not any("environment.txt" in b for b in blockers), blockers
    assert any(re.search(r"\bA3\b", d) and "did not run" in d for d in decisions), "a NOT RUN check isn't the owner's call"
    assert not any(re.search(r"\bA3\b", item) for item in blockers + warnings), "a NOT RUN check is listed twice"
    assert footer_value(markup, "CI").startswith("no PR yet"), footer_value(markup, "CI")
    assert "review stamp stale, validate stamp missing" in footer_value(markup, "Stamps").replace(" ,", ","), footer_value(markup, "Stamps")
    write(repo, "decision-notes", NOTES.replace("Decide if one FAIL on the phone check blocks the merge.",
                                                "- Merge: recommend yes once A2 is fixed; waiting costs a day.\n"
                                                "- Keep the phone check: A to keep, B to drop; recommend A."))
    _, markup = generate(repo, base)
    decisions = listed(markup, "decisions")
    assert "recommend yes once A2 is fixed" in merge_note(markup), merge_note(markup)
    assert not any("recommend yes once A2" in d for d in decisions), decisions
    assert any("Keep the phone check" in d for d in decisions), decisions
    write(repo, "review", REVIEW + "Open:      a second open point — src/cart.py:80 — decision for the human\n"
                                   "Open:      a third open point — src/cart.py:90 — decision for the human\n")
    write(repo, "checks-guide", GUIDE.replace("## Before the merge (optional)\n",
                                              "## Before the merge (optional)\n\n### M0. What you need\n- **Where:** you\n"))
    _, markup = generate(repo, base)
    decisions = listed(markup, "decisions")
    without = [d for d in decisions if "no recorded impact" in d]
    assert len(without) == 1 and all(point in without[0] for point in ("totals in two currencies", "a second open point", "a third open point")), decisions
    assert not any(re.search(r"\bM0\b", d) for d in decisions), decisions
    assert any("Keep the phone check" in d for d in decisions), decisions


def a_failure_is_never_folded_away(base):
    repo, evidence = full_branch(base)
    _, markup = generate(repo, base)
    for part in parts_of(markup):
        assert all("open" in fold for fold in enclosing_folds(markup, f'<div class="body" id="{part}"')), part
    assert "open" in enclosing_folds(markup, 'id="check-A2"')[-1], "a FAIL check is folded away"
    assert "open" not in enclosing_folds(markup, 'id="check-A1"')[-1], "a passing check isn't folded"
    write(repo, "validation", f"""# Validation

| # | AC | Check | Case | Expected | Result | Evidence |
|---|---|---|---|---|---|---|
| A1 | AC1, AC2, AC3 | Coupon applied once | + | one line | FAIL | [output]({evidence}/A1.txt) |
""")
    _, markup = generate(repo, base)
    assert "open" in enclosing_folds(markup, 'id="check-A1"')[-1], "a lone FAIL check is folded away"
    assert [b for b in listed(markup, "blockers") if "A1" in b], listed(markup, "blockers")
    os.remove(path_of(repo, "review"))
    _, markup = generate(repo, base)
    assert all("open" in fold for fold in enclosing_folds(markup, '<div class="body" id="review"')), "the review is unavailable and its section is closed"
    assert any("the review is missing" in b for b in listed(markup, "blockers")), listed(markup, "blockers")
    review = text(page_part(markup, "review"))
    assert ("unavailable" in review or "not recorded" in review) and not re.search(r"Fixed 0|Open 0|0 fixed", review), review[:400]


def the_review_line_names_a_skipped_reviewer(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + LENSES)
    _, markup = generate(repo, base)
    review = text(section(markup, 3))
    for part in ("2 lenses", "codex on correctness", "2 fixed", "1 open"):
        assert part in review, (part, review[:600])
    not_run = text(re.search(r'id="review-not-run">(.*?)</dd>', markup, re.S).group(1))
    for part in ("codex Maintainability", "exit 1", "security lens"):
        assert part in not_run, (part, not_run)


def the_tests_line_counts_each_level(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", report.replace("| unit | yes | [A1]({evidence}/A1.txt) |".format(evidence=evidence),
                                             f"| unit | yes | [A1]({evidence}/A1.txt) |\n| a11y | no | no screen reader here |"))
    _, markup = generate(repo, base)
    assert layer_ids(markup, "Unit") == ["A1"], layer_row(markup, "Unit")
    assert layer_ids(markup, "Layer not recorded") == ["A2", "A3", "A4"], layer_row(markup, "Layer not recorded")
    assert tally_of(markup).startswith("4 checks in the validation report: 2 passed, 1 failed, 1 did not run."), tally_of(markup)
    assert "Not run a11y tests: no screen reader here" in layer_row(markup, "Accessibility"), layer_row(markup, "Accessibility")
    os.remove(path_of(repo, "validation"))
    _, markup = generate(repo, base)
    testing = text(section(markup, 2))
    assert ("unavailable" in testing or "not recorded" in testing) and not re.search(r"\b0/0\b|\b0 FAIL", testing), testing[:400]


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
    head = re.search(r'<table class="grid-impact stack matrix"><thead>(.*?)</thead>', one, re.S)
    assert head and text(head.group(1)) == "Criterion How it is checked Run for real Test suite Bugs planted", head and text(head.group(1))
    rows = {n: body for n, body in re.findall(r'<tr[^>]*id="ac-(\d+)">(.*?)</tr>', one, re.S)}
    cells = lambda n: [text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", rows[n], re.S)]
    assert cells("1")[1:] == ["Failed Tampered total refused A2", "Passed Coupon applied once A1", "None"], cells("1")
    assert cells("2")[1:] == ["Failed Tampered total refused A2", "None", "None"], cells("2")
    assert 'href="#check-A1"' in rows["1"] and 'href="#check-A2"' in rows["1"], rows["1"]
    assert "No check" in text(rows["3"]), rows["3"]
    assert any("no check proves “The receipt names the coupon”" in d for d in listed(markup, "decisions")), listed(markup, "decisions")
    two = section(markup, 2)
    assert layer_groups(markup) == ["e2e"], layer_groups(markup)
    assert two.index('id="check-A1"') < two.index('id="manual"') < two.index('id="level-e2e"') < two.index('id="check-A2"')
    assert layer_chip(markup, "A1") == "Unit" and layer_chip(markup, "A2") == "End to end", (layer_chip(markup, "A1"), layer_chip(markup, "A2"))


def levels_fall_back_to_test_types(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    two = section(markup, 2)
    assert layer_groups(markup) == ["not-recorded"], layer_groups(markup)
    assert two.index('id="check-A1"') < two.index('id="level-not-recorded"') < two.index('id="check-A2"'), "A1 isn't unit"
    assert layer_chip(markup, "A1") == "Unit" and layer_chip(markup, "A2") == "Layer not recorded", layer_chip(markup, "A2")
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", re.sub(r"## Test types.*?(?=## Notes)", "", report, flags=re.S))
    _, markup = generate(repo, base)
    assert layer_groups(markup) == ["not-recorded"]
    assert layer_chip(markup, "A1") == "Layer not recorded", layer_chip(markup, "A1")


# v2 AC3: each check's steps, expected, actual and evidence

def a_check_opens_to_steps_expected_actual_evidence(base):
    repo, evidence = full_branch(base)
    output(evidence, "S1.txt", "bash checks/S1.sh", "precondition: an empty cart\nstep: apply coupon ONE\nPASS one discount line\n"
           "noise\nnote: the second load reads the cache\nstep: refresh the cart\nFAIL two discount lines\n", repo, working_tree(repo))
    add_rows(repo, f"| S1 | AC1 | refresh keeps one discount | + | one discount line after refresh | FAIL | two lines: [output]({evidence}/S1.txt) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "S1")
    terms = [text(t) for t in re.findall(r"<dt>(.*?)</dt>", block)]
    assert terms[:4] == ["Steps", "Expected", "Actual", "Source"], terms
    steps = re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", block, re.S).group(1)
    assert [text(s) for s in re.findall(r"<li>(.*?)</li>", steps)] == ["precondition: an empty cart", "step: apply coupon ONE",
                                                                         "note: the second load reads the cache",
                                                                         "step: refresh the cart"], steps
    actual = text(re.search(r"<dt>Actual</dt><dd>(.*?)</dd>", block, re.S).group(1))
    assert actual.startswith("two lines") and actual.index("PASS one discount line") < actual.index("FAIL two discount lines"), actual
    assert "noise" not in actual
    assert "one discount line after refresh" in text(re.search(r"<dt>Expected</dt><dd>(.*?)</dd>", block, re.S).group(1))
    assert "Not recorded" in text(re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", check_block(markup, "A1"), re.S).group(1))


def a_check_proving_many_criteria_counts_them(base):
    repo, evidence = full_branch(base)
    output(evidence, "W1.txt", "make all", "ok\n", repo, working_tree(repo))
    output(evidence, "W2.txt", "make two", "ok\n", repo, working_tree(repo))
    add_rows(repo, f"| W1 | AC1, AC2, AC3 | all three | + | ok | PASS | [output]({evidence}/W1.txt) |\n"
                   f"| W2 | AC1, AC2 | two | + | ok | PASS | [output]({evidence}/W2.txt) |\n")
    _, markup = generate(repo, base)
    many = check_block(markup, "W1")
    meta = text(re.search(r'<p class="meta">(.*?)</p>', many, re.S).group(1))
    assert "proves 3 criteria" in meta and "A coupon applies once." not in meta, f"three criteria aren't counted: {meta}"
    which = re.search(r'<details class="crit"><summary>which criteria</summary>(.*?)</details>', many, re.S)
    assert which and all(f'href="#ac-{n}"' in which.group(1) for n in (1, 2, 3)) and "The receipt names the coupon." in text(which.group(1)), \
        "the counted criteria aren't one click away by their text"
    two = text(re.search(r'<p class="meta">(.*?)</p>', check_block(markup, "W2"), re.S).group(1))
    assert re.search(r"proves A coupon applies once\. ?; A tampered total is refused\.", two) and "criteria" not in two, \
        f"two criteria aren't named in line: {two}"


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



def tab_group(block):
    """(the fieldset's markup, {tab label: whether it is checked}, the radios' names) of a check's Video and Log tabs, or None."""
    found = re.search(r'<fieldset class="tabs"[^>]*>(.*?)</fieldset>', block, re.S)
    if not found:
        return None
    radios = re.findall(r'<input type="radio"[^>]*>', found.group(1))
    labels = dict(re.findall(r'<label for="([^"]+)"[^>]*>(.*?)</label>', found.group(1)))
    checked = {text(labels[re.search(r'id="([^"]+)"', radio).group(1)]): " checked" in radio for radio in radios}
    return found.group(0), checked, {re.search(r'name="([^"]+)"', radio).group(1) for radio in radios}


def a_failed_checks_raw_evidence_folds_but_its_outcome_stays_open(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    block = check_block(markup, "A2")
    assert all(" open" in fold for fold in enclosing_folds(markup, 'id="check-A2"')), "the failed check is folded away"
    outcome = block[:block.find('<details class="raw"')]
    assert "<dt>Expected</dt>" in outcome and "<dt>Actual</dt>" in outcome, "the outcome sits inside the evidence fold"
    raw = re.search(r'<details class="raw">(.*?)<summary>(.*?)</summary>', block, re.S)
    assert raw and " open" not in raw.group(1) and re.match(r"Evidence: \d+ files?$", raw.group(2)), block[:800]
    assert '<pre class="out' not in outcome, "the failed check's log shows before its fold"


def a_log_and_its_video_show_as_tabs_log_first_unless_passed(base):
    repo, evidence = full_branch(base)
    for name, result in (("T1", "PASS"), ("T2", "FAIL"), ("T3", "NOT RUN")):
        output(evidence, f"{name}.txt", f"bash checks/{name}.sh", f"step: run {name}\nPASS the line\n", repo, working_tree(repo))
        put(os.path.join(evidence, f"{name}.webm"), b"\x1aE\xdf\xa3 not really a video")
    add_rows(repo, "".join(f"| {name} | AC1 | a CLI check | + | the line | {result} | seen: [log]({evidence}/{name}.txt) "
                           f"[video]({evidence}/{name}.webm) |\n" for name, result in (("T1", "PASS"), ("T2", "FAIL"), ("T3", "NOT RUN"))))
    _, markup = generate(repo, base)
    names = set()
    for name, selected in (("T1", "Video"), ("T2", "Log"), ("T3", "Log")):
        block = check_block(markup, name)
        group = tab_group(block)
        assert group, f"{name}: the log and the video aren't tabs"
        assert block.count('<fieldset class="tabs"') == 1, f"{name}: its log and video show as tabs more than once"
        fieldset, checked, radio_names = group
        assert list(checked) == ["Video", "Log"], checked
        assert [label for label, on in checked.items() if on] == [selected], (name, checked)
        assert re.search(r"<legend[^>]*>[^<]*\S", fieldset), "AC5: the group has no legend"
        assert len(radio_names) == 1 and not radio_names & names, (name, radio_names, names)
        names |= radio_names
        assert "<video controls" in fieldset and f"{name}.webm" in fieldset, fieldset[:600]
        assert '<pre class="out tail">' in fieldset and "PASS the line" in fieldset, "the Log tab doesn't hold the log"
        steps = re.search(r"<dt>Steps</dt><dd>(.*?)</dd>", block, re.S).group(1)
        assert f"step: run {name}" in text(steps), steps
        assert "evidence current" in text(block), "the freshness doesn't come from the log"
    assert "<script" not in markup.lower()


def a_second_video_of_the_same_stem_stays_beside_the_tabs(base):
    repo, evidence = full_branch(base)
    output(evidence, "V1.txt", "make v1", "PASS one\n", repo, working_tree(repo))
    for ext in ("webm", "mp4"):
        put(os.path.join(evidence, f"V1.{ext}"), b"\x1aE\xdf\xa3 not really a video")
    add_rows(repo, f"| V1 | AC1 | a CLI check | + | the line | PASS | [log]({evidence}/V1.txt) [video]({evidence}/V1.webm) "
                   f"[again]({evidence}/V1.mp4) |\n")
    _, markup = generate(repo, base)
    block = check_block(markup, "V1")
    fieldset = tab_group(block)[0]
    assert "V1.webm" in fieldset and "V1.mp4" not in fieldset, fieldset[:600]
    assert re.search(r'<video controls[^>]*aria-label="V1\.mp4"', block), "the second video was dropped"


def a_log_or_video_alone_or_with_another_stem_is_not_tabs(base):
    repo, evidence = full_branch(base)
    output(evidence, "S1.txt", "make s1", "PASS one\n", repo, working_tree(repo))
    output(evidence, "S2.txt", "make s2", "PASS two\n", repo, working_tree(repo))
    put(os.path.join(evidence, "S3.webm"), b"\x1aE\xdf\xa3 not really a video")
    put(os.path.join(evidence, "S4.webm"), b"\x1aE\xdf\xa3 not really a video")
    output(evidence, "S5.txt", "make s5", "PASS five\n", repo, working_tree(repo))
    put(os.path.join(evidence, "S5.mov"), b"\0\0\0 ftypqt not really")
    add_rows(repo, f"| S1 | AC1 | log alone | + | one | PASS | [log]({evidence}/S1.txt) |\n"
                   f"| S2 | AC1 | other stems | + | two | PASS | [log]({evidence}/S2.txt) [video]({evidence}/S4.webm) |\n"
                   f"| S3 | AC1 | video alone | + | a video | PASS | [video]({evidence}/S3.webm) |\n"
                   f"| S5 | AC1 | a video that doesn't play | + | five | PASS | [log]({evidence}/S5.txt) [video]({evidence}/S5.mov) |\n")
    _, markup = generate(repo, base)
    for name in ("A1", "S1", "S2", "S3", "S5"):
        assert tab_group(check_block(markup, name)) is None, f"{name} shouldn't be tabs"
    assert '<pre class="out tail">' in check_block(markup, "S1")
    two = check_block(markup, "S2")
    assert '<pre class="out tail">' in two and "<video controls" in two
    assert "<video controls" in check_block(markup, "S3")
    assert '<fieldset class="tabs"' not in markup
    put(os.path.join(evidence, "S3.replay"), "")
    _, markup = generate(repo, base)
    assert "a replay of s3.txt" in text(check_block(markup, "S3")).lower(), "a replay alone doesn't say it is one"
    assert "replay" not in text(check_block(markup, "S2")).lower(), "a live video alone reads as a replay"

def a_replay_says_so_and_a_check_without_a_video_is_your_call(base):
    repo, evidence, gh = ready_branch(base)
    put(os.path.join(evidence, "U1.replay"), "a replay of U1.txt, recorded after the check ran\n")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Ready")
    live = tab_group(check_block(markup, "A1"))
    assert live and list(live[1]) == ["Video", "Log"] and "replay" not in text(live[0]).lower(), \
        "a live video's tabs read as before"
    replay = tab_group(check_block(markup, "U1"))
    assert replay, "the replay and its log aren't tabs"
    video_tab = next(iter(replay[1]))
    assert "replay" in video_tab.lower() and "log" in video_tab.lower(), ("the tab doesn't say it is a replay of the log", replay[1])
    assert text(check_block(markup, "U1")).count("not a live recording") == 1, "the replay's caption isn't shown once"
    os.remove(os.path.join(evidence, "U1.replay"))
    assert "replay" not in text(tab_group(check_block(generate(repo, base, gh)[1], "U1"))[0]).lower(), \
        "the page read the marker once and kept it"

    rows = ready_rows(evidence).replace(f" [video]({evidence}/M1.webm)", "") + \
        f"| A9 | AC2 | e2e | Needs staging | + | shown | NOT RUN | needs the payment sandbox |\n"
    report_edit(repo, "validation", ready_rows(evidence), rows)
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert "no video" in text(check_block(markup, "M1")), "a check without a video doesn't say so"
    assert "no video" not in text(check_block(markup, "A9")), "a check that didn't run needs no video"
    assert "no video" not in text(check_block(markup, "A1"))
    assert_verdict(markup, "Your call", "Planted bugs", "evidence is missing")
    assert rail_of(markup)["Validate"] == ("not run", "1 of 6 checks did not run", "wait"), rail_of(markup)["Validate"]
    assert "video evidence is missing for “Planted bugs”" in why_of(markup), why_of(markup)

    put(os.path.join(evidence, "M1.webm"), b"")
    report_edit(repo, "validation", rows, ready_rows(evidence).replace("| PASS: 21/21 killed |", "| FAIL: 20/21 killed |"))
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert "no video" in text(check_block(markup, "M1")), "an empty video, on a FAIL row, counted as a video"
    calls = [text(item) for item in re.findall(r"<li>.*?</li>", re.search(r'<ul id="calls">(.*?)</ul>', markup, re.S).group(1), re.S)]
    assert sum("Planted bugs" in call for call in calls) == 1, ("an empty video gave M1 other than one reason", calls)
    os.remove(os.path.join(evidence, "M1.webm"))
    _, markup = generate(repo, base, gh)
    calls = [text(item) for item in re.findall(r"<li>.*?</li>", re.search(r'<ul id="calls">(.*?)</ul>', markup, re.S).group(1), re.S)]
    assert [call for call in calls if "M1" in call and "M1.webm" in call] and sum("M1" in call for call in calls) == 1, \
        ("a missing video gave M1 other than its one reason", calls)


def why_of(markup):
    return text(re.search(r'<details[^>]*id="verdict-why".*?</details>', markup, re.S).group(0))


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
    assert "3 tests" in summary_line and "1 FAIL" in summary_line, summary_line
    meta = text(re.search(r'<p class="meta">(.*?)</p>', block, re.S).group(1))
    assert "proves A coupon applies once." in meta and "A coupon applies once." not in summary_line, \
        f"the criterion isn't named once, beside the check: {meta} / {summary_line}"
    def rows_of(markup):
        return [[text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)]
                for r in re.findall(r"<tr>(.*?)</tr>", markup, re.S) if "<td" in r]
    every = re.search(r'<details class="earlier"><summary>All 3 tests, by section</summary>(.*)', runner.group(1), re.S)
    assert every, "AC9: the full list isn't folded under its count"
    head = runner.group(1)[:runner.group(1).index('<details class="earlier">')]
    assert rows_of(head.split('<p class="runner-h">')[0]) == [["cart", "2", "1"], ["refunds", "1", "0"]], rows_of(head.split('<p class="runner-h">')[0])
    parts = dict(re.findall(r'<p class="runner-h">(.*?)</p>(.*?)(?=<p class="runner-h">|$)', head, re.S))
    assert list(parts) == ["Failures", "Named in the spec"], list(parts)
    assert rows_of(parts["Failures"]) == [["tampered_total", "FAIL", "A tampered total is refused."]], rows_of(parts["Failures"])
    assert [r[0] for r in rows_of(parts["Named in the spec"])] == ["tampered_total", "refund_issued"], parts["Named in the spec"]
    groups = re.findall(r'<p class="runner-h">(.*?)</p>', every.group(1))
    assert groups == ["cart", "refunds"], groups
    assert rows_of(every.group(1)) == [["coupon_once", "ok", ""], ["tampered_total", "FAIL", "A tampered total is refused."], ["refund_issued", "ok", "A tampered total is refused."]]
    assert "<details class=\"runner\">" not in check_block(markup, "A1"), "AC3: `ok 12 rows` outside a section was read as a test"
    assert "3 tests in the runner output, 1 failed" in text(section(markup, 2)), text(section(markup, 2))[:500]


# v2 AC4: the review per lens

def the_review_shows_each_lens(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + LENSES)
    _, markup = generate(repo, base)
    three = section(markup, 3)
    table = re.search(r'aria-label="Table: Lens, What it checks, Model, Findings, Confirmed, Outcomes"><table class="lenses">(.*?)</table>', three, re.S)
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
    open_entry = re.search(r'<tr id="review-open-1">(.*?)</tr>', three, re.S)
    assert open_entry and "src/cart.py:77" in text(open_entry.group(1)), "AC4: a finding lost its parts"


def a_review_without_lenses_says_not_recorded(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    three = section(markup, 3)
    assert "per-lens counts not recorded" in text(three), text(three)[:600]
    rows = lens_rows(markup)
    assert [r[0] for r in rows] == ["correctness", "tests"], rows
    assert rows[0][1] == "You are reviewing for behavior bugs only. Ignore style.", rows[0]
    assert all(cell == "not recorded" for row in rows for cell in row[2:]), rows
    write(repo, "review", "# " + REVIEW)
    _, markup = generate(repo, base)
    who = text(review_facts(markup))
    assert "2 lenses: correctness, tests" in who and "Cross-model: codex on correctness" in who, who


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
    order = [words.find(s) for s in ("After the merge", "P1", "P2", "Deploy", "make deploy", "Smoke checks",
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
        named = text(annex(five, kind)[0])
        assert named.endswith(path_of(repo, kind)), (kind, named)
        assert text(five).count(path_of(repo, kind)) == 1, (kind, text(five).count(path_of(repo, kind)))
    assert "c123" in text(five) and "Only unit-tested: the refund path." in text(five) and "Second paragraph with detail." in text(five)
    os.remove(path_of(repo, "follow-pr"))
    _, markup = generate(repo, base)
    words = text(annex(section(markup, 5), "follow-pr")[1])
    assert "not recorded" in words and words.count(path_of(repo, "follow-pr")) == 1, words


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
    assert "Background:" in ship and "Where a regression could show up" in ship and "Recommendation:" in ship
    page = read("docs", "framework.md").split("**`bin/decision-report`**", 1)[1][:2000]
    assert "a rail of spec, review, validate and CI" in page and "control panel" not in page, "the reference describes the old page"


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


def unrun_optional_steps_are_still_to_do(base):
    repo, _ = full_branch(base)
    write(repo, "checks-guide", GUIDE.replace("## After the merge", "### M0. Setup\n- **Where:** you\n\n### M3. Try it in Safari\n"
                                              "- **Where:** you, on a Mac\n\n### S0. Staging access\n- **Where:** you\n\n"
                                              "### S1. Load staging\n- **Where:** you, on staging\n\n## After the merge"))
    _, markup = generate(repo, base)
    todo = text(re.search(r'<h3 id="todo">.*?(?=<details class="more"|</div></details>)', section(markup, 2), re.S).group(0))
    assert "M3 Try it in Safari" in todo and "S1 Load staging" in todo, todo
    assert "M0" not in todo and "S0" not in todo, todo
    decided = " | ".join(listed(markup, "decisions"))
    assert "M3" not in decided and "S1" not in decided, "a step the owner may run is listed as a decision before the merge"


def a_failed_step_after_the_merge_is_a_blocker(base):
    repo, _ = full_branch(base)
    write(repo, "checks-guide", GUIDE.replace("| M2 | PASS | refund seen |", "| M2 | PASS | refund seen |\n| P1 | FAIL | 3 errors |"))
    _, markup = generate(repo, base)
    assert any("P1" in b and "failed" in b for b in listed(markup, "blockers")), listed(markup, "blockers")
    assert 'href="#guide-P1"' in markup and enclosing_folds(markup, 'id="guide-P1"'), "the blocker's link has nowhere to open"


def test_types_heading_and_ran_in_any_form(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation")) as fh:
        report = fh.read()
    write(repo, "validation", report.replace("## Test types", "## Test Types").replace(
        f"| unit | yes | [A1]({evidence}/A1.txt) |", f"| unit | yes | [A1]({evidence}/A1.txt) |\n| a11y | `no` | no screen reader |"))
    _, markup = generate(repo, base)
    assert layer_ids(markup, "Unit") == ["A1"], layer_row(markup, "Unit")
    assert "Not run a11y tests: no screen reader" in layer_row(markup, "Accessibility"), layer_row(markup, "Accessibility")


def levels_map_aliases_and_stay_bounded(base):
    repo, evidence = full_branch(base)
    rows = "".join(f"| X{n} | AC1 | lvl{n} | many | + | ok | PASS | none |\n" for n in range(500))
    add_rows(repo, "| Y1 | AC1 | Accessibility | axe | + | ok | PASS | none |\n| Y2 | AC1 | visual | shots | + | ok | PASS | none |\n"
                   "| Y3 | AC1 | **Query budget** | queries | + | ok | PASS | none |\n" + rows,
             header="| # | AC | Level | Check | Case | Expected | Result | Evidence |")
    _, markup = generate(repo, base)
    groups = layer_groups(markup)
    assert len(groups) <= 14 and "other" in groups, groups
    assert len(re.findall(r'<span class="lay">', section(markup, 2))) <= 14, "a report can widen the test map without bound"
    for check, name in (("Y1", "Accessibility"), ("Y2", "Design"), ("Y3", "Query budget"), ("X7", "Other")):
        assert layer_chip(markup, check) == name, (check, layer_chip(markup, check))


def a_shared_suite_output_counts_once(base):
    repo, evidence = full_branch(base)
    output(evidence, "U1.txt", "tests/run.sh", "== cart\nok   coupon_once\nFAIL tampered_total\nok   refund_issued\n",
           repo, working_tree(repo), exit_code=1)
    add_rows(repo, f"| U1 | AC1 | unit | the suite | + | all ok | FAIL | [output]({evidence}/U1.txt) |\n"
                   f"| U2 | AC2 | unit | the suite again | + | all ok | FAIL | [output]({evidence}/U1.txt) |\n",
             header="| # | AC | Level | Check | Case | Expected | Result | Evidence |")
    _, markup = generate(repo, base)
    assert "3 tests in the runner output, 1 failed" in layer_row(markup, "Unit"), layer_row(markup, "Unit")
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
    problem = headline(markup, "problem")
    assert problem.startswith("The task must stop a second discount"), problem


def triage_lens_names_get_their_brief(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW.replace("lenses: correctness, tests", "lenses: correctness, ux-a11y-i18n (added: forms, labels)"))
    _, markup = generate(repo, base)
    rows = lens_rows(markup)
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
    assert text(section(markup, 1)).count("Not recorded the validation tables have no AC column") == 3, text(section(markup, 1))[:600]
    assert not any("has no check" in d for d in listed(markup, "decisions")), "a table without the column claims no check covers a criterion"


def a_merge_order_item_is_not_the_merge_note(base):
    repo, _ = full_branch(base)
    write(repo, "decision-notes", NOTES.replace("Decide if one FAIL", "- Merge order: #6 before this one.\n- Decide if one FAIL"))
    _, markup = generate(repo, base)
    assert 'id="merge-note"' not in markup, "the verdict is the page's answer: a missing recommendation is no gap to report"
    assert any("Merge order" in i for i in listed(markup, "decisions")), listed(markup, "decisions")


def a_check_id_with_punctuation_gets_its_level(base):
    repo, evidence = full_branch(base)
    add_rows(repo, "| A1.2 | AC1 | dotted | + | ok | PASS | none |\n")
    report = open(path_of(repo, "validation")).read()
    write(repo, "validation", report.replace("| unit | yes | [A1]", "| e2e | yes | A1.2. |\n|  | yes | A1 |\n| unit | yes | [A1]"))
    _, markup = generate(repo, base)
    assert layer_chip(markup, "A1.2") == "End to end", layer_chip(markup, "A1.2")
    assert layer_chip(markup, "A1") == "Unit", layer_chip(markup, "A1")


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
    call = [n for n in verdict_items(markup, "calls") if "verify report" in n]
    assert call and "stop_checks.py verify" in call[0] and "suite.txt" in call[0], verdict_items(markup, "calls")
    assert not [n for n in listed(markup, "warnings") if n.startswith("Verify")], "nothing proves the related tests passed: that's a call, not a note"
    assert "stop_checks.py verify" in footer_value(markup, "Verify"), footer_value(markup, "Verify")
    why = re.search(r'<details[^>]*id="verdict-why".*?</details>', markup, re.S).group(0)
    assert 'href="file://' + urllib.parse.quote(os.path.realpath(os.path.join(evidence, "suite.txt"))) + '"' in why


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


def a_missing_verify_report_is_your_call(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Ready")
    os.remove(path_of(repo, "verify"))
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "verify report")
    assert not verdict_items(markup, "stops"), "a missing report isn't a failed one"


def switching_verify_off_raises_no_verify_blocker(base):
    repo, _, gh = ready_branch(base)
    os.remove(path_of(repo, "verify"))
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "verify report")
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "verify")
    assert out.returncode == 0, out
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Ready")
    assert not [i for i in decide_items(markup) + listed(markup, "warnings") if "verify" in i.lower()], (decide_items(markup), listed(markup, "warnings"))


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


def the_test_map_holds_each_test_in_order(base):
    repo, _ = full_branch(base)
    write(repo, "validation", PYRAMID_ROWS)
    _, markup = generate(repo, base)
    two = section(markup, 2)
    named = [text(n) for n in re.findall(r'<span class="lay">(.*?)</span>', two)]
    assert named == ["Unit", "Integration", "End to end", "By hand", "Accessibility"], named
    assert text(re.search(r'<p class="none">.*?</p>', two, re.S).group(0)) == "None in this change: Mutation.", two
    assert layer_groups(markup) == ["e2e", "a11y"], layer_groups(markup)
    for name, ids in (("Unit", ["A1", "A5"]), ("Integration", ["A4"]), ("End to end", ["A3"]), ("Accessibility", ["A2"])):
        assert layer_ids(markup, name) == ids, (name, layer_row(markup, name))
    assert "f-lvl-" not in markup, "the test map left a filter behind"


def a_layer_without_tests_says_so(base):
    repo, _ = full_branch(base)
    write(repo, "validation", PYRAMID_ROWS.replace("| e2e |", "| unit |").replace("| integration |", "| unit |"))
    _, markup = generate(repo, base)
    none = text(re.search(r'<p class="none">.*?</p>', section(markup, 2), re.S).group(0))
    assert none == "None in this change: Integration, End to end, Mutation.", none
    assert layer_ids(markup, "Unit") == ["A1", "A3", "A4", "A5"], layer_row(markup, "Unit")


def the_coverage_shows_each_criterion_short(base):
    repo, _ = full_branch(base)
    put(path_of(repo, "spec"), SPEC.replace("1. A coupon applies once. — verify: test.",
                                            "1. A coupon applies once. A refresh never adds a second one, nor does `a.b()`. — verify: test."))
    _, markup = generate(repo, base)
    row = re.search(r'<tr[^>]*id="ac-1">(.*?)</tr>', section(markup, 1), re.S).group(1)
    criterion = re.search(r'<th scope="row">(.*?)</th>', row, re.S).group(1)
    assert text(criterion) == "A coupon applies once.", criterion
    fold = re.search(r'<details class="crit"><summary>(.*?)</summary>(.*?)</details>', row, re.S)
    assert fold and text(fold.group(2)).startswith("A refresh never adds a second one, nor does a.b()"), row
    assert re.search(r'<a class="lc p" href="#check-A1"><b>Passed</b>Coupon applied once<span class="id">A1</span></a>', row), row
    short = re.search(r'<tr[^>]*id="ac-3">(.*?)</tr>', section(markup, 1), re.S).group(1)
    assert '<summary>the rest</summary>' in row and "<summary>how it&#x27;s checked</summary>" in short, short


# v2 AC9: the summary says each thing once

def a_level_not_run_shows_once(base):
    repo, evidence = full_branch(base)
    add_rows(repo, "| A9 | AC3 | screen reader | + | read | NOT RUN | no screen reader here |\n")
    report = open(path_of(repo, "validation")).read()
    write(repo, "validation", report.replace("| unit | yes | [A1]", "| a11y | no | no screen reader here (A9) |\n| unit | yes | [A1]"))
    _, markup = generate(repo, base)
    row = layer_row(markup, "Accessibility")
    assert row.count("a11y") == 1 and "Not run a11y tests: no screen reader here" in row, row
    skipped = "| e2e | no | no browser here |\n"
    write(repo, "validation", report.replace("| unit | yes | [A1]", skipped * 2 + "| unit | yes | [A1]"))
    testing = text(section(generate(repo, base)[1], 2))
    assert testing.count("Not run e2e tests") == 1, testing[:800]


def the_review_line_counts_lenses_and_rounds_apart(base):
    repo, _ = full_branch(base)
    review = REVIEW.replace("re-check rounds: 1", "re-check rounds: 2").replace(
        "Not run:   security lens — no auth code touched", "Not run:   none (every lens ran)")
    write(repo, "review", review + LENSES + "| correctness-recheck | codex | 1 | 1 | fixed=1 |\n")
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "claude-review")
    assert out.returncode == 0, out
    _, markup = generate(repo, base)
    line = text(review_facts(markup))
    assert "recheck" not in line and "2 re-check rounds" in line, line
    assert "none (every lens ran)" not in line, line
    assert "Cross-model: codex" in line and "claude-review off by you" in line, line
    write(repo, "review", review.replace("Not run:   none (every lens ran)", "Not run:   cross-model, claude-review switched off by the user"))
    line = text(review_facts(generate(repo, base)[1]))
    assert "switched off" not in line and "claude-review off by you" in line, line
    write(repo, "review", review + LENSES + "| correctness | claude | not run: claude-review switched off by the user | | |\n")
    line = text(review_facts(generate(repo, base)[1]))
    assert "switched off" not in line and "claude-review off by you" in line, line


def a_merge_item_in_the_notes_is_the_recommendation(base):
    repo, _ = full_branch(base)
    for item in ("- Merge with CI green: the risk is low.", "- Merge: with CI green, the risk is low.",
                 "- Merge if CI is green: the risk is low.", "- Merge — CI is green, the risk is low."):
        write(repo, "decision-notes", NOTES.replace("Decide if one FAIL", item + "\n- Decide if one FAIL"))
        _, markup = generate(repo, base)
        assert "the risk is low" in merge_note(markup), (item, merge_note(markup))
        assert not any("the risk is low" in i for i in listed(markup, "decisions")), listed(markup, "decisions")


def several_nits_show_one_per_line(base):
    repo, _ = full_branch(base)
    write(repo, "review", REVIEW + "Open:      (3 Nits kept) a typo in the help; a long line; a stale comment — decide later\n")
    _, markup = generate(repo, base)
    entry = re.search(r'<tr id="review-open-2">(.*?)</tr>', section(markup, 3), re.S)
    assert entry and "3 Nits kept" in text(entry.group(1)), section(markup, 3)[:3000]
    parts = [text(p) for p in re.findall(r'<p class="fr">(.*?)</p>', entry.group(1), re.S)]
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
    assert re.search(r'<dl class="sum"><div class="row"><dt id="sum-signals">Signals</dt>', footer(markup)), "AC1: no Signals row in the footer"
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
        assert parts_of(markup) == CHANGE_PARTS, parts_of(markup)


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
    write(repo, "decision-notes", INVESTIGATION_NOTES.replace("Confidence: medium", "Confidence: high. One trace only"))
    _, markup = generate(repo, base)
    answer = summary_row(markup, "answer")
    assert "One trace only" in answer and ". One trace" not in answer, answer
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


def a_file_a_row_names_three_ways_shows_once(base):
    repo, _ = investigation_branch(base)
    _, markup = generate(repo, base)
    once = check_block(markup, "F1").count('class="file"')
    write(repo, "validation", "# Validation: totals drift\n\n## A. Findings\n\n"
                              + FINDINGS_ROWS.replace("[trace](trace.txt)", "`trace.txt`, [trace](trace.txt) and trace.txt"))
    _, markup = generate(repo, base)
    assert check_block(markup, "F1").count('class="file"') == once, "a file the row names three ways shows more than once"


def notes_over_the_limit_keep_the_change_page(base):
    repo, _ = investigation_branch(base, notes=INVESTIGATION_NOTES + "x" * (2 * 1024 * 1024))
    _, markup = generate(repo, base)
    assert "Decision page for" in text(markup), "notes over 2 MB gave the investigation page"
    shown = sh(repo, REPORTS, "brief")
    assert shown.returncode == 0 and "mode: investigation" not in shown.stdout, shown


# The control-panel page: every part, each verdict rule, each evidence kind.

READY_NOTES = """# Each coupon counts once

A shopper who refreshes the cart keeps one discount.

## Stability
It adds one check on the cart path, and a refused coupon leaves the total as it was.

## Your experience
A shopper sees one discount line, and nothing else moves.

## The problem
Headline: A refresh gave a second discount
Background: Shop sells things online. The cart adds up what a shopper pays, and a coupon lowers it.
Who it hits: Shoppers who refresh the cart.
What they see: Two discount lines and a low total.
How bad: Harmful. The shop loses the discount twice.
How often: Rare. One support ticket on record.
If we wait: Each refresh can still double a discount.

A refresh ran the coupon step twice.

Figure: The double discount
![A shopper applies a coupon.](step1.svg)
![A refresh applies it again.](step2.svg)

## The solution
Headline: The cart remembers each coupon
For you: Nothing to do.
Time added: No measurable time.
Connects to: The cart only.
If it fails: The coupon is refused and logged.
To undo: Revert the pull request.
Rejected: A database lock, since one cart has one writer.

The cart stores the coupon it applied.

### Worked example
```text
apply ONE -> -5, refresh -> -5
```

### Architecture map
![The cart is touched, checkout could be affected. Legend: filled is touched, outlined could be affected.](map.svg)

### Where a regression could show up
| System | Why it could regress | What checked it |
|---|---|---|
| Checkout | It reads the cart total | A1 |
| Receipt | It prints the coupon | Not checked |

## Decisions
- Keep the refused-coupon log line
  Options: A keep it; B drop it
  Cost: A adds one line per refusal.
  Recommendation: A, since support reads it.
  Accepts: A.
"""

READY_REVIEW = """Review (risk: high; lenses: correctness, tests; cross-model: codex per lens; re-check rounds: 1)

Worst catch: a refresh doubled the discount.

## Findings

Fixed:     a coupon applied twice on refresh — src/cart.py:40 — test_coupon_once
Fixed:     the total ignored tax — src/cart.py:52
Rejected:  a race on checkout — handled at src/cart.py:80
Pre-existing: the log grows without a bound — src/log.py:9
Open:      two currencies collapse — src/cart.py:77 — uncertain — If it goes wrong: a shopper sees the wrong currency — Worst case: a wrong total on a mixed cart — Can wait because: the shop sells in one currency today
Not run:   security lens — no auth code touched

## Lenses

| Lens | Model | Findings | Confirmed | Outcomes |
|---|---|---|---|---|
| Correctness | claude | 4 | 3 | fixed=2, wrong=1 |
| Correctness | codex | 2 | 1 | fixed=1 |
| Tests | claude | 1 | 1 | open=1 |
"""

READY_GUIDE = """# Checks guide

## Before the merge (optional)

### M1. Pay on a phone
- **Where:** you, on a phone
- **Expected:** one discount line on the phone.
- **If not:** send the screenshot of the cart.

## After the merge

### P1. Watch the error log
- **Where:** the agent, on this machine
- **Expected:** a quiet error log for an hour.
- **If not:** page the cart owner.
"""


def stamp(repo, *kinds):
    """The stamps for the change as it is, written directly: this page reads them, it doesn't judge how they were earned."""
    code = ("import os,sys;sys.path.insert(0,sys.argv[1]);import review_stamp as r\n"
            "for k in sys.argv[2:]:\n    p=r.stamp_path(k);os.makedirs(os.path.dirname(p),exist_ok=True);open(p,'w').write(r.fingerprint_for(k))\n")
    out = sh(repo, "python3", "-c", code, os.path.join(KIT, "hooks"), *kinds)
    assert out.returncode == 0, out


def stamp_file(repo, kind):
    out = sh(repo, "python3", "-c", "import sys;sys.path.insert(0,sys.argv[1]);import review_stamp as r;print(r.stamp_path(sys.argv[2]))",
             os.path.join(KIT, "hooks"), kind)
    assert out.returncode == 0, out
    return out.stdout.strip()


def ready_rows(evidence):
    return f"""| A1 | AC1 | e2e | Coupon applied once | + | one discount line | PASS | [output]({evidence}/A1.txt) [video]({evidence}/A1.webm) |
| A2 | AC2 | e2e | Tampered total refused | − | exit 1 | PASS | [output]({evidence}/A2.txt) [video]({evidence}/A2.webm), [shot]({evidence}/shot.png) |
| A3 | AC3 | e2e | Receipt names the coupon | + | the coupon's name | PASS: the receipt prints ONE | [output]({evidence}/A3.txt), [video]({evidence}/run.webm) |
| U1 | AC1, AC2 | unit | The suite | + | all pass | PASS | [output]({evidence}/U1.txt) [video]({evidence}/U1.webm) |
| M1 | AC1 | mutation | Planted bugs | − | every one killed | PASS: 21/21 killed | [output]({evidence}/M1.txt) [video]({evidence}/M1.webm) |
"""


def ready_branch(base, notes=READY_NOTES, rows=None):
    """A branch every source calls clean: each verdict test breaks one input of it. Returns (repo, evidence, gh state)."""
    repo = new_repo(base)
    evidence = path_of(repo, "evidence")
    current = working_tree(repo)
    put(path_of(repo, "spec"), SPEC)
    if notes is not None:
        write(repo, "decision-notes", notes)
    write(repo, "pr-body", "The cart applies a coupon once.\n")
    write(repo, "verify", "# Verify: PASS\n\n2026-10-26 · verify in shop\n\nran: tests/run.sh cart\n")
    write(repo, "review", READY_REVIEW)
    for name, body in (("A1", "precondition: a cart\nstep: apply the coupon\nline: one discount\nPASS AC1 one line\n"), ("A2", "exit 1 on a tampered total\n"),
                       ("A3", "ONE on the receipt\n"), ("U1", "== cart\nok a test\n"), ("M1", "killed one\n")):
        output(evidence, f"{name}.txt", f"make {name}", body, repo, current)
        if name != "A3":  # A3's video is run.webm
            put(os.path.join(evidence, f"{name}.webm"), b"\x1aE\xdf\xa3 not really a video")
    output(evidence, "environment.txt", "evidence-env", "php 8.3\n", repo, current)
    put(os.path.join(evidence, "shot.png"), PNG)
    put(os.path.join(evidence, "run.webm"), b"\x1aE\xdf\xa3 not really a video")
    for name, words in (("step1", "first frame"), ("step2", "second frame"), ("map", "the map")):
        put(os.path.join(evidence, f"{name}.svg"), f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 480 40"><text y="20">{words}</text></svg>')
    write(repo, "validation", f"""# Validation: cart

## A. Cart

| # | AC | Level | Check | Case (+/−) | Expected | Result | Evidence |
|---|---|---|---|---|---|---|---|
{rows or ready_rows(evidence)}
## Test types

| Type | Ran | Evidence |
|---|---|---|
| unit | yes | U1 |
| e2e | yes | A1 |

## Not proven

- The receipt on a thermal printer — If it goes wrong: a shopper can't read the coupon's name — Worst case: support answers a question — Checked by: P1, after the merge
""")
    write(repo, "checks-guide", READY_GUIDE)
    stamp(repo, "review", "validate")
    head = git(repo, "rev-parse", "HEAD")
    gh = {"pr": {"number": 7, "url": "https://github.com/o/shop/pull/7", "title": "Apply a coupon once", "headRefOid": head, "state": "OPEN"},
          "checks": {head: {"check_runs": [{"name": "suite", "status": "completed", "conclusion": "success", "details_url": ""}]}}}
    return repo, evidence, gh


def report_edit(repo, kind, old, new):
    path = path_of(repo, kind)
    with open(path) as fh:
        before = fh.read()
    assert old in before, (kind, old)
    put(path, before.replace(old, new))


def page_part(page, name):
    for chunk in re.split(r'(?=<section class="sec")|(?=<div class="box (?:prob|sol)")|(?=<footer)', page):
        if f'<div class="body" id="{name}"' in chunk:
            return chunk
    raise AssertionError(f"no section {name}")


def enclosing_folds(markup, anchor):
    """The attributes of each <details> an element sits inside, outermost first."""
    at, stack = markup.find(anchor), []
    assert at >= 0, anchor
    for found in re.finditer(r"<details([^>]*)>|</details>", markup[:at]):
        if found.group(0) == "</details>":
            stack.pop()
        else:
            stack.append(found.group(1))
    return stack


def verdict_of(page):
    found = re.search(r'<p class="verdict"[^>]*>(.*?)</p>', page, re.S)
    assert found, "no verdict"
    return text(found.group(1))


def embedded_svgs(page):
    return [base64.b64decode(data) for data in re.findall(r'src="data:image/svg\+xml;base64,([^"]+)"', page)]


def assert_verdict(markup, word, *words):
    said = verdict_of(markup)
    assert said.startswith(word), said
    for piece in words:
        assert piece in said or piece in text(re.search(r'<details[^>]*id="verdict-why".*?</details>', markup, re.S).group(0)), (piece, said)


# AC1, and the owner's order: Testing before Code review

PAGE_ORDER = ("<h1", 'class="lead"', 'class="verdict"', 'class="rail"', 'id="problem"', 'id="fix"', 'id="testing"', 'id="review"', 'id="limits"',
              'id="decide"', "<footer")


def assert_page_order(markup):
    at = [markup.find(part) for part in PAGE_ORDER]
    assert -1 not in at and at == sorted(at), dict(zip(PAGE_ORDER, at))


def the_page_holds_its_parts_in_order(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    assert_page_order(markup)
    assert "<h1>Each coupon counts once</h1>" in markup
    assert "A shopper who refreshes the cart keeps one discount." in text(re.search(r'<p class="lead">.*?</p>', markup, re.S).group(0))
    assert list(rail_of(markup)) == ["Spec", "Review", "Validate", "CI", "Your call"], rail_of(markup)
    assert list(cards_of(markup)) == ["Stability", "Your experience", "Undo"], cards_of(markup)


def a_branch_with_nothing_still_has_its_parts_in_order(base):
    repo = new_repo(base)
    _, markup = generate(repo, base)
    assert_page_order(markup)
    assert_verdict(markup, "Not ready")


# AC2: one test per verdict rule, each breaking one input of a clean branch

def a_clean_branch_reads_ready(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Ready", "CI passed")


def a_failed_ci_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    gh["checks"][git(repo, "rev-parse", "HEAD")]["check_runs"][0]["conclusion"] = "failure"
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "CI failed")


def a_missing_ci_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    gh["checks"] = {}
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "CI")


def a_branch_without_a_pr_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    gh["pr"] = None
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "CI")


def a_fail_row_is_not_ready(base):
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | **FAIL** |")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "Tampered total refused failed")


def a_failed_guide_step_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    write(repo, "checks-guide", READY_GUIDE + "\n## Results (2026-10-26)\n\n| Step | Result | Evidence |\n|---|---|---|\n| M1 | FAIL | two lines |\n")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "M1")


def a_missing_review_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    os.remove(stamp_file(repo, "review"))
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "review")


def a_stale_review_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    put(stamp_file(repo, "review"), "an older change\n")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "review is stale")


def a_missing_validation_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    os.remove(path_of(repo, "validation"))
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "validation")


def a_stale_validation_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    put(stamp_file(repo, "validate"), "an older change\n")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "validation is stale")


def stale_evidence_is_not_ready(base):
    repo, evidence, gh = ready_branch(base)
    output(evidence, "A1.txt", "make A1", "old\n", repo, git(repo, "rev-parse", "main^{tree}"))
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "A1.txt")


def a_local_head_past_the_pr_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    put(os.path.join(repo, "README.md"), "Shop, later\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "later")
    stamp(repo, "review", "validate")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "differs")


def a_blocking_open_finding_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "one currency today", "one currency today — Blocking: yes")
    stamp(repo, "review")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "two currencies collapse")


def a_blocking_gap_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "P1, after the merge", "P1, after the merge — Blocking: yes")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "thermal printer")


def a_blocking_decision_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "decision-notes", "  Accepts: A.", "  Accepts: A.\n  Blocking: yes")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Not ready", "refused-coupon log line")


def a_not_run_row_is_your_call(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | NOT RUN |")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "Tampered total refused did not run")


def an_open_finding_without_its_impact_is_your_call(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", " — If it goes wrong: a shopper sees the wrong currency — Worst case: a wrong total on a mixed cart — Can wait because: the shop sells in one currency today", "")
    stamp(repo, "review")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "impact")


def a_gap_without_its_impact_is_your_call(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", " — If it goes wrong: a shopper can't read the coupon's name — Worst case: support answers a question — Checked by: P1, after the merge", "")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "impact")


def a_phase_off_never_blocks(base):
    repo, _, gh = ready_branch(base)
    os.remove(path_of(repo, "review"))
    os.remove(stamp_file(repo, "review"))
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "review")
    assert out.returncode == 0, out
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Ready")


def notes_reasons(markup):
    why = re.search(r'<details[^>]*id="verdict-why".*?</details>', markup, re.S).group(0)
    return [(href, text(phrase)) for href, phrase in re.findall(r'<li><a href="([^"]*)">([^<]*decision notes[^<]*)</a>', why)]


def missing_decision_notes_are_your_call(base):
    for n, notes in enumerate((None, " \n\t\n", "\ufeff\n")):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, _, gh = ready_branch(here, notes=notes)
        markup = generate(repo, here, gh)[1]
        assert_verdict(markup, "Your call")
        assert "the decision notes are missing" in verdict_of(markup), verdict_of(markup)
        assert any(path_of(repo, "decision-notes") in item for item in verdict_items(markup, "calls")), "the reason names where the notes go"
        reasons = notes_reasons(markup)
        assert len(reasons) == 1 and reasons[0][0] == "#annex-decision-notes" and "missing" in reasons[0][1], (notes, reasons)
        assert "missing decision notes" in text(re.search(r'<details[^>]*id="verdict-why".*?</details>', markup, re.S).group(0)), "the rule names them"
    here = os.path.join(base, "ci")
    os.makedirs(here)
    repo, _, gh = ready_branch(here, notes=None)
    gh["checks"][git(repo, "rev-parse", "HEAD")]["check_runs"][0]["conclusion"] = "failure"
    markup = generate(repo, here, gh)[1]
    assert_verdict(markup, "Not ready", "CI failed")
    assert sum("decision notes are missing" in item for item in verdict_items(markup, "calls")) == 1, verdict_items(markup, "calls")
    assert not any("decision notes" in item for item in verdict_items(markup, "stops")), verdict_items(markup, "stops")
    here = os.path.join(base, "unreadable")
    os.makedirs(here)
    repo, _, gh = ready_branch(here)
    put(path_of(repo, "decision-notes"), b"\xff\xfe not text")
    reasons = notes_reasons(generate(repo, here, gh)[1])
    assert len(reasons) == 1 and "can't be read" in reasons[0][1], reasons


def an_unreadable_guide_is_your_call(base):
    repo, _, gh = ready_branch(base)
    put(path_of(repo, "checks-guide"), b"\xff\xfe not text")
    _, markup = generate(repo, base, gh)
    assert_verdict(markup, "Your call", "checks guide")


def the_first_screen_holds_no_status_tokens(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | **FAIL** |")
    _, markup = generate(repo, base, gh)
    first = re.search(r'<header class="hero">(.*?)</header>', markup, re.S).group(1)
    first = re.sub(r'<details[^>]*id="verdict-why".*?</details>', "", first, flags=re.S)  # a fold, past the first screen
    assert not re.search(r" [·|•] ", text(first)), text(first)
    assert cards_of(markup), "no cards to read"
    for label, body in cards_of(markup).items():
        assert body and body.endswith((".", "?", "!")), (label, body)
        assert not re.search(r"\.\s+[A-Z]", body), f"more than one sentence: {label}: {body}"


# AC3 and the owner's Background and map

def the_problem_carries_its_fact_sheet(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "problem")
    said = text(part)
    assert headline(markup, "problem") == "A refresh gave a second discount", headline(markup, "problem")
    for label, value in (("Who it hits", "Shoppers who refresh"), ("What they see", "Two discount lines"), ("How bad", "Harmful."),
                         ("How often", "Rare."), ("If we wait", "Each refresh can still")):
        found = re.search(rf"<dt>{label}.*?</dt><dd>(.*?)</dd>", part, re.S)
        assert found and value in text(found.group(1)), label
    assert part.count("Agent&#x27;s judgment") >= 2, "How bad and How often name whose judgment they are"
    assert "SEV" not in said


def a_background_comes_before_the_problem(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "problem")
    in_order(part, "Who it hits", "Shop sells things online", "A refresh ran the coupon step twice")


def missing_facts_are_left_out(base):
    repo, _, gh = ready_branch(base, notes="## The problem\nA refresh ran twice.\n\n## The solution\nThe cart stores it.\n")
    _, markup = generate(repo, base, gh)
    problem, fix = text(page_part(markup, "problem")), text(page_part(markup, "fix"))
    for label in ("Some background first", "Who it hits", "What they see", "How bad", "How often", "If we wait", "No deadline", "Not recorded"):
        assert label not in problem, (label, problem)
    for label in ("For you", "Time added", "Connects to", "If it fails", "To undo", "Rejected", "Worked example", "Before and after", "Not recorded"):
        assert label not in fix, (label, fix)
    change = text(re.search(r'<section class="sec"><h2>The change</h2>.*?</section>', markup, re.S).group(0))
    assert "Architecture map" not in change and "Where a regression could show up" not in change, change
    assert cards_of(markup) == {}, cards_of(markup)
    assert headline(markup, "problem") == "A refresh ran twice." and headline(markup, "fix") == "The cart stores it."


def the_solution_carries_its_fact_sheet_and_map(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "fix")
    assert headline(markup, "fix") == "The cart remembers each coupon", headline(markup, "fix")
    for label, value in (("For you", "Nothing to do"), ("Time added", "No measurable"), ("Connects to", "The cart only"), ("If it fails", "refused and logged"),
                         ("To undo", "Revert"), ("Rejected", "A database lock")):
        found = re.search(rf"<dt>{label}.*?</dt><dd>(.*?)</dd>", part, re.S)
        assert found and value in text(found.group(1)), label
    assert "apply ONE -&gt; -5" in part, "the worked example"
    in_order(part, "Architecture map", 'alt="The cart is touched', 'id="regressions"')
    assert re.search(r'class="legend".*?Changed.*?Could be affected.*?Not touched', part, re.S), "the map has its legend"
    table = re.search(r'<table[^>]*id="regressions".*?</table>', part, re.S)
    assert table, "no regression table"
    said = text(table.group(0))
    assert "Checkout" in said and "It reads the cart total" in said and "A1" in said and "Not checked" in said, said
    assert re.search(r"Agent&#x27;s judgment.*?<table[^>]*id=\"regressions\"", part, re.S), "the table names whose judgment it is"


def a_blank_regression_check_reads_not_checked(base):
    notes = READY_NOTES.replace("| Receipt | It prints the coupon | Not checked |", "| Receipt | It prints the coupon |  |")
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    assert "Not checked" in text(re.search(r'<table[^>]*id="regressions".*?</table>', markup, re.S).group(0))


def the_skills_document_the_new_notes(base):
    ship = open(os.path.join(KIT, "skills", "kit-ship", "SKILL.md")).read()
    for field in ("Who it hits", "What they see", "How bad", "How often", "If we wait", "For you", "Time added", "Connects to", "If it fails", "To undo", "Rejected",
                  "Worked example", "Background", "Architecture map", "Where a regression could show up", "Change map", "Figure:", "Stability", "Your experience"):
        assert field in ship, field
    review = open(os.path.join(KIT, "skills", "kit-review", "SKILL.md")).read()
    for field in ("If it goes wrong:", "Worst case:", "Can wait because:", "Worst catch:"):
        assert field in review, field
    validate = open(os.path.join(KIT, "skills", "kit-validate", "SKILL.md")).read()
    for field in ("## Not proven", "If it goes wrong:", "Worst case:", "Checked by:"):
        assert field in validate, field


# AC4 and the owner's per-figure controls

def a_figure_embeds_in_place(base):
    repo, evidence, gh = ready_branch(base, notes=READY_NOTES.replace("A refresh ran the coupon step twice.", "Before.\n\n![What the bars show](bars.png)\n\nAfter."))
    put(os.path.join(evidence, "bars.png"), PNG)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "problem")
    in_order(part, "Before.", 'alt="What the bars show"', "After.")
    assert "data:image/png;base64," in part


def an_svg_stays_an_inert_image(base):
    repo, evidence, gh = ready_branch(base)
    put(os.path.join(evidence, "map.svg"), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9"><script>steal()</script><text>m</text></svg>')
    _, markup = generate(repo, base, gh)
    assert "steal()" not in markup and "<script" not in markup and "<svg" not in markup, "an SVG's markup is on the page"
    assert not re.search(r'href="[^"]*\.svg"', markup), "a link to an SVG original runs its scripts"
    assert not [src for tag, attrs in tags(markup) for name, src in attrs if name == "src" and tag == "img" and not src.startswith("data:image/")]
    assert any(b"steal()" in svg for svg in embedded_svgs(markup)), "the figure wasn't embedded as an image"


def a_figure_series_is_a_static_strip(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    strip = re.search(r'<figure class="strip".*?</figure>\s*(?=<p|<dl|<h3|</div>)', page_part(markup, "problem"), re.S)
    assert strip, "no strip"
    strip = strip.group(0)
    assert "The double discount" in text(strip), "the series keeps its title"
    assert "A shopper applies a coupon." in text(strip) and "A refresh applies it again." in text(strip), "the captions are text"
    assert strip.find("A shopper applies") < strip.find("A refresh applies"), "the frames keep their order"
    assert len(re.findall(r'<img src="data:image/svg\+xml', strip)) == 2
    assert not [1 for tag, attrs in tags(markup) if tag == "input" and not evidence_tab(attrs)], "nothing moves, so nothing needs a control"
    style = re.search(r"<style>(.*?)</style>", markup, re.S).group(1)
    assert "@keyframes" not in style and "animation" not in style, "the page's stylesheet moves nothing"


def a_single_image_is_not_a_series(base):
    notes = READY_NOTES.replace("Figure: The double discount\n![A shopper applies a coupon.](step1.svg)\n![A refresh applies it again.](step2.svg)\n", "![One image.](step1.svg)\n")
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    assert 'class="strip"' not in markup, "a single image is no series"
    assert 'alt="One image."' in page_part(markup, "problem")


def an_svg_with_its_own_motion_holds_still(base):
    repo, evidence, gh = ready_branch(base)
    put(os.path.join(evidence, "map.svg"), '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 9 9"><style>.a{animation:x 1s infinite}@keyframes x{to{opacity:0}}</style>'
                                           '<rect class="a" width="9" height="9"><animate attributeName="x" to="3" dur="1s"/><set attributeName="y" to="1"/></rect></svg>')
    _, markup = generate(repo, base, gh)
    holding = [svg for svg in embedded_svgs(markup) if b"<rect" in svg]
    assert holding, "the figure is gone"
    for svg in holding:
        assert b"<animate" not in svg and b"<set" not in svg, svg
        assert b"*{animation:none!important}" in svg, "nothing would control this motion, so it holds still"


SCENE = {"title": "The coupon runs twice", "alt": "A coupon applies once, then a refresh applies it again and the total drops twice.",
         "seconds": 10, "height": 120,
         "shapes": [{"shape": "rect", "x": 0, "y": 8, "width": 190, "height": 66, "stroke": "rule", "fill": "bg"},
                    {"shape": "text", "x": 95, "y": 36, "text": "Cart", "font-size": 20, "font-weight": 600, "fill": "fg", "text-anchor": "middle"},
                    {"shape": "line", "x1": 192, "y1": 41, "x2": 232, "y2": 41, "stroke": "need", "stroke-width": 3, "stroke-dasharray": [40],
                     "tracks": {"opacity": [[2, 0], [2.5, 1], [7, 1], [8, 0]], "stroke-dashoffset": [[2, 40], [4, 0]]}},
                    {"shape": "rect", "x": 0, "y": 90, "width": 480, "height": 20, "fill": "need", "tracks": {"scaleX": [[1, 0], [9, 1]]}},
                    {"shape": "group", "tracks": {"opacity": [[5, 0], [5.5, 1]]},
                     "shapes": [{"shape": "circle", "cx": 300, "cy": 40, "r": 8, "fill": "need", "pulse": True}]}],
         "steps": [{"from": 0, "to": 5, "caption": "A shopper applies a coupon."}, {"from": 5, "to": 10, "caption": "A refresh applies it again."}]}
SCENE_SPOT = "A refresh ran the coupon step twice."


def scene_page(base, scenes):
    """The clean branch with these scene files (name to content) named in its problem, in order, each after a line of its own."""
    lines = "\n\n".join(f"Before {name}.\n\n![{name}]({name}.scene.json)" for name in scenes)
    repo, evidence, gh = ready_branch(base, notes=READY_NOTES.replace(SCENE_SPOT, lines + "\n\nAfter."))
    for name, content in scenes.items():
        data = content if isinstance(content, bytes) else content.encode() if isinstance(content, str) else json.dumps(content).encode()
        put(os.path.join(evidence, name + ".scene.json"), data)
    return generate(repo, base, gh)[1]


def players_of(markup):
    return re.findall(r'<div class="player".*?</div></div>(?=</figure>)', markup, re.S)


def stylesheet(markup):
    return re.search(r"<style>(.*?)</style>", markup, re.S).group(1)


def keyframes_of(markup):
    return {name: body for name, body in re.findall(r"@keyframes (\w+)\{((?:[^{}]*\{[^{}]*\})*)\}", stylesheet(markup))}


def a_scene_plays_in_place(base):
    markup = scene_page(base, {"coupon": SCENE})
    part = page_part(markup, "problem")
    in_order(part, "Before coupon.", 'class="player"', "After.")
    assert "<script" not in markup and "<img" not in re.search(r'<div class="player".*', part, re.S).group(0).split("After.")[0], "a scene is inline SVG, with no script"
    metas = [dict(attrs) for tag, attrs in tags(markup) if tag == "meta"]
    assert [m["content"] for m in metas if m.get("http-equiv") == "Content-Security-Policy"] == [CSP], "the CSP stays exactly as it is"
    player = players_of(markup)[0]
    assert '<figcaption class="strip-title">The coupon runs twice</figcaption>' in part, "the title is on the page"
    radios = re.findall(r'<input type="radio" class="sw s-(ra|pa|rb|pb) vh" name="([\w.-]+)" id="([\w.-]+)"', player)
    assert [r[0] for r in radios] == ["ra", "pa", "rb", "pb"] and len({r[1] for r in radios}) == 1, radios
    assert re.search(r'<input type="radio" class="sw s-ra vh"[^>]* checked>', player) and player.count(" checked") == 1, "it starts playing, on the first copy"
    copies = re.findall(r'<div class="copy copy-([ab])">(.*?)</div>(?=<div class="c)', player, re.S)
    assert [side for side, _ in copies] == ["a", "b"] and copies[0][1] == copies[1][1], "restart swaps two identical copies"
    drawn = copies[0][1]
    assert drawn.count("<svg") == 1 and 'role="img"' in drawn and f'aria-label="{SCENE["alt"]}"' in drawn, "the alt text carries what the motion shows"
    assert 'viewBox="0 0 480 120"' in drawn and "--L:10s" in player
    assert '<i class="prog" aria-hidden="true"></i>' in drawn, "a progress bar on the same timeline"
    assert ">A shopper applies a coupon.</li>" in drawn and ">A refresh applies it again.</li>" in drawn, "every caption is visible"
    assert re.findall(r'<label class="btn [\w-]+" for="([\w.-]+)">', player) == [radios[1][2], radios[0][2], radios[3][2], radios[2][2], radios[2][2], radios[0][2]]
    frames = keyframes_of(markup)
    line = re.search(r'<line [^>]*>', drawn).group(0)
    assert 'x1="192"' in line and 'stroke="var(--need)"' in line and 'stroke-dasharray="40"' in line, line
    assert 'opacity="0"' in line and 'stroke-dashoffset="0"' in line, "the base attributes are the last value of each track: the whole picture"
    assert re.search(r'class="m" style="--k:\w+"', line), line
    body = frames[re.search(r"--k:(\w+)", line).group(1)]
    for stop, declared in (("0.00%", "opacity:0"), ("20.00%", "opacity:0"), ("25.00%", "opacity:1"), ("70.00%", "opacity:1"), ("80.00%", "opacity:0"), ("100.00%", "opacity:0"),
                           ("20.00%", "stroke-dashoffset:40"), ("40.00%", "stroke-dashoffset:0")):
        assert re.search(re.escape(stop) + r"\{[^}]*" + re.escape(declared) + r"[;}]", body), (stop, declared, body)
    bar = re.search(r'<rect [^>]*class="m grow"[^>]*>', drawn).group(0)
    assert "transform" not in bar, "a bar ending full needs no base transform"
    assert "animation-timing-function:linear" in frames[re.search(r"--k:(\w+)", bar).group(1)], "the bar fills without easing"
    light = re.findall(r'<li class="m" style="--k:(\w+)">', drawn)
    assert len(light) == 2, drawn
    first, second = frames[light[0]], frames[light[1]]
    assert re.search(r"48\.00%\{color:var\(--fg\);border-left-color:var\(--ok\)", first) and re.search(r"50\.00%\{color:var\(--muted\)", first), first
    assert re.search(r"48\.00%\{color:var\(--muted\)", second) and re.search(r"50\.00%\{color:var\(--fg\)", second), second
    style = stylesheet(markup)
    assert ".m{animation:var(--k) var(--L) linear infinite}" in style and ".grow{transform-box:fill-box;transform-origin:left}" in style
    group = re.search(r'<g [^>]*>', drawn).group(0)
    assert re.search(r'class="m" style="--k:\w+"', group) and 'opacity="1"' in group, group
    pulse = re.search(r'<circle [^>]*>', drawn).group(0)
    assert 'class="busy"' in pulse and re.search(r'style="animation-delay:-[\d.]+s"', pulse), "the pulse's delay is the generator's, from its place"


def a_scene_keeps_one_attribute_of_each_kind(base):
    both = {**SCENE, "shapes": [{"shape": "rect", "x": 0, "y": 0, "width": 480, "height": 20, "stroke": "need", "tracks": {"scaleX": [[0, 0], [5, .5]], "opacity": [[0, 1], [5, .5]]}},
                                {"shape": "text", "x": 1, "y": 40, "text": "pulsing", "pulse": True}]}
    markup = scene_page(base, {"both": both})
    player = players_of(markup)[0]
    for tag in re.findall(r"<[a-z]+ [^>]*>", player):
        assert tag.count(" class=") <= 1 and tag.count(" style=") <= 1, ("a duplicate attribute silently drops the second", tag)
        declared = re.search(r' style="([^"]*)"', tag)
        assert not declared or not re.search(r"(?:^|;)animation:", declared.group(1)), ("an inline animation shorthand beats the pause rule", tag)
    grown = re.search(r'<rect [^>]*>', player).group(0)
    assert 'class="m grow"' in grown and "transform:scaleX(0.5)" in grown and 'opacity="0.5"' in grown, ("a bar that ends part way keeps that as its picture", grown)


def a_scene_slides_a_shape_to_its_final_place(base):
    slid = {**SCENE, "shapes": [{"shape": "circle", "cx": 20, "cy": 100, "r": 9, "tracks": {"translateY": [[0, -80], [2, -80], [4, 0]]}}]}
    markup = scene_page(base, {"slid": slid})
    circle = re.search(r"<circle [^>]*>", players_of(markup)[0]).group(0)
    assert 'cy="100"' in circle and 'class="m"' in circle and "translateY" not in circle, ("its place is the still picture", circle)
    motion = stylesheet(markup)
    assert "transform:translateY(-80px)" in motion and "transform:translateY(0px)" in motion, motion[:2000]


def a_hostile_scene_is_refused(base):
    def top(**changes):
        return {**SCENE, **changes}

    def shape(extra, kind="rect"):
        base_shape = {"rect": {"shape": "rect", "x": 0, "y": 0, "width": 9, "height": 9}, "text": {"shape": "text", "x": 0, "y": 20, "text": "t"},
                      "line": {"shape": "line", "x1": 0, "y1": 0, "x2": 1, "y2": 1, "stroke": "fg"}, "group": {"shape": "group", "shapes": []}}[kind]
        return top(shapes=[{**base_shape, **extra}])

    def tracked(tracks):
        return shape({"tracks": tracks})

    many_points = [[n * .001, 1] for n in range(1001)]
    cases = {"slide-ends-away": (tracked({"translateY": [[0, 0], [1, 40]]}), "translateY must end at 0"),
             "slide-and-stretch": (tracked({"translateY": [[0, 9], [1, 0]], "scaleX": [[0, 0], [1, 1]]}), "share one transform"),
             "slide-not-number": (tracked({"translateY": [[0, "1px;background:url(x)"], [1, 0]]}), "translateY value"),
             "slide-far": (tracked({"translateY": [[0, 100000], [1, 0]]}), "translateY value"),
             "not-json": (b"{<script>evil()</script>", "not valid JSON"),
             "not-utf8": (b'{"title":"\xff"}', "not valid JSON"),
             "bottomless": (b"[" * 100_000, "not valid JSON"),
             "nan": (json.dumps(top(seconds=float("nan"))), "not valid JSON"),
             "infinity": (json.dumps(top(height=float("inf"))), "not valid JSON"),
             "huge-exponent": ('{"title":"t","alt":"a","seconds":1e400,"height":9,"shapes":[],"steps":[]}', "seconds must be a number"),
             "top-list": ("[1, 2]", "must be an object"),
             "oversized-file": (json.dumps(top(title="x" * (128 * 1024))), "over 128 KB"),
             "unknown-top-key": (top(onload="evil()"), "is not allowed"),
             "missing-steps": ({k: v for k, v in SCENE.items() if k != "steps"}, 'missing "steps"'),
             "no-shapes": (top(shapes=[]), "shapes must hold"),
             "no-steps": (top(steps=[]), "steps must hold"),
             "seconds-text": (top(seconds="10s;background:url(evil)"), "seconds must be a number"),
             "seconds-bool": (top(seconds=True), "seconds must be a number"),
             "seconds-zero": (top(seconds=0), "seconds must be a number"),
             "seconds-unbounded": (top(seconds=10 ** 9), "seconds must be a number"),
             "height-unbounded": (top(height=10 ** 9), "height must be a number"),
             "title-markup-type": (top(title={"<b>": 1}), "title must be text"),
             "title-long": (top(title="x" * 201), "over 200 characters"),
             "alt-empty": (top(alt="  "), "alt must be text"),
             "unknown-shape": (top(shapes=[{"shape": "image", "href": "evil.svg"}]), 'unknown shape "image"'),
             "foreign-object": (top(shapes=[{"shape": "foreignObject", "x": 1}]), 'unknown shape "foreignObject"'),
             "shape-not-object": (top(shapes=["<script>"]), "must be an object"),
             "event-handler": (shape({"onclick": "evil()"}), "is not allowed"),
             "style-key": (shape({"style": "background:url(evil)"}), "is not allowed"),
             "id-key": (shape({"id": "dup"}), "is not allowed"),
             "class-key": (shape({"class": "m"}), "is not allowed"),
             "x-string": (shape({"x": "1;url(evil)"}), "x must be a number"),
             "x-bool": (shape({"x": True}), "x must be a number"),
             "x-unbounded": (shape({"x": 1e30}), "x must be a number"),
             "missing-geometry": (top(shapes=[{"shape": "rect", "x": 0}]), 'missing "y"'),
             "raw-colour": (shape({"fill": "#ff0000"}), "not a colour token"),
             "named-colour": (shape({"stroke": "red"}), "not a colour token"),
             "url-colour": (shape({"fill": "url(evil)"}), "not a colour token"),
             "var-colour": (shape({"fill": "var(--evil)"}), "not a colour token"),
             "dash-text": (shape({"stroke-dasharray": "4 url(evil)"}), "stroke-dasharray must be"),
             "dash-long": (shape({"stroke-dasharray": [1] * 5}), "stroke-dasharray must be"),
             "small-text": (shape({"font-size": 19}, "text"), "font-size must be a number"),
             "text-not-text": (shape({"text": ["<b>"]}, "text"), "text must be text"),
             "text-long": (shape({"text": "x" * 201}, "text"), "over 200 characters"),
             "anchor-free": (shape({"text-anchor": "url(evil)"}, "text"), "text-anchor must be one of"),
             "weight-free": (shape({"font-weight": "bold;x"}, "text"), "font-weight must be one of"),
             "group-in-group": (top(shapes=[{"shape": "group", "shapes": [{"shape": "group", "shapes": []}]}]), "a group can't hold a group"),
             "group-fill": (shape({"fill": "fg"}, "group"), "is not allowed"),
             "track-fill": (tracked({"fill": [[0, "#f00"], [1, "#00f"]]}), "is not allowed"),
             "track-transform": (tracked({"transform": [[0, 0], [1, 1]]}), "is not allowed"),
             "track-time-past": (tracked({"opacity": [[0, 1], [11, 0]]}), "time must be a number"),
             "track-time-before": (tracked({"opacity": [[-1, 1], [1, 0]]}), "time must be a number"),
             "track-time-text": (tracked({"opacity": [["0s", 1], [1, 0]]}), "time must be a number"),
             "track-backwards": (tracked({"opacity": [[5, 1], [1, 0]]}), "must increase"),
             "track-value": (tracked({"opacity": [[0, 3], [1, 0]]}), "must be a number"),
             "track-css-value": (tracked({"opacity": [[0, "1;background:url(evil)"], [1, 0]]}), "must be a number"),
             "track-one-point": (tracked({"opacity": [[0, 1]]}), "at least two"),
             "track-point-shape": (tracked({"opacity": [[0, 1, 2], [1, 0]]}), "[second, value]"),
             "opacity-twice": (shape({"opacity": .5, "tracks": {"opacity": [[0, 1], [1, .5]]}}), "both"),
             "pulse-text": (shape({"pulse": "yes"}), "pulse must be true or false"),
             "pulse-and-track": (shape({"pulse": True, "tracks": {"opacity": [[0, 1], [1, 0]]}}), "one animation"),
             "too-many-shapes": (top(shapes=[{"shape": "rect", "x": 0, "y": 0, "width": 1, "height": 1}] * 201), "more than 200 shapes"),
             "too-many-in-groups": (top(shapes=[{"shape": "group", "shapes": [{"shape": "rect", "x": 0, "y": 0, "width": 1, "height": 1}] * 150}] * 2), "more than 200 shapes"),
             "too-many-points": (tracked({"opacity": many_points}), "more than 1000 timing points"),
             "too-many-steps": (top(steps=[{"from": n / 10, "to": n / 10 + .05, "caption": "c"} for n in range(13)]), "more than 12 steps"),
             "step-backwards": (top(steps=[{"from": 5, "to": 2, "caption": "c"}]), "must end after it starts"),
             "step-past-end": (top(steps=[{"from": 5, "to": 11, "caption": "c"}]), "to must be a number"),
             "step-too-short": (top(steps=[{"from": 1, "to": 1.2, "caption": "c"}]), "too short"),
             "step-overlap": (top(steps=[{"from": 0, "to": 5, "caption": "c"}, {"from": 4, "to": 8, "caption": "d"}]), "overlap"),
             "step-key": (top(steps=[{"from": 0, "to": 5, "caption": "c", "href": "evil"}]), "is not allowed"),
             "caption-not-text": (top(steps=[{"from": 0, "to": 5, "caption": 3}]), "caption must be text"),
             "caption-long": (top(steps=[{"from": 0, "to": 5, "caption": "x" * 1001}]), "over 1000 characters")}
    markup = scene_page(base, {name: content for name, (content, _) in cases.items()})
    assert not players_of(markup), "a hostile scene played"
    assert "@keyframes" not in stylesheet(markup) and ".player" not in stylesheet(markup)
    refused = re.findall(r'<figure class="strip scene-refused">(.*?)</figure>', markup, re.S)
    assert len(refused) == len(cases), (len(refused), len(cases))
    for (name, (_, reason)), figure in zip(cases.items(), refused):
        assert name + ".scene.json" in figure, (name, figure[:200])
        said = text(figure)
        assert 'refused' in said and reason in said, (name, reason, said[:300])
    for tag, attrs in tags(markup):
        assert tag not in ("script", "svg", "image", "foreignobject"), tag
        for attr, value in attrs:
            assert not attr.startswith("on") and "url(evil" not in (value or "") and "var(--evil" not in (value or ""), (tag, attr, value)
    assert "evil" not in stylesheet(markup)


def markup_in_a_scene_is_only_ever_text(base):
    hostile = "</text></svg><script>evil()</script><img src=x onerror=evil()><style>*{background:url(evil)}</style>"
    scene = {**SCENE, "title": hostile, "alt": hostile + '" onload="evil()',
             "shapes": [{"shape": "text", "x": 1, "y": 40, "text": hostile}], "steps": [{"from": 0, "to": 10, "caption": hostile}]}
    markup = scene_page(base, {"hostile": scene})
    assert len(players_of(markup)) == 1, "plain text can't refuse a scene"
    names = [tag for tag, _ in tags(markup)]
    assert "script" not in names and names.count("style") == 1, names
    player_tags = tags(players_of(markup)[0])
    assert not [tag for tag, _ in player_tags if tag == "img"], "the markup became an element"
    assert not [(tag, attr) for tag, attrs in tags(markup) for attr, _ in attrs if attr.startswith("on")]
    assert "url(evil)" not in stylesheet(markup)
    assert "&lt;script&gt;evil()&lt;/script&gt;" in players_of(markup)[0], "it reads as text"
    assert re.search(r'aria-label="[^"]*&quot; onload=&quot;evil\(\)"', players_of(markup)[0])
    for tag, attrs in player_tags:
        assert dict(attrs).get("style") is None or "evil" not in dict(attrs)["style"]


def a_scene_caption_is_plain_text(base):
    scene = {**SCENE, "steps": [{"from": 0, "to": 10, "caption": "See **bold** `code` [x](https://example.com) https://example.com/y"}]}
    markup = scene_page(base, {"plain": scene})
    player = players_of(markup)[0]
    assert "**bold**" in player and "`code`" in player, "a caption isn't Markdown"
    assert "<a " not in player and "<code" not in player and "<strong" not in player, "a caption links nothing"


def a_refused_scene_costs_only_its_figure(base):
    notes = READY_NOTES.replace(SCENE_SPOT, "Before.\n\n![broken](broken.scene.json)\n\n![good](good.scene.json)\n\nAfter.")
    repo, evidence, gh = ready_branch(base, notes=notes)
    put(os.path.join(evidence, "good.scene.json"), json.dumps(SCENE).encode())
    put(os.path.join(evidence, "broken.scene.json"), json.dumps(SCENE).encode())
    clean_page = generate(repo, base, gh)[1]
    assert len(players_of(clean_page)) == 2
    clean = verdict_of(clean_page)
    broken = {**SCENE, "shapes": [{"shape": "image"}], "alt": "Blocking: yes", "title": "<b>Not ready</b> FAIL"}
    put(os.path.join(evidence, "broken.scene.json"), json.dumps(broken).encode())
    markup = generate(repo, base, gh)[1]
    assert verdict_of(markup) == clean, ("a scene moved the verdict", verdict_of(markup), clean)
    assert_verdict(markup, "Ready")
    figure = re.search(r'<figure class="strip scene-refused">.*?</figure>', markup, re.S).group(0)
    said = text(figure)
    assert "A shopper applies a coupon." in said and "A refresh applies it again." in said, ("its captions still tell the story", said)
    assert 'unknown shape "image"' in said and "refused" in said and "broken" in said, said
    assert "<b>Not ready" not in figure, "a refused scene's text is still only text"
    assert len(players_of(markup)) == 1, "the scene after it still plays"
    assert "After." in page_part(markup, "problem") and "The double discount" in page_part(markup, "problem"), "the prose and figures around it stay"
    assert not [item for item in decide_items(markup) + listed(markup, "blockers") + listed(markup, "warnings") if "scene" in item.lower()], "the verdict's lists name no scene"


def text_the_page_cant_write_costs_only_its_figure(base):
    # json.loads turns "\ud800" into a lone surrogate, which would make the page's UTF-8 write fail and leave no page at all
    steps = [{"from": 0, "to": 5, "caption": "Readable."}, {"from": 5, "to": 10, "caption": "\ud800"}]
    markup = scene_page(base, {"lone": {**SCENE, "title": "\ud800"}, "drawn": {**SCENE, "shapes": [{"shape": "text", "x": 1, "y": 40, "text": "\ud800"}]},
                               "caption": {**SCENE, "steps": steps}, "good": SCENE})
    figures = re.findall(r'<figure class="strip scene-refused">(.*?)</figure>', markup, re.S)
    assert len(figures) == 3 and len(players_of(markup)) == 1, (len(figures), len(players_of(markup)))
    assert "Readable." in text(figures[2]) and figures[2].count("<li") == 1, "the captions it can still say"
    assert_verdict(markup, "Ready")


def a_refused_scene_with_no_readable_captions_says_so(base):
    markup = scene_page(base, {"gone": b"{not json", "listless": {**SCENE, "steps": "none", "shapes": []}, "half": {**SCENE, "shapes": [], "steps": [{"caption": "Readable."}, {"caption": 3}, 7]}})
    figures = re.findall(r'<figure class="strip scene-refused">(.*?)</figure>', markup, re.S)
    assert len(figures) == 3
    assert "<li" not in figures[0] and "<li" not in figures[1], "nothing to read, nothing listed"
    assert "Readable." in text(figures[2]) and figures[2].count("<li") == 1, figures[2]


def a_missing_scene_costs_only_its_figure(base):
    notes = READY_NOTES.replace(SCENE_SPOT, "![Gone](gone.scene.json)\n\n![Outside](../../outside.scene.json)\n\nAfter.")
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "problem")
    assert "gone.scene.json" in part and "missing" in text(part), text(part)[:400]
    assert "outside.scene.json" in part and "outside the evidence directory" in text(part)
    assert "After." in part and not players_of(markup)
    assert_verdict(markup, "Ready")


def two_scenes_leave_no_duplicate_id(base):
    markup = scene_page(base, {"one": SCENE, "two": SCENE, "three": {**SCENE, "title": "Another"}})
    players = players_of(markup)
    assert len(players) == 3, len(players)
    ids = [dict(attrs)["id"] for _, attrs in tags(markup) if "id" in dict(attrs)]
    assert len(ids) == len(set(ids)), sorted(i for i in ids if ids.count(i) > 1)
    for player in players:
        for copy in re.findall(r'<div class="copy copy-[ab]">(.*?)</div>(?=<div class="c)', player, re.S):
            assert ' id="' not in copy and 'href="' not in copy, "two copies would duplicate any id inside, and any link to one"
    names = [re.findall(r'<input type="radio"[^>]* name="([\w.-]+)"', player)[0] for player in players]
    assert len(set(names)) == 3, names
    for player in players:
        targets = re.findall(r' for="([\w.-]+)"', player)
        assert targets and all(target in ids for target in targets), targets
    frames = re.findall(r"@keyframes (\w+)\{", stylesheet(markup))
    assert len(frames) == len(set(frames)), "keyframes of one scene would run another's"
    used = {name for player in players for name in re.findall(r"--k:(\w+)", player)}
    assert used and used == set(frames) - {"busy", "prog"}, used ^ set(frames)
    per_player = [set(re.findall(r"--k:(\w+)", player)) for player in players]
    assert not per_player[0] & per_player[1] and not per_player[1] & per_player[2], "each scene has its own names"


def the_pause_rule_covers_the_progress_bar(base):
    style = stylesheet(scene_page(base, {"coupon": SCENE}))
    rule = re.search(r"([^{}]*)\{animation-play-state:paused\}", style)
    assert rule, "nothing pauses"
    selectors = {selector.strip() for selector in rule.group(1).split(",")}
    assert selectors == {".s-pa:checked~.copy-a *", ".s-pb:checked~.copy-b *", ".s-pa:checked~.copy-a .prog::after", ".s-pb:checked~.copy-b .prog::after"}, selectors
    assert ".prog::after{" in style and "animation:prog var(--L) linear infinite" in style, "the bar runs on the figure's clock"
    assert ".s-rb:checked~.copy-b,.s-pb:checked~.copy-b{display:grid}" in style and ".s-rb:checked~.copy-a,.s-pb:checked~.copy-a{display:none}" in style and ".copy-b{display:none}" in style


def a_scene_holds_still_without_motion(base):
    style = stylesheet(scene_page(base, {"coupon": SCENE}))
    assert re.search(r"(?<![\w-])\.sw\{bottom:", style), "the browser scrolls to the focused radio, and the focus ring is on the controls below the picture"
    for media in ("(prefers-reduced-motion:reduce)", "print"):
        blocks = [body for body in re.findall(r"@media " + re.escape(media) + r"\{((?:[^{}]*\{[^{}]*\})*)\}", style) if ".player" in body]
        assert len(blocks) == 1, (media, blocks)
        block = blocks[0]
        stopped = re.search(r"([^{}]*)\{animation:none!important\}", block)
        assert stopped and {s.strip() for s in stopped.group(1).split(",")} == {".player *", ".player .prog::after"}, (media, block)
        assert ".copy-a{display:grid!important}" in block, (media, "the first copy shows whatever radio is checked")
        hidden = re.search(r"([^{}]*)\{display:none!important\}", block)
        assert hidden and {s.strip() for s in hidden.group(1).split(",")} >= {".copy-b", ".ctl", ".prog", ".sw"}, (media, block)


def old_notes_without_a_scene_carry_no_player(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    style = stylesheet(markup)
    for piece in (".player", ".copy-", "@keyframes", "animation", ".prog"):
        assert piece not in style, piece
    assert 'class="player"' not in markup and '<figure class="strip"' in markup, "an image series stays the still strip"
    assert not [1 for tag, attrs in tags(markup) if tag in ("input", "svg") and not evidence_tab(attrs)]


def a_figure_series_with_a_scene_stops_at_it(base):
    notes = READY_NOTES.replace("![A refresh applies it again.](step2.svg)\n", "![A refresh applies it again.](step2.svg)\n![Moves.](coupon.scene.json)\n")
    repo, evidence, gh = ready_branch(base, notes=notes)
    put(os.path.join(evidence, "coupon.scene.json"), json.dumps(SCENE))
    _, markup = generate(repo, base, gh)
    strip = re.search(r'<figure class="strip">.*?</figure></figure>', markup, re.S).group(0)
    assert strip.count("<img") == 2 and 'class="player"' not in strip, "a scene isn't a frame"
    assert len(players_of(markup)) == 1, "it plays on its own, after the series"


def the_ship_skill_example_scene_plays(base):
    with open(os.path.join(KIT, "skills", "kit-ship", "SKILL.md"), encoding="utf-8") as fh:
        skill = fh.read()
    examples = re.findall(r"```json\n(.*?)```", skill, re.S)
    assert len(examples) == 1, "the contract shows one scene"
    assert "`<name>.scene.json`" in skill and "![what it shows](<name>.scene.json)" in skill, "the contract names the file and the line"
    markup = scene_page(base, {"example": examples[0]})
    assert len(players_of(markup)) == 1, re.findall(r'<figure class="strip scene-refused">.*?</figure>', markup, re.S)


def print_shows_the_folds_content(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    style = re.search(r"<style>(.*?)</style>", markup, re.S).group(1)
    printing = re.search(r"@media print\{(.*?\})\}", style, re.S)
    assert printing and "details" in printing.group(1), "print shows the folds' content"


def a_lost_frame_costs_that_frame(base):
    repo, evidence, gh = ready_branch(base)
    os.remove(os.path.join(evidence, "step2.svg"))
    os.chmod(os.path.join(evidence, "step1.svg"), 0)
    try:
        _, markup = generate(repo, base, gh)
    finally:
        os.chmod(os.path.join(evidence, "step1.svg"), 0o644)
    part = text(page_part(markup, "problem"))
    assert "A shopper applies a coupon." in part and "unreadable" in part, part[:600]
    assert "A refresh applies it again." in part and "missing" in part, part[:600]


# AC5

def review_section(markup):
    return page_part(markup, "review")


def the_review_section_says_who_and_what_each_lens_is_for(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 1 | open=1 |\n| Security | codex | not run: exit 1 | — | — |")
    stamp(repo, "review")
    _, markup = generate(repo, base, gh)
    part = review_section(markup)
    assert part.startswith('<section class="sec">'), "the review is never folded away"
    said = text(part)
    assert "claude" in said and "codex" in said, said[:400]
    assert "Correctness" in said and "Tests" in said
    assert "a refresh doubled the discount" in said, "the worst catch"
    assert "security lens" in said and "no auth code touched" in said and "not run: exit 1" in said, "the lenses that didn't run"
    for outcome in ("Fixed", "Rejected", "Pre-existing", "Open", "Not run"):
        assert re.search(rf'id="review-{outcome.lower().replace(" ", "-")}"', part), outcome
    assert "handled at src/cart.py:80" in said, "a rejection keeps its reason"


def the_review_funnel_shares_one_scale_and_explains_the_difference(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 0 | wrong=1 |")  # confirmed differs from the report's lines
    stamp(repo, "review")
    _, markup = generate(repo, base, gh)
    part = review_section(markup)
    said = text(part)
    assert "The lenses raised 7, and 4 held up when checked against the code." in said, said[:500]
    assert "The report's lines differ from the confirmed count" in said, "the difference is explained"
    assert re.search(r"2 fixed, 1 rejected, 1 open, 1 already there", said), said[:500]


def an_open_finding_shows_its_impact_or_says_it_is_missing(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    row = text(re.search(r'<tr id="review-open-1">.*?</tr>', markup, re.S).group(0))
    for words in ("two currencies collapse", "a shopper sees the wrong currency", "a wrong total on a mixed cart", "the shop sells in one currency"):
        assert words in row, row
    report_edit(repo, "review", " — Worst case: a wrong total on a mixed cart", "")
    _, markup = generate(repo, base, gh)
    row = text(re.search(r'<tr id="review-open-1">.*?</tr>', markup, re.S).group(0))
    assert "Impact not recorded" in row and "a shopper sees the wrong currency" in row, row


def a_legacy_review_keeps_its_findings(base):
    repo, _, gh = ready_branch(base)
    write(repo, "review", REVIEW)
    stamp(repo, "review")
    _, markup = generate(repo, base, gh)
    part = text(review_section(markup))
    for words in ("a coupon applied twice on refresh", "a race on checkout", "the log grows without a bound", "totals in two currencies collapse", "no auth code touched"):
        assert words in part, words
    assert "Impact not recorded" in part
    assert "per-lens counts not recorded" in part.lower(), part[:500]
    assert 'id="review-matrix"' not in review_section(markup) and "The lenses raised" not in part, "raised and confirmed are not invented"
    assert_verdict(markup, "Your call")


def a_missing_review_report_is_named(base):
    repo, _, gh = ready_branch(base)
    report = path_of(repo, "review")
    os.remove(report)
    _, markup = generate(repo, base, gh)
    assert report in text(review_section(markup)) or "no report" in text(review_section(markup)), text(review_section(markup))[:400]
    assert_verdict(markup, "Not ready", "review")


def a_stale_review_is_named_in_its_section(base):
    repo, _, gh = ready_branch(base)
    put(stamp_file(repo, "review"), "an older change\n")
    _, markup = generate(repo, base, gh)
    assert "stale" in text(review_section(markup))[:400], text(review_section(markup))[:400]


def reconstructed_counts_are_flagged(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "Worst catch:", "The Claude lenses' finding counts are reconstructed after a context break.\n\nWorst catch:")
    _, markup = generate(repo, base, gh)
    assert "reconstructed after a context break" in text(review_section(markup))


# AC6 and the owner's layers

def testing_section(markup):
    return page_part(markup, "testing")


def the_criteria_map_to_their_proof_and_a_gap_shows(base):
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", "| A3 | AC3 |", "| A3 |  |")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    part = testing_section(markup)
    one = re.search(r'<tr id="ac-1">.*?</tr>', part, re.S).group(0)
    assert 'href="#check-A1"' in one and 'href="#check-U1"' in one and 'href="#check-M1"' in one, one
    three = re.search(r'<tr id="ac-3">.*?</tr>', part, re.S).group(0)
    assert "No check" in text(three), three
    assert_verdict(markup, "Your call", "no check proves “The receipt names the coupon”")


def every_link_on_the_page_lands_inside_a_fold_it_can_open(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    targets = set(re.findall(r'href="#([^"]+)"', markup))
    assert {"testing", "review", "limits", "decide", "check-A1", "todo-M1"} <= targets, targets
    for target in targets:
        assert re.search(rf'\bid="{re.escape(target)}"', markup), f"nothing at #{target}"
        # A browser opens a closed <details> for a link to something inside it, not for a link to the <details> itself.
        assert not re.search(rf'<details\b[^>]*\bid="{re.escape(target)}"', markup), f"#{target} can't open its fold"


def a_guide_step_already_passed_is_not_to_do(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    assert re.search(r'<p[^>]*id="todo-M2"[^>]*>.*?already passed', markup), "no record of the passed step"
    row = re.search(r'<div class="row"><span class="lay">By hand</span>.*?</div></div>', markup, re.S).group(0)
    assert '<a class="lc p" href="#todo-M2"><b>Passed</b>' in row, row
    assert '<a class="lc s" href="#todo-P1"><b>To do</b>' in row, row
    assert "4 steps in the checks guide: 1 passed, 1 failed, 2 to do by hand." in tally_of(markup), tally_of(markup)


def the_automated_part_is_short_and_says_what_it_did_not_measure(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    part = re.search(r'<div id="automated".*?</dl>', testing_section(markup), re.S)
    assert part, "no automated part"
    said = text(part.group(0))
    assert re.search(r"On this machine\s+Passed", said) and re.search(r"In CI\s+Passed", said) and "21 of 21" in said, said
    assert "PASS" not in said, f"the verdict said twice: {said}"
    assert "Coverage" not in said, f"coverage nobody measured is left off: {said}"
    report_edit(repo, "validation", "PASS: 21/21 killed", "PASS")
    _, markup = generate(repo, base, gh)
    assert "Planted bugs" not in text(re.search(r'<div id="automated".*?</dl>', markup, re.S).group(0)), "no killed count, no invented one"
    report_edit(repo, "verify", "# Verify: PASS", "# Verify: FAIL")
    _, markup = generate(repo, base, gh)
    said = text(re.search(r'<div id="automated".*?</dl>', markup, re.S).group(0))
    assert re.search(r"On this machine\s+FAIL\b", said) and said.count("FAIL") == 1, f"the verdict said twice: {said}"
    assert "tests/run.sh cart" in said, said


def automated_levels_are_not_manual_tests(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    manual = re.search(r'<div id="manual".*?(?=<div id="todo"|<h3 id="todo")', testing_section(markup), re.S).group(0)
    assert 'id="check-A1"' in manual and 'id="check-A2"' in manual
    assert 'id="check-U1"' not in manual and 'id="check-M1"' not in manual, "an automated level is listed among the manual tests"
    automated = re.search(r'<div id="automated".*?(?=<div id="manual")', testing_section(markup), re.S).group(0)
    assert 'id="check-U1"' in automated and 'id="check-M1"' in automated


def a_manual_test_shows_its_proof(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    block = check_block(markup, "A1")
    said = text(block)
    for words in ("step: apply the coupon", "one discount line", "PASS AC1 one line", "exit" , git(repo, "rev-parse", "HEAD")):
        assert words.lower() in said.lower(), words
    assert "AC1" in said and "Checks that it works" in said, said[:300]
    two = check_block(markup, "A2")
    assert "data:image/png;base64," in two, "an image shows inline"
    three = check_block(markup, "A3")
    assert "<video" in three and "the receipt prints ONE" in text(three), "a video plays inline, and a Result's detail is its actual"
    assert 'href="file://' in block, "a link to the full file"


def evidence_shown_earlier_is_shown_again(base):
    notes = READY_NOTES.replace("A refresh ran the coupon step twice.", "A refresh ran the coupon step twice.\n\n![The cart.](shot.png)")
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    assert "data:image/png;base64," in page_part(markup, "problem"), "shown in the problem"
    assert "data:image/png;base64," in check_block(markup, "A2"), "shown again where the test is"
    assert "shown above" not in text(testing_section(markup))


def two_tests_citing_one_file_each_show_it(base):
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", f"[output]({evidence}/A2.txt) ", f"[output]({evidence}/A1.txt) ")
    _, markup = generate(repo, base, gh)
    assert "PASS AC1 one line" in check_block(markup, "A2"), "evidence named twice is shown twice"


def each_evidence_exception_says_which_it_is(base):
    repo, evidence, gh = ready_branch(base)
    put(os.path.join(evidence, "huge.png"), PNG + b"\0" * (2 * 1024 * 1024 + 10))
    put(os.path.join(evidence, "locked.png"), PNG)
    put(os.path.join(evidence, "locked.txt"), "secret\n")
    outside = os.path.join(base, "outside.txt")
    put(outside, "OUTSIDE\n")
    rows = (f"| B1 |  | e2e | huge | + | x | PASS | [i]({evidence}/huge.png) |\n"
            f"| B2 |  | e2e | locked | + | x | PASS | [i]({evidence}/locked.png) |\n"
            f"| B3 |  | e2e | locked text | + | x | PASS | [i]({evidence}/locked.txt) |\n"
            f"| B4 |  | e2e | outside | + | x | PASS | [i]({outside}) |\n")
    report_edit(repo, "validation", "\n## Test types", rows + "\n## Test types")
    for name in ("locked.png", "locked.txt"):
        os.chmod(os.path.join(evidence, name), 0)
    try:
        _, markup = generate(repo, base, gh)
    finally:
        for name in ("locked.png", "locked.txt"):
            os.chmod(os.path.join(evidence, name), 0o644)
    assert "over 2 MB" in text(check_block(markup, "B1"))
    assert "unreadable" in text(check_block(markup, "B2")), text(check_block(markup, "B2"))
    assert "unreadable" in text(check_block(markup, "B3")), text(check_block(markup, "B3"))
    assert "outside the evidence directory" in text(check_block(markup, "B4"))
    assert "OUTSIDE" not in markup.replace(outside, "")


def guide_steps_are_listed_as_to_do(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    steps = [text(step) for step in re.findall(r'<div class="todo-step">(.*?)</div>', testing_section(markup), re.S)]
    assert len(steps) == 2, steps
    assert "M1" in steps[0] and "you" in steps[0].lower() and "before the merge" in steps[0].lower(), steps
    assert "P1" in steps[1] and "agent" in steps[1].lower() and "after the merge" in steps[1].lower(), steps
    assert "one discount line on the phone" in steps[0], "what it proves comes from the guide"


def fail_and_not_run_rows_stay_visible(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | **FAIL** |")
    report_edit(repo, "validation", "| the coupon's name | PASS: the receipt prints ONE |", "| the coupon's name | NOT RUN |")
    report_edit(repo, "validation", "| all pass | PASS |", "| all pass | **FAIL** |")
    _, markup = generate(repo, base, gh)
    for check in ("A2", "A3", "U1"):
        block = check_block(markup, check)
        assert re.search(r"FAIL|NOT RUN", text(block)), check
        assert "<details" not in block or re.search(r"<details[^>]* open", block), f"{check} is folded away"
    assert all(" open" in attrs for attrs in enclosing_folds(markup, 'id="check-U1"')), "a failed automated row sits in a closed fold"


def a_passing_report_from_an_earlier_commit_reads_as_stale(base):
    repo, evidence, gh = ready_branch(base)
    old = git(repo, "rev-parse", "main^{tree}")
    for name in ("A1", "A2", "A3", "U1", "M1"):
        output(evidence, f"{name}.txt", f"make {name}", "old\n", repo, old)
    output(evidence, "environment.txt", "evidence-env", "php\n", repo, old)
    _, markup = generate(repo, base, gh)
    head = text(re.search(r'<p id="freshness">.*?</p>', testing_section(markup), re.S).group(0))
    assert "stale" in head.lower(), head
    assert "stale" in text(check_block(markup, "A1")).lower()
    assert_verdict(markup, "Not ready")


def evidence_without_a_header_reads_as_unknown(base):
    repo, evidence, gh = ready_branch(base)
    for name in ("A1", "A2", "A3", "U1", "M1"):
        put(os.path.join(evidence, f"{name}.txt"), "no header\n")
    _, markup = generate(repo, base, gh)
    head = text(re.search(r'<p id="freshness">.*?</p>', testing_section(markup), re.S).group(0))
    assert "unknown" in head.lower() or "can't tell" in head.lower(), head
    assert "stale" not in head.lower(), head


def evidence_on_this_commit_reads_as_current(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    head = text(re.search(r'<p id="freshness">.*?</p>', testing_section(markup), re.S).group(0))
    assert "covers" in head.lower() and "stale" not in head.lower() and "unknown" not in head.lower(), head


def the_layers_overview_names_each_level_present(base):
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", "\n## Test types", f"| N1 |  |  | no level | + | x | PASS | [o]({evidence}/A1.txt) |\n\n## Test types")
    _, markup = generate(repo, base, gh)
    part = testing_section(markup)
    names = re.findall(r'<div class="row"><span class="lay">(.*?)</span>', part)
    assert names == ["Unit", "End to end", "Mutation", "By hand", "Layer not recorded"], names
    assert layer_ids(markup, "End to end") == ["A1", "A2", "A3"] and layer_ids(markup, "Unit") == ["U1"], layer_ids(markup, "End to end")
    assert '<p class="none">None in this change: Integration.</p>' in part, "a layer with nothing in it says so"
    assert layer_ids(markup, "Layer not recorded") == ["N1"]
    assert 'class="layers"' not in part, "the layer definitions table is cut"


def manual_tests_are_grouped_under_their_layer(base):
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", "\n## Test types", f"| N1 |  |  | no level | + | x | PASS | [o]({evidence}/A1.txt) |\n\n## Test types")
    _, markup = generate(repo, base, gh)
    manual = re.search(r'<div id="manual".*?(?=<div id="todo"|<h3 id="todo")', testing_section(markup), re.S).group(0)
    in_order(manual, 'id="level-e2e"', 'id="check-A1"', 'id="level-not-recorded"', 'id="check-N1"')
    assert re.search(r'class="chip"[^>]*>End to end<', check_block(markup, "A1")), "each test carries its layer in words"
    assert re.search(r'class="chip"[^>]*>Layer not recorded<', check_block(markup, "N1"))
    assert re.search(r'class="chip"[^>]*>Unit<', check_block(markup, "U1")), "automated rows carry theirs too"


# AC7

def limits_section(markup):
    return page_part(markup, "limits")


def a_gap_shows_what_the_user_would_see(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    said = text(limits_section(markup))
    for words in ("The receipt on a thermal printer", "a shopper can't read the coupon's name", "support answers a question", "P1, after the merge"):
        assert words in said, words
    assert "Impact not recorded" not in said


def old_prose_gaps_read_impact_not_recorded(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "## Not proven\n\n- The receipt on a thermal printer — If it goes wrong: a shopper can't read the coupon's name — Worst case: support answers a question — Checked by: P1, after the merge\n",
                "## Only unit-tested\nThe refund path on a real gateway.\n\n## Couldn't run locally\nWhether the printer prints: it needs the shop's printer.\n")
    stamp(repo, "validate")
    _, markup = generate(repo, base, gh)
    said = text(limits_section(markup))
    assert "The refund path on a real gateway" in said and "Whether the printer prints" in said, said
    assert said.count("Impact not recorded") >= 2, said
    assert_verdict(markup, "Your call", "impact")


def notes_bullets_in_a_validation_report_are_gaps(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    said = text(limits_section(markup))
    assert "the refund path" in said and "A3 needs the payment sandbox" in said, said
    assert "Impact not recorded" in said


def no_gap_says_so(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "- The receipt on a thermal printer — If it goes wrong: a shopper can't read the coupon's name — Worst case: support answers a question — Checked by: P1, after the merge\n", "")
    _, markup = generate(repo, base, gh)
    assert "No gap recorded" in text(limits_section(markup))


def a_not_run_check_and_an_uncovered_criterion_are_gaps(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "| A3 | AC3 |", "| A3 |  |")
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | NOT RUN |")
    _, markup = generate(repo, base, gh)
    said = text(limits_section(markup))
    assert "Tampered total refused" in said and "No check proves “The receipt names the coupon”" in said, said


def a_decision_shows_options_cost_recommendation_and_what_merging_accepts(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    part = page_part(markup, "decide")
    said = text(part)
    assert "Keep the refused-coupon log line" in text(re.search(r"<article class=\"decision\".*?</h3>", part, re.S).group(0))
    assert re.findall(r"<li><b>([A-Z])</b>", part) == ["A", "B"], part
    assert "A adds one line per refusal." in said and "A, since support reads it." in said
    assert "Merging without answering accepts A." in said


def no_decisions_say_so_and_merge_is_no_decision(base):
    notes = READY_NOTES[:READY_NOTES.index("## Decisions")]
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    assert "No decisions needed" in text(page_part(markup, "decide"))
    assert rail_of(markup)["Your call"] == ("nothing to decide", "", "you"), rail_of(markup)


def a_legacy_decision_keeps_its_text_and_its_merge_note_moves(base):
    notes = NOTES.replace("Decide if one FAIL on the phone check blocks the merge.",
                          "- Merge: merge when CI passes; recommend merge.\n- The nohup notice: A accepts it, B adds a list; recommend A.")
    repo, _, gh = ready_branch(base, notes=notes)
    _, markup = generate(repo, base, gh)
    part = text(page_part(markup, "decide"))
    assert "The nohup notice" in part and "Not recorded" in part, part
    assert "merge when CI passes" not in part, "Merge is no decision"
    assert "merge when CI passes" in text(element(markup, "merge-note")), "but it stays visible"


# AC8

def an_unreadable_image_costs_that_image(base):
    repo, evidence = full_branch(base)
    locked = os.path.join(evidence, "locked.png")
    put(locked, PNG)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Locked\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B1 | AC1 | a locked image | + | an image | PASS | [img]({locked}) |\n")
    os.chmod(locked, 0)
    try:
        _, markup = generate(repo, base)
    finally:
        os.chmod(locked, 0o644)
    assert "unreadable" in text(check_block(markup, "B1")), text(check_block(markup, "B1"))
    assert "ok 12 rows" in markup, "the page lost the rest"


def an_escaped_backtick_in_a_cell_reads_as_a_backtick(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Ticks\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B1 | AC1 | ticks | + | a name | PASS: named \\`sleep\\` | [o]({evidence}/A1.txt) |\n")
    _, markup = generate(repo, base)
    said = text(check_block(markup, "B1"))
    assert "named `sleep`" in said and "\\`" not in said, said


def a_result_keeps_its_detail(base):
    repo, evidence = full_branch(base)
    with open(path_of(repo, "validation"), "a") as fh:
        fh.write("\n## B. Detail\n\n| # | AC | Check | Case | Expected | Result | Evidence |\n|---|---|---|---|---|---|---|\n"
                 f"| B1 | AC1 | a detail | + | one | PASS: exactly one line | [o]({evidence}/A1.txt) |\n")
    _, markup = generate(repo, base)
    assert "exactly one line" in text(check_block(markup, "B1"))


def old_notes_keep_their_narrative(base):
    repo, _ = full_branch(base)
    _, markup = generate(repo, base)
    assert_page_order(markup)
    assert "The task must stop a second discount" in text(page_part(markup, "problem"))
    fix = page_part(markup, "fix")
    assert "adds a guard" in text(fix) and "apply_coupon" in fix
    assert cards_of(markup)["Your experience"] == "A shopper sees one discount line.", cards_of(markup)
    assert "one FAIL on the phone check" in text(page_part(markup, "decide"))
    assert "Written by the agent" in text(page_part(markup, "problem"))


# The owner's folds

def no_section_is_folded_away(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    assert markup.count('<section class="sec">') == 5 and '<details class="sec"' not in markup, "a section is a fold"
    assert re.findall(r'<div class="body" id="(\w+)">', markup) == ["problem", "fix", "testing", "review", "limits", "decide"]
    for part in parts_of(markup):
        assert not enclosing_folds(markup, f'<div class="body" id="{part}"'), part
    for href in rail_links(markup):
        assert f'id="{href}"' in markup, href
    assert not re.search(r'<details[^>]*\bid="(problem|fix|testing|review|limits|decide|undo|check-[^"]*)"', markup), "a link to an id on <details> doesn't open it"


def long_lists_start_folded_with_their_count(base):
    repo, _, gh = ready_branch(base)
    _, markup = generate(repo, base, gh)
    review = review_section(markup)
    for name, count in (("fixed", 2), ("rejected", 1), ("pre-existing", 1)):
        fold = re.search(rf'<details class="more"(?P<attrs>[^>]*)><summary><b>[^<]*</b> <span class="n">{count}</span>(?:(?!</details>).)*?id="review-{name}-1"', review, re.S)
        assert fold and " open" not in fold.group("attrs"), f"the {name} list isn't folded with its count"
    assert '<details class="check">' in check_block(markup, "A1"), "a passing check starts folded"


def what_blocks_the_merge_stays_open(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "| exit 1 | PASS |", "| exit 1 | **FAIL** |")
    _, markup = generate(repo, base, gh)
    assert '<details class="check" open>' in check_block(markup, "A2"), "a failed check stays open"
    for anchor in ('id="check-A2"', 'id="review-open-1"', '<article class="decision"', 'id="limits"'):
        assert all(" open" in attrs for attrs in enclosing_folds(markup, anchor)), anchor


def a_list_past_the_threshold_folds(base):
    repo, evidence, gh = ready_branch(base)
    rows = "".join(f"| Z{n} | AC1 | unit | row {n} | + | x | PASS | [o]({evidence}/U1.txt) |\n" for n in range(9))
    report_edit(repo, "validation", "\n## Test types", rows + "\n## Test types")
    _, markup = generate(repo, base, gh)
    automated = re.search(r'<div id="automated".*?(?=<div id="manual")', markup, re.S).group(0)
    assert re.search(r'<details class="more"><summary><b>[^<]*</b> <span class="n">11</span>', automated), "eleven passing rows aren't folded with their count"


# The verdict never reads Ready past something it can't vouch for

READY_GAP = "- The receipt on a thermal printer — If it goes wrong: a shopper can't read the coupon's name — Worst case: support answers a question — Checked by: P1, after the merge"
READY_DECISION = "  Accepts: A.\n"


def ready_with_rows(base, change):
    """The clean branch with its validation rows changed, stamped again."""
    repo, evidence, gh = ready_branch(base)
    report_edit(repo, "validation", ready_rows(evidence), change(evidence))
    stamp(repo, "validate")
    return repo, evidence, gh


def no_ci_and_the_ci_phase_off_are_not_stops(base):
    repo, _, gh = ready_branch(base)
    nothing = {"statusCheckRollup": None}
    gh["checks"] = {}
    gh["no_ci"] = {"defaultBranchRef": {"target": nothing}, "parent": nothing, "pullRequests": {"nodes": []}}
    markup = generate(repo, base, gh)[1]
    assert footer_value(markup, "CI").startswith("no CI"), footer_value(markup, "CI")
    assert_verdict(markup, "Ready")
    out = sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", "ci")
    assert out.returncode == 0, out
    markup = generate(repo, base, gh)[1]
    assert footer_value(markup, "CI").startswith("CI phase off"), footer_value(markup, "CI")
    assert_verdict(markup, "Ready")


def a_failed_verify_is_not_ready_and_one_that_checked_nothing_is_your_call(base):
    repo, _, gh = ready_branch(base)
    write(repo, "verify", "# Verify: FAIL (exit 1)\n")
    assert_verdict(generate(repo, base, gh)[1], "Not ready", "verify failed")
    write(repo, "verify", "# Verify: PASS, but nothing ran\n")
    assert_verdict(generate(repo, base, gh)[1], "Your call", "checked nothing")


def each_missing_impact_field_is_your_call(base):
    for n, (kind, field) in enumerate((("review", " — If it goes wrong: a shopper sees the wrong currency"), ("review", " — Worst case: a wrong total on a mixed cart"),
                                       ("review", " — Can wait because: the shop sells in one currency today"),
                                       ("validation", " — If it goes wrong: a shopper can't read the coupon's name"), ("validation", " — Worst case: support answers a question"),
                                       ("validation", " — Checked by: P1, after the merge"))):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        report_edit(repo, kind, field, "")
        stamp(repo, "review", "validate")
        assert_verdict(generate(repo, here, gh)[1], "Your call", "no recorded impact")


def every_old_way_of_naming_a_gap_is_listed(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "## Not proven", "Couldn't run before the merge: the real launchctl.\nCouldn't run here: the installed plugin.\nCouldn't run locally: the live pane.\nCouldn't run: nothing.\n\n## Not proven")
    stamp(repo, "validate")
    said = text(page_part(generate(repo, base, gh)[1], "limits"))
    assert "the real launchctl" in said and "the installed plugin" in said and "the live pane" in said, said
    assert said.count("Impact not recorded") == 6, said  # two columns each for those three: a fourth row would be the "nothing" line


def a_real_gap_starting_with_none_or_nothing_is_kept(base):
    for n, gap in enumerate(("None of the e2e tests reach the refund path", "Nothing exercises the refund path")):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        report_edit(repo, "validation", READY_GAP, f"{READY_GAP}\n- {gap} — If it goes wrong: a refund fails — Worst case: money is lost — Checked by: nothing — Blocking: yes")
        stamp(repo, "validate")
        assert_verdict(generate(repo, here, gh)[1], "Not ready", gap)


def the_sentinels_still_mean_nothing_to_say(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", READY_GAP, "- None.")
    stamp(repo, "validate")
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Ready")
    assert text(page_part(markup, "limits")).startswith("Not proven No gap recorded."), text(page_part(markup, "limits"))[:200]
    for n, word in enumerate(("None", "None.", "Nothing to decide.", "N/A")):
        here = os.path.join(base, str(n))
        repo, _, gh = ready_branch(here, notes=READY_NOTES[:READY_NOTES.index("## Decisions")] + f"## Decisions\n{word}\n")
        markup = generate(repo, here, gh)[1]
        assert rail_of(markup)["Your call"][0] == "nothing to decide", (word, rail_of(markup))


def every_way_of_writing_blocking_blocks(base):
    for n, mark in enumerate(("Blocking: yes, until P1 passes", "Blocking: true", "**Blocking:** Yes.", "blocking: YES")):
        here = os.path.join(base, f"open{n}")
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        report_edit(repo, "review", "one currency today", f"one currency today — {mark}")
        stamp(repo, "review")
        assert_verdict(generate(repo, here, gh)[1], "Not ready", "two currencies collapse")
    for n, tail in enumerate((" Blocking: yes", "\n  Blocking: yes", "\nBlocking: yes")):
        here = os.path.join(base, f"gap{n}")
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        report_edit(repo, "validation", READY_GAP, READY_GAP + "." + tail)
        stamp(repo, "validate")
        assert_verdict(generate(repo, here, gh)[1], "Not ready", "thermal printer")
    for n, notes in enumerate((READY_NOTES.replace("- Keep the refused-coupon log line", "- Keep the refused-coupon log line — Blocking: yes"),
                               READY_NOTES.replace(READY_DECISION, READY_DECISION + "Blocking: yes\n"))):
        here = os.path.join(base, f"dec{n}")
        os.makedirs(here)
        repo, _, gh = ready_branch(here, notes=notes)
        assert_verdict(generate(repo, here, gh)[1], "Not ready", "refused-coupon")


def blocking_no_does_not_block(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "one currency today", "one currency today — Blocking: no")
    report_edit(repo, "validation", READY_GAP, READY_GAP + " — Blocking: No.")
    stamp(repo, "review", "validate")
    assert_verdict(generate(repo, base, gh)[1], "Ready")


def a_decision_starting_with_merge_keeps_its_blocking_mark(base):
    notes = READY_NOTES.replace("- Keep the refused-coupon log line", "- Merge the two coupon tables first?").replace(READY_DECISION, READY_DECISION + "  Blocking: yes\n")
    repo, _, gh = ready_branch(base, notes=notes)
    assert_verdict(generate(repo, base, gh)[1], "Not ready", "Merge the two coupon tables")


def a_second_decisions_heading_adds_to_the_first(base):
    notes = READY_NOTES + "\n## Decisions\n- Another choice\n  Blocking: yes\n"
    repo, _, gh = ready_branch(base, notes=notes)
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Not ready", "Another choice")
    assert "refused-coupon log line" in page_part(markup, "decide")


def an_unreadable_report_is_your_call(base):
    for n, kind in enumerate(("checks-guide", "decision-notes", "follow-pr")):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        with open(path_of(repo, kind), "wb") as fh:
            fh.write(b"\xff\xfe a FAIL nobody can read")
        assert_verdict(generate(repo, here, gh)[1], "Your call", "read")


def evidence_on_another_commit_is_your_call(base):
    repo, evidence, gh = ready_branch(base)
    with open(os.path.join(evidence, "A1.txt")) as fh:
        output = fh.read()
    with open(os.path.join(evidence, "A1.txt"), "w") as fh:
        fh.write(re.sub(r"tree \w+$", "tree 1111111111111111111111111111111111111111", output, count=1, flags=re.M))
    stamp(repo, "validate")
    markup = generate(repo, base, gh)[1]
    assert "This evidence is stale" in markup
    assert not verdict_of(markup).startswith("Ready"), verdict_of(markup)


def evidence_that_names_no_commit_is_your_call(base):
    repo, evidence, gh = ready_branch(base)
    with open(os.path.join(evidence, "A1.txt"), "w") as fh:
        fh.write("no header\n")
    stamp(repo, "validate")
    assert_verdict(generate(repo, base, gh)[1], "Your call", "Coupon applied once")


def a_check_whose_only_evidence_is_a_video_is_not_flagged_for_freshness(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace(f"[output]({ev}/A3.txt), ", ""))
    assert_verdict(generate(repo, base, gh)[1], "Ready")


def a_lens_that_failed_is_your_call_unless_switched_off(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 1 | open=1 |\n| Security | codex | not run: exit 1 | — | — |")
    stamp(repo, "review")
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Your call", "Security")
    assert "Every lens ran" not in markup


def a_report_with_no_lenses_table_does_not_say_every_lens_ran(base):
    repo, _, gh = ready_branch(base)
    with open(path_of(repo, "review")) as fh:
        body = fh.read()
    write(repo, "review", body[:body.index("## Lenses")].replace("Not run:   security lens — no auth code touched\n", ""))
    stamp(repo, "review")
    assert "Every lens ran" not in generate(repo, base, gh)[1]


def a_test_type_marked_not_run_is_your_call(base):
    repo, _, gh = ready_branch(base)
    with open(path_of(repo, "validation")) as fh:
        body = fh.read()
    changed = body.replace("| e2e | yes | A1 |", "| e2e | no | the runner was down |")
    assert changed != body
    write(repo, "validation", changed)
    stamp(repo, "validate")
    assert_verdict(generate(repo, base, gh)[1], "Your call", "e2e")


def a_merged_or_closed_pr_is_not_ready(base):
    repo, _, gh = ready_branch(base)
    gh["pr"]["state"] = "MERGED"
    assert_verdict(generate(repo, base, gh)[1], "Not ready", "merged")


def evidence_outside_the_directory_is_your_call(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace(f"[output]({ev}/A1.txt)", "[output](/etc/hosts)"))
    assert_verdict(generate(repo, base, gh)[1], "Your call", "Coupon applied once")


def criteria_no_check_names_are_your_call(base):
    repo, evidence, gh = ready_with_rows(base, lambda ev: re.sub(r"^(\| \w+ \|) [^|]* \|", r"\1", ready_rows(ev), flags=re.M))
    report_edit(repo, "validation", "| # | AC | Level |", "| # | Level |")
    report_edit(repo, "validation", "|---|---|---|---|---|---|---|---|", "|---|---|---|---|---|---|---|")
    stamp(repo, "validate")
    assert_verdict(generate(repo, base, gh)[1], "Your call", "criterion")


def the_rule_on_the_page_says_what_the_code_does(base):
    repo, _, gh = ready_branch(base)
    why = text(re.search(r'<details[^>]*id="verdict-why".*?</details>', generate(repo, base, gh)[1], re.S).group(0))
    for phrase in ("running", "HEAD", "verify", "lens", "unreadable", "marked as blocking"):
        assert phrase.lower() in why.lower(), phrase
    assert "Nothing else to read" not in why


# The control panel says what the sources say

def the_rail_and_cards_come_from_their_sources(base):
    repo, _, gh = ready_branch(base)
    markup = generate(repo, base, gh)[1]
    rail, cards = rail_of(markup), cards_of(markup)
    assert rail["Review"] == ("open", "1 finding left open", "wait"), rail["Review"]
    assert cards["Undo"] == "Revert the pull request.", cards
    assert cards["Your experience"] != cards["Stability"], cards
    assert rail["Validate"] == ("passed", "5 of 5 checks passed", "pass"), rail["Validate"]


def the_rail_turns_on_a_failure_and_on_missing_sources(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("| one discount line | PASS |", "| one discount line | FAIL |"))
    rail = rail_of(generate(repo, base, gh)[1])
    assert rail["Validate"] == ("failed", "1 of 5 checks failed", "fail"), rail["Validate"]
    here = os.path.join(base, "gone")
    os.makedirs(here)
    repo, _, gh = ready_branch(here)
    report_edit(repo, "validation", " — Worst case: support answers a question", "")
    stamp(repo, "validate")
    markup = generate(repo, here, gh)[1]
    assert "thermal printer a shopper can't read the coupon's name Impact not recorded" in text(page_part(markup, "limits")), text(page_part(markup, "limits"))[:400]
    here = os.path.join(base, "none")
    os.makedirs(here)
    repo, _, gh = ready_branch(here)
    os.remove(path_of(repo, "validation"))
    rail = rail_of(generate(repo, here, gh)[1])
    assert rail["Validate"][0] == "missing" and rail["Validate"][2] != "pass", rail["Validate"]


def a_not_run_check_is_not_counted_as_run(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("| one discount line | PASS |", "| one discount line | NOT RUN |"))
    markup = generate(repo, base, gh)[1]
    lead = text(re.search(r'<div id="manual">.*?</p>', markup, re.S).group(0))
    assert "ran 2 tests" in lead and "2 passed" in lead and "1 did not run" in lead, lead
    assert_verdict(markup, "Your call", "Coupon applied once")


def a_panel_row_holds_no_nested_link(base):
    notes = READY_NOTES.replace("To undo: Revert the pull request.", "To undo: Revert https://github.com/acme/shop/pull/7 now.")
    repo, _, gh = ready_branch(base, notes=notes)
    markup = generate(repo, base, gh)[1]
    first_screen = markup[:markup.index('<section class="sec"')]
    assert "pull/7" in first_screen, "the undo line isn't on the first screen"
    inside = 0
    for tag in re.findall(r"<a\b|</a>", first_screen):
        inside += 1 if tag == "<a" else -1
        assert inside in (0, 1), "a link sits inside another link on the first screen"


def a_funnel_with_no_counts_shows_no_zeros(base):
    repo, _, gh = ready_branch(base)
    with open(path_of(repo, "review")) as fh:
        body = fh.read()
    write(repo, "review", re.sub(r"(?m)^\| (\w+) \| (\w+) \| \d+ \| \d+ \|.*$", r"| \1 | \2 | not run: exit 1 | — | — |", body))
    stamp(repo, "review")
    part = review_section(generate(repo, base, gh)[1])
    matrix = re.search(r'<table class="mx num" id="review-matrix">.*?</table>', part, re.S).group(0)
    rows = [text(r) for r in re.findall(r"<tr><th scope=\"row\">.*?</tr>", matrix, re.S)]
    assert rows == ["Correctness — — — — claude: exit 1; codex: exit 1", "Tests — — — — claude: exit 1"], rows
    assert "The lenses raised" not in text(part), "a count no lens reported was invented"


def a_caught_planted_bug_and_a_survivor_look_different(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("21/21 killed", "20/21 killed"))
    markup = generate(repo, base, gh)[1]
    assert "<li class=miss>" in markup
    assert re.search(r"\.dots \.miss\{[^}]*\}", markup), "a survivor is drawn like a caught bug"


def an_output_that_cannot_be_read_is_your_call(base):
    repo, evidence, gh = ready_branch(base)
    unreadable = os.path.join(evidence, "A1.txt")
    os.chmod(unreadable, 0)
    try:
        assert_verdict(generate(repo, base, gh)[1], "Your call", "Coupon applied once")
    finally:
        os.chmod(unreadable, 0o644)


def a_huge_svg_costs_no_time(base):
    head = b'<svg xmlns="http://www.w3.org/2000/svg"><rect/>'
    for n, body in enumerate((b"animation" * 100_000, b"<![CDATA[" + b"<!--" * 400_000 + b"]]>")):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, evidence, gh = ready_branch(here, notes=READY_NOTES.replace(SCENE_SPOT, SCENE_SPOT + "\n\n![Big.](big.svg)"))
        put(os.path.join(evidence, "big.svg"), head + body + b"</svg>")
        before = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
        generate(repo, here, gh)
        spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - before
        assert spent < 5, f"{spent:.1f} s of CPU on a {len(body)} byte SVG"


def a_check_naming_no_file_is_your_call(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace(f"[output]({ev}/A1.txt) [video]({ev}/A1.webm)", "looks good"))
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Your call", "Coupon applied once")
    assert "Every check&#x27;s evidence covers" not in markup


def a_screenshot_is_not_claimed_to_cover_a_commit(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace(f"[output]({ev}/A3.txt), ", ""))
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Ready")
    said = text(re.search(r'<p id="freshness">.*?</p>', markup, re.S).group(0))
    assert "Every output" in said and "1 check" in said and "screenshot or video" in said, said


def a_full_gap_named_with_an_old_label_counts_once(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", READY_GAP, READY_GAP.replace("- The receipt", "- Only unit-tested: the receipt"))
    stamp(repo, "validate")
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Ready")
    assert text(page_part(markup, "limits")).count("the receipt on a thermal printer") == 1


def the_same_old_gap_in_a_section_and_on_a_line_counts_once(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "validation", "## Not proven", "## Couldn't run locally\n\n- Couldn't run locally: the live pane\n\n## Not proven")
    stamp(repo, "validate")
    assert text(page_part(generate(repo, base, gh)[1], "limits")).count("the live pane") == 1


def a_wrapped_open_line_keeps_its_blocking_mark(base):
    for n, wrapped in enumerate(("Open:      two totals — uncertain — Worst case: b\n           — Blocking: yes",
                                 "Open:\n- two totals — uncertain — Worst case: b\n  — Blocking: yes")):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, _, gh = ready_branch(here)
        with open(path_of(repo, "review")) as fh:
            body = fh.read()
        changed = re.sub(r"^Open: .*$", lambda _: wrapped, body, flags=re.M)
        assert changed != body
        write(repo, "review", changed)
        stamp(repo, "review")
        assert_verdict(generate(repo, here, gh)[1], "Not ready", "two totals")


def a_reviewer_that_failed_on_a_not_run_line_is_your_call(base):
    repo, _, gh = ready_branch(base)
    report_edit(repo, "review", "Not run:   security lens — no auth code touched", "Not run:   codex correctness lens — timed out twice")
    stamp(repo, "review")
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Your call", "timed out twice")
    assert rail_of(markup)["Review"][2] != "pass", rail_of(markup)["Review"]


def a_rail_step_is_not_passed_when_it_holds_the_verdicts_reason(base):
    def word_of(markup, label):
        return "Clear" if rail_of(markup)[{"Testing": "Validate", "Code review": "Review"}[label]][2] == "pass" else "Read this"

    def broken(name, change):
        here = os.path.join(base, name)
        os.makedirs(here)
        repo, evidence, gh = ready_branch(here)
        change(repo, evidence)
        return generate(repo, here, gh)[1]

    def type_not_run(repo, _):
        report_edit(repo, "validation", "| e2e | yes | A1 |", "| e2e | no | the runner was down |")
        stamp(repo, "validate")

    def no_ac_named(repo, evidence):
        report_edit(repo, "validation", ready_rows(evidence), ready_rows(evidence).replace("AC3", "AC1"))
        stamp(repo, "validate")

    def lens_failed(repo, _):
        report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 1 | open=1 |\n| Security | codex | not run: exit 1 | — | — |")
        stamp(repo, "review")

    clean = broken("clean", lambda repo, evidence: None)
    assert word_of(clean, "Testing") == "Clear", word_of(clean, "Testing")
    for name, change in (("type", type_not_run), ("ac", no_ac_named), ("stale", lambda repo, _: put(os.path.join(repo, "later.txt"), "x"))):
        markup = broken(name, change)
        assert word_of(markup, "Testing") == "Read this", (name, word_of(markup, "Testing"))
    def nothing_left_open(repo):
        with open(path_of(repo, "review")) as fh:
            lines = fh.read().split("\n")
        put(path_of(repo, "review"), "\n".join(line for line in lines if not line.startswith(("Open:", "Not run:"))))
        stamp(repo, "review")

    assert word_of(broken("lens", lens_failed), "Code review") == "Read this"
    settled = broken("settled", lambda repo, _: nothing_left_open(repo))
    assert word_of(settled, "Code review") == "Clear", word_of(settled, "Code review")
    one_lens_down = broken("down", lambda repo, _: (nothing_left_open(repo), lens_failed(repo, None)))
    assert word_of(one_lens_down, "Code review") == "Read this", "a lens that didn't run leaves the row unread, even with nothing open"
    worded = broken("worded", lambda repo, evidence: (report_edit(repo, "validation", "one discount line | PASS |", "one discount line | FAILED |"), stamp(repo, "validate")))
    assert word_of(worded, "Testing") == "Read this", "a result the page can't count isn't a clear row"


def no_decisions_needed_is_no_decision(base):
    repo, _, gh = ready_branch(base, notes=READY_NOTES[:READY_NOTES.index("## Decisions")] + "## Decisions\nNo decisions needed.\n")
    markup = generate(repo, base, gh)[1]
    assert_verdict(markup, "Ready")
    assert rail_of(markup)["Your call"] == ("nothing to decide", "", "you"), rail_of(markup)


def a_result_the_page_cannot_read_is_your_call(base):
    for word in ("FAILED", "Fail", "Not run", "SKIPPED"):
        here = os.path.join(base, word.replace(" ", ""))
        os.makedirs(here)
        repo, _, gh = ready_with_rows(here, lambda ev: ready_rows(ev).replace("one discount line | PASS |", f"one discount line | {word} |"))
        assert_verdict(generate(repo, here, gh)[1], "Your call", "Coupon applied once", word)


def a_huge_count_costs_no_time_and_breaks_nothing(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("21/21 killed", "9" * 16000 + " killed"))
    started = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime
    markup = generate(repo, base, gh)[1]
    spent = resource.getrusage(resource.RUSAGE_CHILDREN).ru_utime - started
    assert spent < 5, f"{spent:.1f} s of CPU"  # CPU, not the wall clock: the workers share the machine
    assert "Planted bugs" in markup
    here = os.path.join(base, "lens")
    os.makedirs(here)
    repo, _, gh = ready_branch(here)
    report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | " + "9" * 5000 + " | 1 | open=1 |")
    stamp(repo, "review")
    assert_verdict(generate(repo, here, gh)[1], "Your call", "Tests")


def animated_svg_stays_still_however_it_is_written(base):
    for n, svg in enumerate(('<svg xmlns="http://www.w3.org/2000/svg"><style>rect{animation:x 1s infinite!important}</style><rect/></svg>',
                             '<SVG xmlns="http://www.w3.org/2000/svg"><rect><ANIMATE attributeName="x"/></rect></SVG>',
                             '<svg xmlns="http://www.w3.org/2000/svg" xmlns:s="http://www.w3.org/2000/svg"><rect><s:animate attributeName="x"/></rect></svg>',
                             '<!-- <svg> --><svg xmlns="http://www.w3.org/2000/svg"><style>rect{ANIMATION:x 1s}</style><rect/></svg>')):
        here = os.path.join(base, str(n))
        os.makedirs(here)
        repo, evidence, gh = ready_branch(here, notes=READY_NOTES.replace(SCENE_SPOT, SCENE_SPOT + "\n\n![Moving.](moving.svg)"))
        put(os.path.join(evidence, "moving.svg"), svg)
        markup = generate(repo, here, gh)[1]
        for item in embedded_svgs(markup):
            if b"<rect" in item.lower():
                assert not re.search(rb"(?i)<[\w:]*animate", item), item
                assert re.match(rb"(?i)\s*<svg\b[^>]*><style>\*\{animation:none!important\}", item), item
                assert b"infinite!important" not in item, "an author's !important outlasts the stop rule, so this figure is refused"
        assert "moving.svg" in markup or "Moving." in markup, svg


def a_figure_past_its_limit_never_links_to_a_page(base):
    notes = READY_NOTES.replace("![The cart is touched, checkout could be affected. Legend: filled is touched, outlined could be affected.](map.svg)", "![The map.](big.html)\n![The picture.](pic.png)")
    repo, evidence, gh = ready_branch(base, notes=notes)
    with open(os.path.join(evidence, "big.html"), "w") as fh:
        fh.write("<script>alert(1)</script>" + " " * (3 * 1024 * 1024))  # past the image limit, so it isn't drawn
    os.symlink(os.path.join(evidence, "big.html"), os.path.join(evidence, "pic.png"))
    markup = generate(repo, base, gh)[1]
    assert "big.html" in markup and "pic.png" in markup
    assert not re.search(r'href="[^"]*(?:big\.html|pic\.png)"', markup), "a figure links to a page that runs its own scripts"


def a_deeply_nested_scene_costs_only_itself(base):
    for depth in (900, 960, 985, 995, 60000):  # the parser and the refusal's own printing give up at different depths
        here = os.path.join(base, str(depth))
        os.makedirs(here)
        markup = scene_page(here, {"deep": '{"title":"Deep","alt":"Deep","seconds":' + "[" * depth + "]" * depth + ',"height":100,"shapes":[],"steps":[]}'})
        assert '<ol class="rail"' in markup and "deep.scene.json" in markup and "scene-refused" in markup, depth


def refused_scenes_share_the_embed_budget(base):
    bad = {"title": "Bad", "alt": "Bad", "seconds": -1, "height": 100, "shapes": [], "steps": [{"from": 0, "to": 1, "caption": "&" * 1000}] * 12}
    named = "".join("![Bad](bad.scene.json)\n\n" for _ in range(500))  # about 58 KB each once escaped: 29 MB uncharged
    repo, evidence, gh = ready_branch(base, notes=READY_NOTES.replace(SCENE_SPOT, named))
    put(os.path.join(evidence, "bad.scene.json"), json.dumps(bad))
    markup = generate(repo, base, gh)[1]
    assert len(markup) < 22 * 1024 * 1024, len(markup)
    assert "scene-refused" in markup


def only_the_first_scenes_start_playing_and_a_page_holds_a_bounded_number(base):
    named = "".join(f"![Scene {n}](s{n}.scene.json)\n\n" for n in range(14))
    repo, evidence, gh = ready_branch(base, notes=READY_NOTES.replace(SCENE_SPOT, named))
    for n in range(14):
        put(os.path.join(evidence, f"s{n}.scene.json"), json.dumps(SCENE))
    markup = generate(repo, base, gh)[1]
    players = players_of(markup)
    assert len(players) == 12, len(players)
    assert markup.count("scene-refused") == 2
    for n, player in enumerate(players):
        checked = re.findall(r'class="sw s-(\w+) vh"[^>]*\bchecked', player)
        assert checked == (["ra"] if n < 3 else ["still"]), (n, checked)
    assert ".s-still:checked~.copy-a *,.s-still:checked~.copy-a .prog::after{animation:none}" in markup, "a stopped scene shows its whole picture, not its first frame"


def a_mismatched_fence_forges_no_heading(base):
    def level_of_real(fenced, here):
        os.makedirs(here)
        notes = READY_NOTES.replace("A refresh ran the coupon step twice.", f"A refresh ran the coupon step twice.\n\n#### Real\n\n{fenced}\n\nAfter.")
        repo, _, gh = ready_branch(here, notes=notes)
        markup = generate(repo, here, gh)[1]
        assert not re.search(r"<h\d[^>]*>forged</h\d>", markup)
        return re.search(r"<(h\d)[^>]*>Real</h\d>", markup).group(1)
    assert level_of_real("~~~\n```\n# forged\n~~~", os.path.join(base, "odd")) == level_of_real("~~~\n# forged\n~~~", os.path.join(base, "even"))


def the_phone_rule_for_commands_is_on_every_page(base):
    repo, _, gh = ready_branch(base)
    markup = generate(repo, base, gh)[1]
    assert 'class="player"' not in markup, "this page must have no scene"
    assert re.search(r"@media \(max-width:640px\)\{[^@]*pre\.cmd\{white-space:pre-wrap", stylesheet(markup))


def every_disclosure_shows_it_opens(base):
    repo, _, gh = ready_branch(base)
    style = stylesheet(generate(repo, base, gh)[1])
    assert not re.search(r"(?:^|\})summary\{[^}]*list-style:none", style), "a summary with no marker reads as plain text"
    for hides_its_marker in re.findall(r"(\.\w+)>summary[^{},]*(?:,[^{}]*)?\{[^}]*list-style:none", style):
        assert f"{hides_its_marker}>summary::after" in style, hides_its_marker


def the_player_names_its_controls_for_any_state(base):
    player = players_of(scene_page(base, {"coupon": SCENE}))[0]
    names = re.findall(r'<input type="radio"[^>]*aria-label="([^"]*)"', player)
    assert len(names) == 4 and not any(name.startswith(("Play:", "Restart and play:")) for name in names), names
    assert 'role="group"' in player


GUARD_TESTS = (no_ci_and_the_ci_phase_off_are_not_stops, a_failed_verify_is_not_ready_and_one_that_checked_nothing_is_your_call, each_missing_impact_field_is_your_call, every_old_way_of_naming_a_gap_is_listed, a_real_gap_starting_with_none_or_nothing_is_kept, the_sentinels_still_mean_nothing_to_say, every_way_of_writing_blocking_blocks, blocking_no_does_not_block, a_decision_starting_with_merge_keeps_its_blocking_mark, a_second_decisions_heading_adds_to_the_first, an_unreadable_report_is_your_call, evidence_on_another_commit_is_your_call, evidence_that_names_no_commit_is_your_call, a_check_whose_only_evidence_is_a_video_is_not_flagged_for_freshness, a_lens_that_failed_is_your_call_unless_switched_off, a_report_with_no_lenses_table_does_not_say_every_lens_ran, a_test_type_marked_not_run_is_your_call, a_merged_or_closed_pr_is_not_ready, evidence_outside_the_directory_is_your_call, criteria_no_check_names_are_your_call, the_rule_on_the_page_says_what_the_code_does, the_rail_and_cards_come_from_their_sources, the_rail_turns_on_a_failure_and_on_missing_sources, a_not_run_check_is_not_counted_as_run, a_panel_row_holds_no_nested_link, a_funnel_with_no_counts_shows_no_zeros, a_caught_planted_bug_and_a_survivor_look_different, an_output_that_cannot_be_read_is_your_call, a_huge_svg_costs_no_time, a_check_naming_no_file_is_your_call, a_screenshot_is_not_claimed_to_cover_a_commit, a_full_gap_named_with_an_old_label_counts_once, the_same_old_gap_in_a_section_and_on_a_line_counts_once, a_wrapped_open_line_keeps_its_blocking_mark, a_reviewer_that_failed_on_a_not_run_line_is_your_call, a_rail_step_is_not_passed_when_it_holds_the_verdicts_reason, no_decisions_needed_is_no_decision, a_result_the_page_cannot_read_is_your_call, a_huge_count_costs_no_time_and_breaks_nothing, animated_svg_stays_still_however_it_is_written, a_figure_past_its_limit_never_links_to_a_page, a_deeply_nested_scene_costs_only_itself, refused_scenes_share_the_embed_budget, only_the_first_scenes_start_playing_and_a_page_holds_a_bounded_number, a_mismatched_fence_forges_no_heading, the_phone_rule_for_commands_is_on_every_page, every_disclosure_shows_it_opens, the_player_names_its_controls_for_any_state)


REDESIGN_TESTS = (the_page_holds_its_parts_in_order, a_branch_with_nothing_still_has_its_parts_in_order, a_clean_branch_reads_ready, a_failed_ci_is_not_ready,
                  a_missing_ci_is_not_ready, a_branch_without_a_pr_is_not_ready, a_fail_row_is_not_ready, a_failed_guide_step_is_not_ready, a_missing_review_is_not_ready,
                  a_stale_review_is_not_ready, a_missing_validation_is_not_ready, a_stale_validation_is_not_ready, stale_evidence_is_not_ready,
                  a_local_head_past_the_pr_is_not_ready, a_blocking_open_finding_is_not_ready, a_blocking_gap_is_not_ready, a_blocking_decision_is_not_ready,
                  a_not_run_row_is_your_call, an_open_finding_without_its_impact_is_your_call, a_gap_without_its_impact_is_your_call, a_phase_off_never_blocks,
                  an_unreadable_guide_is_your_call, missing_decision_notes_are_your_call, the_first_screen_holds_no_status_tokens, the_problem_carries_its_fact_sheet, a_background_comes_before_the_problem,
                  missing_facts_are_left_out, the_solution_carries_its_fact_sheet_and_map, a_blank_regression_check_reads_not_checked, the_skills_document_the_new_notes,
                  a_figure_embeds_in_place, an_svg_stays_an_inert_image, a_figure_series_is_a_static_strip, a_single_image_is_not_a_series,
                  an_svg_with_its_own_motion_holds_still, print_shows_the_folds_content, a_lost_frame_costs_that_frame,
                  a_scene_plays_in_place, a_scene_keeps_one_attribute_of_each_kind, a_scene_slides_a_shape_to_its_final_place, a_hostile_scene_is_refused, markup_in_a_scene_is_only_ever_text,
                  a_scene_caption_is_plain_text, a_refused_scene_costs_only_its_figure, text_the_page_cant_write_costs_only_its_figure, a_refused_scene_with_no_readable_captions_says_so,
                  a_missing_scene_costs_only_its_figure, two_scenes_leave_no_duplicate_id, the_pause_rule_covers_the_progress_bar,
                  a_scene_holds_still_without_motion, old_notes_without_a_scene_carry_no_player, a_figure_series_with_a_scene_stops_at_it,
                  the_ship_skill_example_scene_plays,
                  the_review_section_says_who_and_what_each_lens_is_for, the_review_funnel_shares_one_scale_and_explains_the_difference,
                  an_open_finding_shows_its_impact_or_says_it_is_missing, a_legacy_review_keeps_its_findings, a_missing_review_report_is_named,
                  a_stale_review_is_named_in_its_section, reconstructed_counts_are_flagged, the_criteria_map_to_their_proof_and_a_gap_shows,
                  every_link_on_the_page_lands_inside_a_fold_it_can_open, a_guide_step_already_passed_is_not_to_do,
                  the_automated_part_is_short_and_says_what_it_did_not_measure, automated_levels_are_not_manual_tests, a_manual_test_shows_its_proof,
                  evidence_shown_earlier_is_shown_again, two_tests_citing_one_file_each_show_it, each_evidence_exception_says_which_it_is, guide_steps_are_listed_as_to_do,
                  fail_and_not_run_rows_stay_visible, a_passing_report_from_an_earlier_commit_reads_as_stale, evidence_without_a_header_reads_as_unknown,
                  evidence_on_this_commit_reads_as_current, the_layers_overview_names_each_level_present, manual_tests_are_grouped_under_their_layer,
                  a_gap_shows_what_the_user_would_see, old_prose_gaps_read_impact_not_recorded, notes_bullets_in_a_validation_report_are_gaps, no_gap_says_so,
                  a_not_run_check_and_an_uncovered_criterion_are_gaps, a_decision_shows_options_cost_recommendation_and_what_merging_accepts,
                  no_decisions_say_so_and_merge_is_no_decision, a_legacy_decision_keeps_its_text_and_its_merge_note_moves, an_unreadable_image_costs_that_image,
                  an_escaped_backtick_in_a_cell_reads_as_a_backtick, a_result_keeps_its_detail, old_notes_keep_their_narrative, no_section_is_folded_away,
                  long_lists_start_folded_with_their_count, what_blocks_the_merge_stays_open, a_list_past_the_threshold_folds)



# The diagrams: a rail under the verdict, chips led by a check's name, matrices that say unknown, two themes

def rail_of(markup):
    """{step: (state word, detail, tone)}, from the rail under the verdict."""
    found = re.search(r'<ol class="rail"[^>]*>(.*?)</ol>', markup, re.S)
    assert found, "no rail"
    return {text(name): (text(word), text(detail), tone) for tone, name, word, detail in
            re.findall(r'<li class="(\w+)">.*?<span class="nm">(.*?)</span><span class="say"><b>(.*?)</b>(.*?)</span></a>', found.group(1), re.S)}


def cards_of(markup):
    """{title: sentence}, the short cards under the rail."""
    hero_markup = re.search(r'<header class="hero">.*?</header>', markup, re.S).group(0)
    return {text(title): text(body) for title, body in re.findall(r'<div class="box[^"]*"><h2>(.*?)</h2><p>(.*?)</p></div>', hero_markup, re.S)}


def chips_in(markup):
    """Each chip as (tone, link, result word, name, id)."""
    return [(tone, href, text(word), text(name), text(cid)) for tone, href, word, name, cid in
            re.findall(r'<a class="lc (\w)" href="#([\w-]+)"><b>(.*?)</b>(.*?)(?:<span class="id">(.*?)</span>.*?)?</a>', markup, re.S)]


def test_map(markup):
    """{layer: [chip]}, from the test map."""
    assert 'id="test-map"' in markup, "no test map"
    return {text(name): chips_in(row) for name, row in map_rows(markup)}


def map_rows(markup):
    return re.findall(r'<div class="row"><span class="lay">(.*?)</span><div class="chips">(.*?)</div></div>', markup, re.S)


def review_matrix(markup):
    """[(lens, {column: cell})], from the review's matrix."""
    found = re.search(r'<table class="mx num" id="review-matrix">(.*?)</table>', markup, re.S)
    assert found, "no review matrix"
    columns = [text(cell) for cell in re.findall(r'<th scope="col">(.*?)</th>', found.group(1), re.S)][1:]
    return [(text(name), dict(zip(columns, [text(cell) for cell in re.findall(r"<td[^>]*>(.*?)</td>", cells, re.S)])))
            for name, cells in re.findall(r'<tr><th scope="row">(.*?)</th>(.*?)</tr>', found.group(1), re.S)]


def in_dir(base, name):
    here = os.path.join(base, name)
    os.makedirs(here)
    return here


def the_rail_reads_each_phase_from_its_source(base):
    repo, _, gh = ready_branch(base)
    rail = rail_of(generate(repo, base, gh)[1])
    assert list(rail) == ["Spec", "Review", "Validate", "CI", "Your call"], rail
    assert rail["Spec"] == ("written", "3 criteria", "pass"), rail["Spec"]
    assert rail["Review"] == ("open", "1 finding left open", "wait"), rail["Review"]
    assert rail["Validate"] == ("passed", "5 of 5 checks passed", "pass"), rail["Validate"]
    assert rail["CI"] == ("passed", "", "pass"), rail["CI"]
    assert rail["Your call"] == ("1 decision", "", "you"), rail["Your call"]


def the_rail_never_shows_skipped_or_unread_work_as_passed(base):
    def rail_for(name, change, gh_change=None):
        here = in_dir(base, name)
        repo, evidence, gh = ready_branch(here)
        change(repo, evidence)
        if gh_change:
            gh_change(gh, repo)
        return rail_of(generate(repo, here, gh)[1])

    def rows(old, new):
        return lambda repo, evidence: (report_edit(repo, "validation", ready_rows(evidence), ready_rows(evidence).replace(old, new)), stamp(repo, "validate"))

    def phase_off(phase):
        return lambda repo, _: sh(repo, "python3", os.path.join(KIT, "hooks", "phase_switches.py"), "set", "off", phase)
    nothing = lambda repo, evidence: None  # noqa: E731
    expected = {
        "failed": (rows("| one discount line | PASS |", "| one discount line | FAIL |"), None, "Validate", ("failed", "1 of 5 checks failed", "fail")),
        "not-run": (rows("| one discount line | PASS |", "| one discount line | NOT RUN |"), None, "Validate", ("not run", "1 of 5 checks did not run", "wait")),
        "no-video": (nothing, None, "Validate", None),
        "stale": (lambda repo, evidence: put(stamp_file(repo, "validate"), "an older change\n"), None, "Validate",
                  ("stale", "the stamp doesn't cover this commit", "wait")),
        "missing": (lambda repo, evidence: os.remove(path_of(repo, "validation")), None, "Validate", ("missing", "no validation report", "wait")),
        "validate-off": (phase_off("validate"), None, "Validate", ("switched off", "", "off")),
        "review-off": (phase_off("review"), None, "Review", ("switched off", "", "off")),
        "review-stale": (lambda repo, evidence: put(stamp_file(repo, "review"), "an older change\n"), None, "Review",
                         ("stale", "the stamp doesn't cover this commit", "wait")),
        "review-missing": (lambda repo, evidence: os.remove(path_of(repo, "review")), None, "Review", ("missing", "no review report", "wait")),
        "lens-down": (lambda repo, evidence: (report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 1 | open=1 |\n| Security | codex | not run: exit 1 | — | — |"),
                                              stamp(repo, "review")), None, "Review", ("not run", "the codex Security lens did not run", "wait")),
        "spec-missing": (lambda repo, evidence: os.remove(path_of(repo, "spec")), None, "Spec", ("missing", "no spec", "wait")),
        "ci-failed": (nothing, lambda gh, repo: gh["checks"][git(repo, "rev-parse", "HEAD")]["check_runs"][0].update(conclusion="failure"), "CI", ("failed", "", "fail")),
        "ci-pending": (nothing, lambda gh, repo: gh["checks"][git(repo, "rev-parse", "HEAD")]["check_runs"][0].update(status="in_progress", conclusion=None),
                       "CI", ("pending", "", "wait")),
        "ci-unread": (nothing, lambda gh, repo: gh.update(api_error=True), "CI", None),
        "no-pr": (nothing, lambda gh, repo: gh.pop("pr"), "CI", ("missing", "no PR yet", "wait")),
        "ci-off": (phase_off("ci"), None, "CI", ("switched off", "", "off")),
        "no-ci": (nothing, lambda gh, repo: gh.update(checks={}, no_ci={"defaultBranchRef": {"target": {"statusCheckRollup": None}}, "parent": {"statusCheckRollup": None},
                                                                     "pullRequests": {"nodes": []}}), "CI", ("not needed", "this repository runs no CI", "off")),
    }
    for name, (change, gh_change, step, want) in expected.items():
        if name == "no-video":
            here = in_dir(base, name)
            repo, evidence, gh = ready_with_rows(here, lambda ev: ready_rows(ev).replace(f" [video]({ev}/A1.webm)", ""))
            got = rail_of(generate(repo, here, gh)[1])[step]
            assert got == ("missing", "no video for “Coupon applied once”", "wait"), (name, got)
            continue
        got = rail_for(name, change, gh_change)[step]
        if name == "ci-unread":
            assert got[0] == "unavailable" and got[2] == "wait" and "bad gateway" in got[1], (name, got)
            continue
        assert got == want, (name, got)


def the_verdict_lists_each_reason_as_a_chip_led_by_its_name(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("| one discount line | PASS |", "| one discount line | FAIL |"))
    markup = generate(repo, base, gh)[1]
    box = re.search(r'<div class="verdict-box (\w+)"[^>]*>(.*?)</div>', markup, re.S)
    assert box and box.group(1) == "fail", box and box.group(1)
    chips = chips_in(box.group(2))
    assert ("f", "check-A1", "Failed", "Coupon applied once", "A1") in chips, chips
    assert ("w", "review-open-1", "Your call", "open finding: two currencies collapse", "") not in chips, "a finding that doesn't block is not a reason"
    assert "Coupon applied once failed" in verdict_of(markup) and "A1 failed" not in verdict_of(markup), verdict_of(markup)
    here = in_dir(base, "ready")
    repo, _, gh = ready_branch(here)
    markup = generate(repo, here, gh)[1]
    box = re.search(r'<div class="verdict-box (\w+)"[^>]*>(.*?)</div>', markup, re.S)
    assert box.group(1) == "pass" and not chips_in(box.group(2)), box.group(0)


def the_hero_keeps_stability_experience_and_undo_short(base):
    repo, _, gh = ready_branch(base)
    cards = cards_of(generate(repo, base, gh)[1])
    assert cards == {"Stability": "It adds one check on the cart path, and a refused coupon leaves the total as it was.",
                     "Your experience": "A shopper sees one discount line, and nothing else moves.", "Undo": "Revert the pull request."}, cards
    here = in_dir(base, "unrecorded")
    notes = re.sub(r"## Stability\n.*?\n\n## Your experience\n.*?\n\n", "", READY_NOTES, flags=re.S).replace("To undo: Revert the pull request.\n", "")
    repo, _, gh = ready_branch(here, notes=notes)
    markup = generate(repo, here, gh)[1]
    assert cards_of(markup) == {}, cards_of(markup)
    assert "Not recorded" not in re.search(r'<header class="hero">.*?</header>', markup, re.S).group(0), "an empty optional field is still drawn"


def the_cut_blocks_are_gone_and_the_absences_that_matter_stay(base):
    notes = READY_NOTES.replace("How often: Rare. One support ticket on record.\n", "")
    repo, _, gh = ready_branch(base, notes=notes)
    markup = generate(repo, base, gh)[1]
    for gone in ('class="panel"', 'class="layers"', 'class="layername"', 'class="eyebrow"', "<hgroup", 'class="funnel"', "Which layer each test belongs to"):
        assert gone not in markup, gone
    assert "<dt>How often" not in markup, "an optional field with nothing recorded is still drawn"
    # an output's own lines, quoted in <pre> or as a runner line, may name AC1: that is the evidence, not the page's label
    main = re.sub(r'<pre[^>]*>.*?</pre>|<span class="said">.*?</span>', "", markup[markup.index("<main"):markup.index("<footer")], flags=re.S)
    assert not re.search(r"\bAC\d", text(main)), re.findall(r".{40}\bAC\d.{20}", text(main))
    prose = re.search(r'<div class="body" id="problem">.*?(?=<div class="body" id="fix">)', markup, re.S).group(0)
    assert "A refresh ran the coupon step twice." in prose and enclosing_folds(prose, "A refresh ran the coupon step twice.")[-1].startswith(' class="more"'), \
        "the agent's prose is not behind a fold"
    assert '<p class="hl">A refresh gave a second discount</p>' in prose, "the headline is not the problem's one-line card"
    here = in_dir(base, "absent")
    repo, evidence, gh = ready_with_rows(here, lambda ev: ready_rows(ev).replace("| exit 1 | PASS |", "| exit 1 | NOT RUN |")
                                         .replace(f" [video]({ev}/U1.webm)", ""))
    with open(os.path.join(evidence, "A1.txt")) as fh:
        output = fh.read()
    with open(os.path.join(evidence, "A1.txt"), "w") as fh:
        fh.write(re.sub(r"tree \w+$", "tree 1111111111111111111111111111111111111111", output, count=1, flags=re.M))
    stamp(repo, "validate")
    markup = generate(repo, here, gh)[1]
    testing = text(page_part(markup, "testing")).lower()
    for kept in ("not run", "no video", "stale"):
        assert kept in testing, kept


def the_test_map_draws_one_row_per_layer_with_named_chips(base):
    repo, evidence, gh = ready_branch(base)
    put(os.path.join(evidence, "A2.replay"), "")
    markup = generate(repo, base, gh)[1]
    drawn = test_map(markup)
    assert list(drawn) == ["Unit", "End to end", "Mutation", "By hand"], list(drawn)
    assert drawn["End to end"][0] == ("p", "check-A1", "Passed", "Coupon applied once live", "A1"), drawn["End to end"]
    assert drawn["End to end"][1][3] == "Tampered total refused replay", drawn["End to end"]
    assert drawn["By hand"] == [("s", "todo-M1", "To do", "Pay on a phone by hand", "M1"), ("s", "todo-P1", "To do", "Watch the error log by hand", "P1")], drawn["By hand"]
    tally = text(re.search(r'<p class="tally"[^>]*>.*?</p>', markup, re.S).group(0))
    assert tally == "5 checks in the validation report: 5 passed, 0 failed, 0 did not run. 2 steps in the checks guide, to do by hand.", tally
    here = in_dir(base, "mixed")
    repo, evidence, gh = ready_with_rows(here, lambda ev: ready_rows(ev).replace("| one discount line | PASS |", "| one discount line | FAIL |")
                                        .replace("| exit 1 | PASS |", "| exit 1 | NOT RUN |"))
    report_edit(repo, "validation", "Receipt names the coupon", "Receipt shows the coupon")
    markup = generate(repo, here, gh)[1]
    drawn = test_map(markup)
    assert drawn["End to end"][0][:3] == ("f", "check-A1", "Failed") and drawn["End to end"][1][:3] == ("s", "check-A2", "Not run"), drawn["End to end"]
    tally = text(re.search(r'<p class="tally"[^>]*>.*?</p>', markup, re.S).group(0))
    assert tally.startswith("5 checks in the validation report: 3 passed, 1 failed, 1 did not run."), tally
    for chip in re.findall(r'<a class="lc \w" href="#([\w-]+)"', "".join(row for _, row in map_rows(markup))):
        assert f'id="{chip}"' in markup, f"chip {chip} links to nothing"


def the_review_matrix_counts_each_lens_and_says_unknown(base):
    repo, _, gh = ready_branch(base)
    rows = review_matrix(generate(repo, base, gh)[1])
    assert rows == [("Correctness", {"Fixed": "3", "Rejected": "1", "Open": "0", "Pre-existing": "0", "Not run": "Ran"}),
                    ("Tests", {"Fixed": "0", "Rejected": "0", "Open": "1", "Pre-existing": "0", "Not run": "Ran"})], rows
    here = in_dir(base, "unknown")
    repo, _, gh = ready_branch(here)
    report_edit(repo, "review", "| Tests | claude | 1 | 1 | open=1 |", "| Tests | claude | 1 | 1 |  |\n| Security | codex | not run: exit 1 | — | — |")
    report_edit(repo, "review", "| Correctness | codex | 2 | 1 | fixed=1 |", "| Correctness | codex | not run: timed out | — | — |")
    stamp(repo, "review")
    markup = generate(repo, here, gh)[1]
    rows = dict(review_matrix(markup))
    assert rows["Tests"] == {"Fixed": "unknown", "Rejected": "unknown", "Open": "unknown", "Pre-existing": "unknown", "Not run": "Ran"}, rows
    assert rows["Security"]["Not run"] == "codex: exit 1" and rows["Security"]["Fixed"] == "—", rows
    assert rows["Correctness"] == {"Fixed": "2", "Rejected": "1", "Open": "0", "Pre-existing": "0", "Not run": "codex: timed out"}, \
        f"a lens's model that didn't run hides behind the one that did: {rows}"
    assert 'id="review-open-1"' in markup, "an open finding left the page"


def the_regression_matrix_chips_a_check_with_a_result(base):
    notes = READY_NOTES.replace("| Receipt | It prints the coupon | Not checked |", "| Receipt | It prints the coupon | Not checked |\n| Tax | It reads the total | the full suite |\n| Log | It grows | |")
    repo, _, gh = ready_branch(base, notes=notes)
    table = re.search(r'<table class="mx" id="regressions">(.*?)</table>', generate(repo, base, gh)[1], re.S)
    assert table, "no regression matrix"
    rows = {text(name): cell for name, cell in re.findall(r'<tr><th scope="row">(.*?)</th><td[^>]*>.*?</td><td[^>]*>(.*?)</td></tr>', table.group(1), re.S)}
    assert chips_in(rows["Checkout"]) == [("p", "check-A1", "Passed", "Coupon applied once", "A1")], rows["Checkout"]
    assert text(rows["Receipt"]) == "Not checked" and text(rows["Tax"]) == "the full suite" and text(rows["Log"]) == "Not checked", rows
    assert "It reads the cart total" in table.group(1), "a system lost its reason"


def before_and_after_are_paired_only_when_recorded(base):
    table = "### Before and after\n| Case | Before | After |\n|---|---|---|\n| Refresh the cart | Two discounts | One discount |\n\n"
    repo, _, gh = ready_branch(base, notes=READY_NOTES.replace("### Worked example", table + "### Worked example"))
    markup = generate(repo, base, gh)[1]
    paired = re.search(r'<table class="ba"[^>]*>(.*?)</table>', markup, re.S)
    assert paired, "no before and after"
    assert [tuple(text(cell) for cell in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row)) for row in re.findall(r"<tbody>.*?<tr>(.*?)</tr>", paired.group(1), re.S)] == \
        [("Refresh the cart", "Two discounts", "One discount")], paired.group(1)
    here = in_dir(base, "figures")
    figures = "### Before and after\n![Two discount lines.](step1.svg)\n![One discount line.](step2.svg)\n\n"
    repo, _, gh = ready_branch(here, notes=READY_NOTES.replace("### Worked example", figures + "### Worked example"))
    markup = generate(repo, here, gh)[1]
    assert 'class="ba"' not in markup and markup.count('alt="One discount line."') == 1, "figures are not shown as figures"
    here = in_dir(base, "neither")
    repo, _, gh = ready_branch(here)
    assert "Before and after" not in generate(repo, here, gh)[1], "a before and after drawn from nothing"


def a_criterion_is_shown_by_its_text(base):
    repo, _, gh = ready_with_rows(base, lambda ev: ready_rows(ev).replace("| A3 | AC3 |", "| A3 | AC1 |"))
    markup = generate(repo, base, gh)[1]
    assert "A coupon applies once." in text(re.search(r'<p class="meta">.*?</p>', check_block(markup, "A1"), re.S).group(0)), check_block(markup, "A1")[:600]
    rows = [text(row) for row in re.findall(r'<tr id="ac-\d+"><th scope="row">(.*?)</th>', markup, re.S)]
    assert rows == ["A coupon applies once.", "A tampered total is refused.", "The receipt names the coupon."], rows
    assert_verdict(markup, "Your call", "no check proves “The receipt names the coupon”")


def both_themes_follow_the_system(base):
    repo, _, gh = ready_branch(base)
    markup = generate(repo, base, gh)[1]
    style = re.search(r"<style>(.*?)</style>", markup, re.S).group(1)
    assert "color-scheme:light dark" in style and "@media (prefers-color-scheme:dark)" in style, "one theme only"
    for token in ("--bg:#FCFCFC", "--bg:#0A0A0A", "--fail:#C42029", "--fail:#FB414A"):
        assert token in style, token
    assert "<script" not in markup and "coral" not in style.lower()


DIAGRAM_TESTS = (the_rail_reads_each_phase_from_its_source, the_rail_never_shows_skipped_or_unread_work_as_passed,
                 the_verdict_lists_each_reason_as_a_chip_led_by_its_name, the_hero_keeps_stability_experience_and_undo_short,
                 the_cut_blocks_are_gone_and_the_absences_that_matter_stay, the_test_map_draws_one_row_per_layer_with_named_chips,
                 the_review_matrix_counts_each_lens_and_says_unknown, the_regression_matrix_chips_a_check_with_a_result,
                 before_and_after_are_paired_only_when_recorded, a_criterion_is_shown_by_its_text, both_themes_follow_the_system)


TESTS = kit_home.selected((writes_the_page_with_its_csp_and_prints_its_path, bounds_what_it_embeds,
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
                     a_failure_is_never_folded_away, the_review_line_names_a_skipped_reviewer, the_tests_line_counts_each_level,
                     levels_come_from_the_level_column, levels_fall_back_to_test_types, a_check_opens_to_steps_expected_actual_evidence,
                     a_long_log_shows_its_tail, a_check_proving_many_criteria_counts_them, a_video_plays_inline, a_suite_output_lists_its_tests, the_review_shows_each_lens,
                     a_review_without_lenses_says_not_recorded, merge_and_ship_reads_ship_md, a_partial_or_missing_ship_md,
                     reports_are_annexes_without_built_from, the_skills_ask_for_the_new_formats,
                     runner_lines_take_linear_time, hostile_text_in_the_new_parts_renders_inert,
                     a_ship_md_marker_in_a_table_header_keeps_its_tags, unrun_optional_steps_are_still_to_do,
                     a_failed_step_after_the_merge_is_a_blocker, test_types_heading_and_ran_in_any_form, levels_map_aliases_and_stay_bounded,
                     a_shared_suite_output_counts_once, steps_past_the_total_are_linked, the_notes_count_toward_the_total,
                     a_comment_before_the_first_paragraph_is_skipped, triage_lens_names_get_their_brief,
                     coverage_without_an_ac_column_is_not_recorded, a_merge_order_item_is_not_the_merge_note,
                     a_check_id_with_punctuation_gets_its_level, lens_names_split_in_linear_time,
                     report_copies_past_the_total_leave_nothing_embedded, references_link_to_github_and_local_files,
                     a_missing_verify_report_says_how_to_make_it, an_autolink_takes_linear_time, the_test_map_holds_each_test_in_order,
                     a_layer_without_tests_says_so, the_coverage_shows_each_criterion_short, a_level_not_run_shows_once,
                     the_review_line_counts_lenses_and_rounds_apart, a_merge_item_in_the_notes_is_the_recommendation,
                     several_nits_show_one_per_line, references_link_only_what_the_pr_head_holds,
                     a_missing_verify_report_is_your_call, switching_verify_off_raises_no_verify_blocker, the_summary_parsers_take_linear_time,
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
                     a_marker_inside_a_longer_fence_is_not_the_mode, a_kept_script_the_evidence_names_shows_once,
                     notes_over_the_limit_keep_the_change_page, a_file_a_row_names_three_ways_shows_once,
                     a_failed_checks_raw_evidence_folds_but_its_outcome_stays_open,
                     a_log_and_its_video_show_as_tabs_log_first_unless_passed, a_second_video_of_the_same_stem_stays_beside_the_tabs,
                     a_log_or_video_alone_or_with_another_stem_is_not_tabs,
                     a_replay_says_so_and_a_check_without_a_video_is_your_call) + REDESIGN_TESTS + GUARD_TESTS + DIAGRAM_TESTS)


def run_test(test):
    base = tempfile.mkdtemp(prefix="agents-test-decision-report-")
    try:
        test(os.path.realpath(base))
        return test.__name__, None
    except (Exception, SystemExit) as error:  # noqa: BLE001 - report every failure, keep running the rest; a test calling exit must not pass
        return test.__name__, f"{type(error).__name__}: {str(error)[:600]}"
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(os.path.expanduser("~/.agents/repos/shop"), ignore_errors=True)
        if os.path.isdir(QUALITY_LOG):
            os.rmdir(QUALITY_LOG)
        elif os.path.lexists(QUALITY_LOG):
            os.remove(QUALITY_LOG)


# Run one after another, this file alone took 12 minutes. A process pool, not threads, since each worker needs its own
# HOME; its broken-pool error also fails the run if a test kills its worker, where a plain Pool would wait forever.
try:
  with concurrent.futures.ProcessPoolExecutor(max_workers=min(8, os.cpu_count() or 1), mp_context=multiprocessing.get_context("fork"),
                                            initializer=make_own_home) as pool:
    results = list(pool.map(run_test, TESTS))
except concurrent.futures.BrokenExecutor as error:
  print(f"FAIL a test killed its worker: {error}")
  shutil.rmtree(SUITE_DIR, ignore_errors=True)
  raise SystemExit(1)
for name, error in results:
    print(f"{'FAIL' if error else 'ok  '} {name}")
    if error:
        print(f"     {error}")
shutil.rmtree(SUITE_DIR, ignore_errors=True)
raise SystemExit(1 if any(error for _, error in results) else 0)
