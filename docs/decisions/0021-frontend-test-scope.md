# 0021. Frontend testing: Vitest unit tests, no E2E layer

- Status: accepted
- Date: 2026-07-29

## Context

The frontend has had no test infrastructure so far — no runner, no framework, nothing. The frontend SSO work was verified by `tsc -b`, `vite build`, and a manual pass, which is why its review had to state verification limits explicitly instead of asserting coverage.

That was tolerable while the UI was mostly display. It stops being tolerable once the admin console lands: it is the largest role-conditional surface yet, and role/ownership branching is exactly the logic where a silent regression is invisible to `tsc` and to a casual manual pass.

Backend testing is already established (347 tests, `pytest`) and is not in question here.

## Decision

**Vitest for unit tests. No E2E layer.**

- Vitest because the frontend is already Vite — it reuses the existing build config and resolution, so setup is near-zero and there is no second toolchain to keep aligned.
- **What gets tested:** pure logic and the decision functions that gate UI. Concretely, the role ladder and `holdsAtLeast` fail-closed behaviour, `canDecideApprovals`, `canOperateDeployment`'s owner-**or**-admin composition, mode detection's resolve-to-`oidc` behaviour on every failure shape, and the 401 hook's destination preservation.
- **What does not get tested:** full browser flows. The IdP round-trip, the switcher bootstrap, and cross-page navigation stay manual.

Rationale for the split: the defects that have actually shown up in this project's UI work were logic defects with a truth table — a role comparison, a fail-open default, an owner check collapsed into a role check. Those are cheap to pin as units and were the real risk in the SSO work. Browser-flow bugs have not been the failure mode.

## Alternatives considered

- **Vitest + Playwright E2E.** Genuinely catches things units cannot — the login round-trip, the dev switcher bootstrap, cross-page 401 recovery. Rejected for now on cost, not value: there is **no CI in this repo yet**, so an E2E suite would depend on someone remembering to run it locally, and an E2E suite that is not reliably run is worse than none — it produces a false sense of coverage while rotting. Revisit when CI lands; that is the precondition, not team size.
- **Jest + React Testing Library.** The conventional pairing, but it means a second toolchain configured to agree with Vite's resolution and transforms. No benefit here.
- **Continue with nothing.** Rejected: the console's role-conditional branching is precisely where an untested regression silently grants or hides an action.
- **Unit tests plus component-render tests for every page.** Rejected as low-yield — asserting that a page renders text mostly re-tests React, and those tests churn on every markup change.

## Consequences

- Gain: the logic that decides what a user may see is pinned, cheaply, in the layer where this project's real UI defects have lived.
- Gain: reviewers of frontend changes get an automated signal, so a review no longer has to rest entirely on reading code paths.
- Give up: no automated coverage of the login round-trip or any multi-page flow. **Frontend reviews must keep stating verification limits** rather than implying browser flows are covered — that discipline does not go away, it just narrows.
- Watch out: the temptation will be to test rendering because it is easy to write. Keep the suite pointed at decision functions; a render test that asserts a heading exists earns nothing.
- Watch out: **do not let a UI test become the reason a backend check looks redundant.** The backend is the enforcement point; these tests cover presentation only.
- Precondition recorded for the rejected option: **when CI exists, revisit Playwright.**

## Amendment 2026-08-30 — one narrow jsdom exception; still no E2E

Two things happened after this ADR was written, and they point in opposite directions.

**Against the ADR:** its central empirical claim — "browser-flow bugs have not been the failure mode" — was falsified on 2026-08-27. The first exploratory pass that actually drove these screens in a browser found **four defects out of four**: English copy in a Korean UI, mouse-only rows, and two CSS faults. Nothing in the suite could have caught any of them.

**For the ADR:** what found them was an *exploratory* pass, not a regression suite. The two defects worth committing tests for were closed by unit-level pins. An E2E layer would not have found them first and would not keep them fixed more cheaply than the pins do.

**The precondition is now literally true** — `.github/workflows/ci.yml` has landed — so the Playwright option is re-evaluated on merit rather than on cost, and the answer is still no. What is missing is not browser flows; it is that **`node` makes the keyboard and ARIA behaviour added after that pass unverifiable by construction**, and that code is now the pattern every future table copies.

### Decision

1. **No E2E layer.** Unchanged. Revisit only if a defect appears that a mounted component genuinely cannot reach.
2. **An exploratory browser review becomes a per-slice ritual, not a one-off.** It is 4-for-4, it cannot rot because nothing is committed, and it is the only thing in this project that sees a screen. Any slice that ships UI gets a pass before it closes.
3. **jsdom is permitted in one narrow place**: files named `*.a11y.test.tsx`, opting in per file with a `// @vitest-environment jsdom` docblock. Scope is keyboard operability, focus, and ARIA — the behaviour `shouldActivateRow`, `tabIndex`, `aria-current`, and `aria-label` exist to produce. Not rendering, not copy, not layout.

### Why per-file opt-in rather than switching the runner

The original reasoning was that `environment: "node"` makes "do not write render tests" **structural instead of remembered**. Flipping the global environment would dissolve exactly that, and the temptation this ADR warned about ("the temptation will be to test rendering because it is easy to write") is unchanged.

So the guarantee moves rather than disappearing: the default environment stays `node`, and **a test asserts that no file outside the `*.a11y.test.tsx` pattern carries the jsdom docblock.** That is the same enforcement shape this project already uses twice — the `ast` scan banning role literals outside `services/roles.py`, and the `ast` scan banning settings serialization in `console.py`. A rule a machine checks, not a rule a reviewer remembers.

### Consequences

- Gain: the project's first keyboard/ARIA code becomes testable, in the layer where it actually runs.
- Give up: the "impossible by construction" property becomes "caught by a test". Weaker in kind, and the enforcing test must itself be anchored against vacuity — adding the docblock to an ordinary test file has to turn it red.
- Unchanged: the backend is the enforcement point. A UI test must never become the reason a backend check looks redundant.
- Unchanged: frontend reviews still state what they could not verify. Multi-page flows and the IdP round-trip stay manual.
