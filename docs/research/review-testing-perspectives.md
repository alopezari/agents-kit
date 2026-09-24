# Review and testing perspectives for the local agent pipeline (proposal, Sept 2026)

**Question:** which review and testing perspectives measurably improve quality, at a cost that stays reasonable for a local pipeline? The target is minutes and a bounded token spend per change, not hours.

## Principles (from the evidence)

1. **Decide deterministically what to run, and let models judge only what tools can't.**
   - OpenCodeReview's line: "determinism over non-determinism" for cost-effective agent review.
   - Anthropic runs a deterministic test-impact service in its own agentic CI.
   - In practice, a cheap script reads the diff and picks the perspectives; running every lens on every change is where time and tokens go.
2. **Tools before opinions.** When a perspective has a deterministic check (phpcs security sniffs, semgrep, gitleaks, query counts, axe), run the check and give the model its output to interpret. Asking a model to eyeball the same thing is the expensive and less reliable option.
3. **Adversarial, but selective.**
   - Trail of Bits on mutation testing with agents: use it "as a selective adversarial sensor on changed important code" and have an agent triage survivors. "80% of the insights for 1% of the manual work."
   - The cost warning from the same post: a 5-minute suite with 1,000 mutants takes 83 hours, so scope to the diff.
4. **Properties catch what examples miss.** Anthropic's property-based testing agent found real bugs in widely used libraries. Models are good at inferring invariants from names, docs and call sites. It's worth it for parsers, calculations, comparisons and date logic.
5. **Every perspective has to earn its place.** Log what each lens and test finds and what gets confirmed. A perspective that never produces a confirmed finding over many runs gets merged or dropped. This is what keeps the pipeline sustainable as the models improve.

## Pipeline shape

```
triage (deterministic, seconds)  →  risk tier + which review lenses + which test types, with reasons
verify   (every stop, seconds)   →  lint, types, semgrep, gitleaks, related unit tests, red check
self-review (before PR)          →  the triggered lenses; cross-model on high risk
validate    (before PR)          →  the triggered test types; e2e positive/negative; staging guide
```

**Budgets per tier.** These are guidance, and the logs show when they're exceeded.

| Tier | Review | Testing | Target |
|---|---|---|---|
| Low (docs, tests, config, <50 lines) | Correctness pass | `verify` only | < 5 min |
| Standard | 3–4 triggered lenses, one cross-model pass | `verify`, full unit suite, e2e for the criteria | ≤ 20 min |
| High (auth, payments, data, public API, caching, cron, >400 lines) | All triggered lenses, cross-model per lens | Standard, plus the triggered advanced tests below | ≤ 45 min |

## Review lenses

**Existing (keep):** Correctness (always), Tests, Maintainability, Security, Performance and scale, Compatibility and rollout, UX/accessibility/i18n.

**Proposed additions:**

| Lens | Catches | Trigger (from the diff) |
|---|---|---|
| **Operability** | New jobs, endpoints or integrations you can't observe or debug in production: silent failures, useless error messages, missing logs/metrics, retries without alerts | New cron/Action Scheduler jobs, REST routes, CLI commands, external HTTP calls |
| **Data and privacy** (split out of Compatibility) | Personal data in logs or analytics, retention, destructive or irreversible data changes, migrations without a rollback | Schema/migration files, user or order data access, `delete`/`update` over sets, analytics events |

**Triggers for the existing lenses:**
- **Security:** input handling, auth or capabilities, SQL, files, external requests.
- **Performance:** queries, loops, hooks on `init`/`wp`, caches, cron.
- **Compatibility:** public hooks and filters, REST responses, CLI flags, schema.
- **UX:** JS/CSS/templates.

## Test types

| Type | Catches | Tool | Trigger | Cost |
|---|---|---|---|---|
| Unit + related + red check | Regressions; tests that can't fail | PHPUnit (exists) | Every change | Seconds |
| E2E positive/negative | Behavior vs acceptance criteria | `validate` (exists) | Behavior changes | Minutes |
| **Mutation testing on changed lines** | Weak assertions, untested branches in new code | Infection `--git-diff-lines` (PHP), Stryker incremental (JS). Time-boxed, and an agent triages survivors | High risk with non-trivial logic | 2–10 min |
| **Property-based tests** | Edge cases across the input domain (the ISO week-53 class of bug) | eris (PHPUnit), fast-check (JS) | Parsers, validators, date/version/price calculations | Minutes to write, seconds to run |
| **Query budget** | N+1 and query explosions that pass functional tests | WordPress `SAVEQUERIES` count on the affected page/endpoint, before vs after the change | Performance lens triggered, or queries/loops in the diff | ~1 min |
| **Automated accessibility** | Contrast, labels, roles, keyboard traps | axe-core through the A/B browser tool on changed pages | UI changes | ~1 min per page |
| **Runtime compatibility** | Syntax or APIs unavailable on supported versions | `php -l` on changed files in a `php:7.4-cli` container (for a project that supports PHP 7.4) | Repos with an older minimum PHP | Seconds |
| **Migration round-trip** | Migrations that can't run twice, or can't be rolled back | Run up/down/up on a copy of local data | Schema or migration files | Minutes |

**Deliberately left out:**
- **Visual regression screenshots:** noisy and costly locally. Add them only if UI regressions show up in the review data.
- **Broad fuzzing:** poor return outside parsers.
- **Mutation testing on whole codebases:** hours.

## Measurement

**What to log:**
- Every lens: runs, findings, confirmed findings, time.
- Every advanced test: runs, issues found, time.

**Where it goes:** `~/.agents/logs/quality.jsonl`, reviewed by the monthly job. It recommends dropping or merging lenses with no confirmed findings over ~20 triggered runs, and tightening triggers that fire often with nothing found.

## Proposed rollout

1. **Triage and logging:** a `triage.py` that maps the diff to a tier, lenses and test types, with reasons. The skills read it, and every lens and test logs its outcome.
2. **Cheap deterministic additions:**
   - runtime-compat lint (projects with an older minimum PHP);
   - query budget (WordPress repos);
   - axe on UI changes.
3. **The Operability and Data/privacy lenses.**
4. **The heavier tests, high risk only:** mutation testing on changed lines, and property-based tests for parser/calculation code. Check first that a coverage driver (pcov/Xdebug) is available for Infection.

## Sources

- OpenCodeReview, [Determinism over Non-Determinism for Cost-Effective Agent-Based Code Review](https://arxiv.org/pdf/2608.09290)
- Trail of Bits, [Mutation testing for the agentic era](https://blog.trailofbits.com/2026/04/01/mutation-testing-for-the-agentic-era/)
- Anthropic, [Finding bugs with Claude and property-based testing](https://red.anthropic.com/2026/property-based-testing/)
- Anthropic, [Scaling test impact analysis for agentic coding](https://claude.com/blog/agentic-coding-is-straining-ci-heres-how-we-scaled-test-impact-analysis-at-anthropic)
- [Do Coverage and Mutation Scores of LLM-Generated Test Suites Correlate with Their Effectiveness?](https://arxiv.org/html/2607.22880v1)
- [AutoReview: multi-agent security code review](https://dl.acm.org/doi/10.1145/3696630.3728618)
