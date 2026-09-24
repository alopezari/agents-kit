# Review comment taxonomy

Assign exactly one primary `cat` (the most specific that fits). Codes:

## Candidate practices (from engineering-practices.md)
- P1.1 overengineering — speculative generality, unused options/params, YAGNI
- P1.2 wrong-abstraction — forced DRY, shared helper bent with flags/conditionals for a new case
- P1.3 needless-indirection — shallow wrappers, too many tiny functions/classes/layers
- P1.4 simpler-approach — "this could be simpler / just do X"
- P2.1 chesterton — removed or changed something that existed for a reason
- P2.2 scope-creep — unrelated changes, refactor mixed with behavior change, PR should be split
- P2.4 contract-break — backwards compat, public API/hook/filter/schema/output format change, consumers affected
- P2.5 rewrite — rewrote instead of minimal edit
- P3.1 error-handling — swallowed errors, missing error handling, wrong error propagation
- P3.2a missing-validation — missing input validation/sanitization at a boundary, unhandled null/edge case
- P3.2b excessive-defensive — unnecessary checks for impossible cases
- P3.3 silent-fallback — hidden fallback paths, defaults that mask failures
- P4.2 weakened-test — test deleted/skipped/loosened
- P4.3 test-quality — tests testing mocks/implementation, weak assertions
- P4.5 missing-tests — needs tests / coverage
- P5.1 observability — logging, metrics, debuggability
- P5.2 dependency — new/unneeded dependency, version
- P5.3 performance — queries, loops, caching, N+1, bundle size
- P6.1 readability — hard to follow, complex conditionals
- P6.3 comments — unnecessary/missing/outdated comments or docblocks

## Other categories
- C.bug — logic/correctness bug (wrong behavior, off-by-one, wrong condition, race)
- C.security — security/privacy/permissions/escaping/nonce/capabilities
- C.reuse — duplicated existing helper/util/component; "use existing X"
- C.convention — doesn't follow repo/framework conventions or patterns (not naming)
- C.naming — naming
- C.types — typing issues (TS/PHP types)
- C.i18n-a11y — translations, accessibility
- C.ux — product/UX/copy/design feedback
- C.docs-process — changelog, PR description, docs, readme, release process
- C.dead-code — unused code/imports/leftovers/debug statements
- C.config-build — CI, build, config, env
- C.wp-specific — WordPress/WooCommerce-specific API misuse (hooks priority, options, transients, REST, blocks) when not better covered above
- Q.question — genuine question / clarification request with no implied defect
- N.nit — pure style/formatting nit
- A.ack — praise, acknowledgement, "done", FYI
- X.other — none of the above
