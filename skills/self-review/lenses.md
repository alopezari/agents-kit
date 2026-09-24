# Review lenses

Each lens is a complete brief for one reviewer. Pass it verbatim together with the base ref, the change's goal and the evidence instruction from `SKILL.md`.

## Correctness

You are reviewing for behavior bugs only. Ignore style.

Start by filling in these two tables. They are required output, not optional notes: an empty cell or a missing row is itself a finding. Write "none in diff" only after searching for it.

**Paths table.** One row per guard, setting, invariant or dedup check the diff adds or changes. Find every other path to the same outcome (other callers, CLI vs cron, preview/dry-run vs real run, retries, other mutation endpoints, REST vs admin UI) with a code search, not from memory.

| Guard / rule | Path to the same outcome | Enforced there? | Evidence (file:line) |
|---|---|---|---|

**Derived-data table.** One row per new or changed input (setting, option, flag, meta, query param) × every cache, transient, memo, index or stored value that reads it, directly or through a helper. Search for every reader.

| Input | Cache / derived value | Key varies with it, or invalidated on change? | Evidence (file:line) |
|---|---|---|---|

**Spec table**, when a spec exists. One row per acceptance criterion.

| Criterion | Met? | Evidence (test, command output, or file:line) |
|---|---|---|

Then check:
- For persisted settings: does the first save of every value actually persist, including a `false`/empty/default value when the option doesn't exist yet? (WordPress `update_option( $name, false )` on a missing option is a silent no-op.)
- Edge inputs: null, empty string vs missing, zero, boundaries, duplicates, very large values, unusual dates (week 53, DST, month and year boundaries, timezones).
- Failure paths: is a failed query, request or write distinguishable from "nothing found"? Are return values checked? Can a partial failure leave inconsistent state?
- Concurrency and async: stale responses overwriting newer ones, jobs running twice or concurrently, locks shorter than the work.
- Does the change do what the stated goal says, completely?

## Tests

You are reviewing the tests in the diff, and the tests the diff should have.
- Would each new test fail if the implementation were wrong? Look for fixtures that pass by coincidence, loose matchers, and assertions on mocks of the very unit under test.
- Does each new test check the behavior the goal or spec asks for, through the interface a caller sees? A test of an adjacent path, or of internals only, proves nothing about the request. For a bug fix: does the test fail without the fix for the reason the report describes, not on a missing symbol or a setup error?
- Is every behavior change in the diff covered, including the error and edge paths from the Correctness lens?
- Were any tests deleted, skipped or loosened? Each one needs a stated reason.
- Isolation: global state restored, no duplicated setup or teardown the base class already does, no order dependence.
- Are the tests redundant with each other? Duplicates add maintenance without coverage.

## Maintainability

You are reviewing how easy this code will be to change a year from now.
- Reuse: does an existing helper, constant, component or design-system primitive already do this? Conversely, was a shared helper bent with flags to fit one case?
- Simplicity: speculative options or parameters, needless layers, chains of tiny wrappers, whole-file rewrites where an edit would do.
- Text accuracy: is every comment, docblock, README line, help text and log message true of the code as it now stands? Did the change make existing text elsewhere false?
- Names: do they say what things mean in the domain?
- Dead code, debug leftovers, unrelated changes that belong in another PR.
- Does it follow the conventions of neighbouring files?

## Security

You are reviewing as an attacker would.
- Every input from outside (request params, headers, webhooks, files, API responses, stored user content): validated and sanitized on the way in, escaped for its output context on the way out?
- Queries parameterized, no string-built SQL or shell commands with external data.
- Every state-changing action: authorization by capability (not role name) and intent check (nonce/CSRF)?
- Secrets, tokens or personal data in logs, errors, client responses, analytics or URLs?
- Access control on new endpoints, REST routes, AJAX actions and CLI commands; IDOR (acting on another user's object by changing an ID).
- New dependencies: necessary, maintained, pinned?

## Performance and scale

You are reviewing for behavior at 100× today's data and traffic.
- Queries or remote calls inside loops (N+1); missing batching or priming.
- Unbounded work: queries without `LIMIT`/pagination, loops over unbounded sets, payloads without size limits, external calls without timeouts.
- Work added to hot paths: code that runs on every request or page load, global hooks, autoloaded options, middleware.
- Cache behavior: hit rate, stampedes when it expires, memory size.
- Background jobs: batch sizes, retry storms, idempotency.

## Compatibility and rollout

You are reviewing what breaks for existing users, data and callers when this ships.
- Public contracts: function signatures, hooks and filters, REST responses, events, CLI flags, output formats, error messages others may parse.
- Data and schema changes: safe on large tables, safe while old and new code run side by side, reversible?
- Is there a flag or a rollback path for risky behavior changes?
- Deprecate before removing.

## Operability

You are reviewing whether someone on call could tell this is broken in production, and fix it.
- Every new job, endpoint, CLI command or external call: does a failure leave a trace (log, metric, admin notice) that says what failed and for which item, without logging personal data or secrets?
- Silent failure paths: swallowed errors, a job that skips work and reports success, retries without a final alert.
- Error messages a user or operator gets: do they say what to do next?
- Can it be turned off or rolled back without a deploy (flag, setting), when the change is risky?
- Timeouts and retries on external calls: bounded, with backoff, idempotent?

## Data and privacy

You are reviewing what happens to stored data and personal information.
- Personal data (emails, names, addresses, IPs, order details) in logs, analytics events, error messages, URLs or caches, where it shouldn't be.
- Destructive or irreversible data changes (delete, update over sets, schema changes): scoped correctly, backed up or reversible, safe to run twice?
- Migrations: safe on large tables, safe while old and new code both run, with a way back?
- Retention: does new stored data have an owner and an end of life?

## UX, accessibility and i18n

Use only when the diff changes user-facing UI or copy.
- All user-facing strings translatable; no string concatenation that breaks translation.
- Keyboard access, focus management, labels and roles, colour contrast, screen-reader text.
- Loading, empty and error states handled and visible.
- Copy is clear and consistent with the rest of the product; design-system components used instead of raw elements.
