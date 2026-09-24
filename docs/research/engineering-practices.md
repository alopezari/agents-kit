# Practices from real engineers, applied to coding agents

Working list for deciding what goes into `~/.agents/AGENTS.md`. Each practice has:
- **Rule:** phrased so an agent can follow it and a reviewer can check it.
- **Source:** who it comes from.
- **Why for agents:** the typical agent failure it corrects.
- **Status:** ✅ already in AGENTS.md · ➕ candidate · 🔧 better enforced by tooling than prose.

Filter: only practices that change the behavior of a current model. Principles models already follow unprompted, or that are too vague to check ("clean code", "SOLID"), are out.

---

## 1. Simplicity and abstraction

**1.1 Solve today's problem, not the one you imagine for tomorrow.**
Don't add parameters, extension points or generality that no current caller needs.
- Source: Google eng-practices, *What to look for in a code review* (over-engineering); YAGNI (Beck/XP).
- Why for agents: they generalize "just in case": unused options, interfaces with a single implementation.
- Status: ✅ partial ("smallest change"). ➕ make it explicit.

**1.2 Duplication is far cheaper than the wrong abstraction.**
Tolerate two or three copies before extracting. If an existing abstraction needs flags or conditionals to fit a new case, duplicate instead of bending it.
- Source: Sandi Metz, *The Wrong Abstraction*; Dan Abramov, *Goodbye, Clean Code*; AHA (Kent C. Dodds).
- Why for agents: they apply textbook DRY and bolt `if (new_case)` onto shared helpers.
- Status: ➕

**1.3 Deep modules: small interface, lots of functionality behind it.**
Don't split logic into tiny functions or classes that only call each other. Complexity is measured by what the caller has to know.
- Source: John Ousterhout, *A Philosophy of Software Design*.
- Why for agents: misapplied "clean code" produces cascades of 3-line wrappers.
- Status: ➕

**1.4 Do the simplest thing that works. Prefer clearly named functions, even long names, over classes.**
- Source: Armin Ronacher, *Agentic Coding Recommendations*; The Grug Brained Developer; Rob Pike, rules 3 and 4 ("fancy algorithms are slow when n is small").
- Why for agents: simple, explicit code is also what the next agent understands and modifies best.
- Status: ➕

**1.5 Write code that is easy to delete, not easy to extend.**
- Source: tef, *Write code that is easy to delete, not easy to extend*.
- Status: ➕ (can merge with 1.1 and 1.3)

## 2. Changing existing code

**2.1 Chesterton's fence: before removing something odd, find out why it is there.**
Check `git blame`, the PR that introduced it and its tests.
- Source: G. K. Chesterton, adopted in engineering; Titus Winters, *Software Engineering at Google*.
- Why for agents: they "simplify" away workarounds that existed for a real bug.
- Status: ✅ partial ("read history"). ➕ name the delete/simplify case.

**2.2 Separate structural changes from behavioral changes.**
One commit or PR tidies (rename, move, extract) without changing behavior; another changes behavior.
- Source: Kent Beck, *Tidy First?*.
- Why for agents: they mix refactor and fix in one diff, which makes review impossible.
- Status: ✅ partial ("no drive-by refactors"). ➕ allow a preparatory tidy in a separate commit.

**2.3 In legacy code, pin behavior with characterization tests before changing it.**
- Source: Michael Feathers, *Working Effectively with Legacy Code*.
- Status: ➕

**2.4 Hyrum's Law: with enough users, every observable behavior will be depended on by somebody.**
Treat error messages, result ordering, hooks and filters, and output formats as contracts.
- Source: Hyrum Wright, hyrumslaw.com; *Software Engineering at Google*.
- Why for agents: they change output "details" without considering who consumes them. In WordPress and WooCommerce, hooks and filters are public API.
- Status: ✅ partial ("public surfaces"). ➕ add the examples.

**2.5 Don't rewrite from scratch what already works.**
- Source: Joel Spolsky, *Things You Should Never Do*.
- Why for agents: they rewrite whole files instead of editing.
- Status: ➕

## 3. Errors and robustness

**3.1 Don't swallow errors.**
No empty `catch` blocks, and no catch-log-continue. Let the error propagate, or handle it with an explicit decision.
- Source: Ousterhout, "define errors out of existence" (remove the case, don't hide it); common review practice.
- Why for agents: they wrap code in `try/catch` so it "doesn't fail", which hides bugs.
- Status: ➕

**3.2 Validate at system boundaries; trust the interior.**
No defensive checks for impossible cases inside your own code.
- Source: Alexis King, "Parse, don't validate"; common practice.
- Why for agents: they litter code with `if (!x) return` on values that are already guaranteed.
- Status: ➕

**3.3 No silent fallbacks or backwards-compatibility layers nobody asked for.**
- Why for agents: they add "just in case" alternate paths that hide when something goes wrong.
- Status: ➕ (a typical agent failure; no specific classic source)

## 4. Tests and verification

**4.1 Your job is to deliver code you have proven to work.**
Prove it by running it, and include the evidence.
- Source: Simon Willison, *Your job is to deliver code you have proven to work*.
- Status: ✅

**4.2 Never delete, disable or weaken a test to make it pass.**
If you think the test is wrong, say so and explain why.
- Source: Kent Beck, *Augmented Coding: Beyond the Vibes* (warning signs: disabled tests, unrequested features, loops).
- Why for agents: the most documented agent failure, seen in both Claude and GPT.
- Status: ➕ **priority**

**4.3 Test behavior, not implementation or the mock.**
- Source: Kent Beck (TDD); *Software Engineering at Google*, ch. 12 ("test via public APIs").
- Why for agents: they write tests asserting that the mock returned what the mock returns.
- Status: ➕

**4.4 A bug fix comes with a test that fails without the fix.**
- Source: standard practice; Beck.
- Status: ✅

## 5. Production and operations

**5.1 Observability from the first change.**
Error paths should leave useful logs. If this breaks in production, will we find out?
- Source: Charity Majors ("you build it, you run it"); Armin Ronacher (useful logs as a byproduct).
- Status: ✅ partial ("think past the diff")

**5.2 Choose boring technology. Don't add dependencies without justification.**
- Source: Dan McKinley, *Choose Boring Technology*.
- Why for agents: they install libraries for things 10 lines or the stdlib already solve.
- Status: ➕

**5.3 Measure before optimizing.**
- Source: Rob Pike, rules 1 and 2; Knuth.
- Status: ➕ (short)

## 6. Readability and review

**6.1 Optimize code for whoever reads and modifies it next.**
"Too complex" means it can't be understood quickly, or it invites bugs when someone changes it.
- Source: Google eng-practices.
- Status: ✅ implicit (comment rule, "match neighbours")

**6.2 Small, focused PRs.**
- Source: Google eng-practices, *Small CLs*.
- Status: ✅ partial. ➕ say to split them when they grow.

**6.3 Comments: only the why that code cannot express.**
- Source: Ousterhout; your current rule.
- Status: ✅

**6.4 Formatting, style, imports and types: enforced by tools, not prose.**
- Status: 🔧 linters, formatters and a post-edit hook per repo.

---

## Deliberately rejected

- **"Follow Clean Code / SOLID"**: too vague and encourages over-abstraction; replaced by 1.2 and 1.3.
- **"Functions under N lines"**: contradicts 1.3.
- **"Write tests for everything"**: agents already write plenty (Willison). The problem is quality (4.2 and 4.3), not quantity.
- **"Document everything"**: conflicts with the comment rule.

## Next step: experiment with real reviews

Extract the most frequent failures from GitHub review comments (yours and other engineers' in the org). Then cross-check them against this list: confirm the candidates that show up, drop the ones that never do, and add org-specific rules.

## Sources

- Google eng-practices, [What to look for in a code review](https://google.github.io/eng-practices/review/reviewer/looking-for.html) and [The Standard of Code Review](https://google.github.io/eng-practices/review/reviewer/standard.html)
- Sandi Metz, [The Wrong Abstraction](https://sandimetz.com/blog/2016/1/20/the-wrong-abstraction)
- Dan Abramov, [Goodbye, Clean Code](https://overreacted.io/goodbye-clean-code/)
- Simon Willison, [Your job is to deliver code you have proven to work](https://simonwillison.net/2025/Dec/18/code-proven-to-work/)
- Kent Beck, [Augmented Coding: Beyond the Vibes](https://newsletter.kentbeck.com/p/augmented-coding-beyond-the-vibes) and [Tidy First?](https://tidyfirst.substack.com)
- Armin Ronacher, [Agentic Coding Recommendations](https://lucumr.pocoo.org/2025/6/12/agentic-coding/)
- Titus Winters et al., [Software Engineering at Google](https://abseil.io/resources/swe-book)
- John Ousterhout, [A Philosophy of Software Design](https://web.stanford.edu/~ouster/cgi-bin/book.php)
- Hyrum Wright, [Hyrum's Law](https://www.hyrumslaw.com/)
- Dan McKinley, [Choose Boring Technology](https://boringtechnology.club/)
- tef, [Write code that is easy to delete, not easy to extend](https://programmingisterrible.com/post/139222674273/write-code-that-is-easy-to-delete-not-easy-to)
- [The Grug Brained Developer](https://grugbrain.dev/)
- Rob Pike, [Notes on Programming in C (5 rules)](http://doc.cat-v.org/bell_labs/pikestyle)
