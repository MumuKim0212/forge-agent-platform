# 0047. A build may repair its own output from measured smoke failures, inside one build and one budget

- Status: accepted
- Date: 2026-11-18

## Context

An earlier build of a room-booking board wrote raw SQL with an unquoted `end` column. Postgres rejects it, `GET /bookings` answered 500, and the screen showed "HTTP 500". The build passed, was approved, and deployed. The sandbox has no database (ADR 0023), so the agent cannot catch this itself, and `CODING_HYGIENE_GUIDANCE` already tells it to compile and import the app. The platform now measures it: after the preview starts, the smoke check calls each parameterless `GET` route and stores the statuses (`smoke_checks`). Nothing acts on the measurement except a human reading it.

The platform's rule is that a failed build is not retried automatically and stays failed, and that is right for a *failed* build. This decision is about a build that **passed** but whose own preview shows a defect the platform can measure.

Constraints found in the code:

- The agent is single-shot: one prompt in, one `result.json` out. A repair is a new run, not a follow-up message.
- A sandbox is single-use and destroyed after harvest (ADR 0003 invariant 1). "The same sandbox" cannot mean the same container.
- The smoke check needs the image and the preview, so it runs after harvest and publish, inside `execute`, with the build still `RUNNING`.
- Usage is read only after a drain, and the drain closes the build's proxy token (ADR 0014 §Amendment 2026-10-24).
- One wall-clock limit covers the whole build, enforced by the sweep.
- Nothing may read the build trace to make a decision, and text authored inside the sandbox never reaches the failure path.

## Decision

**1. Trigger: a smoke-check failure, and only that.** After an attempt that was harvested, published and previewed, if the smoke check measured at least one failing route, and repair attempts remain, the build runs a repair attempt. A route fails if its status is 500 or above, or its outcome is `server_error` or `no_answer` (the same definition the approval console uses; it becomes one named predicate used by both). Not triggers in this decision:

- a build that failed (any reason code): it stays failed, and "no auto-retry" still holds for it;
- policy-gate findings: the gate's verdict is immutable per build and routes to a human by design, so treating a finding as repairable is a separate decision;
- a missing entrypoint: cheap to add later, not the defect this decision targets;
- a smoke check that could not run (`smoke_checks` NULL): "not measured" never triggers anything.

**2. One build, one token, N attempts.** The repair runs inside the same `execute` call, on the same build row, the same proxy and the same token, so the per-build budget and the per-build wall-clock cover all attempts together. Each attempt is its own single-use sandbox run (provision, execute, harvest, destroy). The drain and the usage read happen once, after the last attempt, so the cost fields remain one figure the proxy measured, and the trace's per-call sums still equal `builds.cost_usd`.

**3. The repair attempt's input.** The workspace is the previous attempt's sanitised copy (ADR 0032), with template-protected paths restored exactly as publish restores them. The user prompt is composed on the host in `agent_prompt.py` from: the same requester-marked request as the first attempt, then platform lines stating that the previous output was run and which routes failed. The failing routes are listed as a delimited block of `GET <path> -> <status>`, taken only from the stored measured fact: paths that already passed the safe-character filter, integer statuses, outcome words, at most the check's 20 routes. Nothing else is fed back: no response bodies, no app logs, no trace, no agent summary, no gate findings. The system prompt does not change, so the prompt fingerprint is unchanged by this decision.

**4. Limits.**
- `BUILD_REPAIR_MAX_ATTEMPTS`: repair attempts after the first. Default 1, 0 disables the loop, validated 0 to 3.
- A repair attempt starts only if the **largest cost and the longest duration of any earlier attempt still fit** in what is left of the build's budget and wall-clock. If not, the build keeps what it has. No fixed fractions.
- The proxy's budget check, the drain, the 4 MiB cap and every refusal apply to every attempt unchanged.

**5. Outcome rules.** The build ships **the last attempt that completed**, meaning it was harvested, passed the entrypoint check and produced an image and a smoke measurement or a clean "not measured".
- An attempt that does not complete (agent failure, no entrypoint, preview could not start, a failure to publish) is discarded, and the previous completed attempt ships. A repair can improve a build, never fail one that had already passed.
- **Fail-closed conditions still fail the build**, whichever attempt hits them: a recorded policy refusal at the proxy, an unmeterable response, a drain that failed or ran out. Those are about the guardrails, not about the output's quality.
- Exactly one of each exists for the build, made from the shipped attempt: one branch and PR (`diff_ref`), one policy-gate run and verdict, one approval request, one live preview. Resources of a discarded or superseded attempt are removed before the next starts or when the build ends.
- `builds.repair_attempts` (integer, not null, default 0) records how many repair attempts were started. It is a measured fact, set by the platform.

**6. The smoke check stays informational.** It still never fails a build and never changes what an approver may decide. The loop reads it; no approval decision does. The approver sees the shipped attempt's measurement, so a build whose repair worked reads clean, and one whose repair did not still shows the failing routes.

## Alternatives considered

- **Re-submit as a new build** (the previous rule). It produces a second PR and a second approval request, resets the budget, and the requester never sees the first failure. Rejected for this defect, where nothing failed visibly.
- **Keep the sandbox container alive and talk to the agent again.** The smoke check is a platform measurement of an image the platform built, so the container would have to outlive harvest. That breaks single-use and the destroy-after-harvest rule. Rejected.
- **Let the agent run its own app against a database in the sandbox.** The sandbox has no database by design (ADR 0023). Rejected.
- **Make a smoke failure fail the build.** It rejects builds the approver might accept (a route that needs a secret, a seeded row), and the smoke check was made informational on purpose. Rejected.
- **Also feed back policy-gate findings.** The gate re-flags already-approved conditions (the gate has no diff access), so a loop would chase findings the build did not introduce. Deferred.
- **Feed back a bounded excerpt of the app's error log.** It carries the real cause (`syntax error at or near "end"`), where a status carries only the symptom. It is also text authored inside the sandbox going back into the sandbox and being logged by the platform. Deferred, not rejected: **trigger:** a repair that failed on the same routes because the status alone was too little (the attempt count per eval case result makes this measurable).

## Consequences

- The "no auto-retry" rule is scoped to failed builds, and the build's run flow and budgets account for the repair.
- Cost rises by up to (1 + `BUILD_REPAIR_MAX_ATTEMPTS`) times for a build whose smoke check fails, and not at all for one whose check is clean. The per-build budget still bounds it.
- A route that cannot be fixed by the agent (it needs input or a seeded row) is retried once for nothing. That is a cost of the trigger being only a status, accepted with a cap of 1.
- **Evals:** a run under a different `BUILD_REPAIR_MAX_ATTEMPTS` is not comparable, as one under another prompt fingerprint is not. The eval run records the setting (nullable, NULL is "not recorded"), the case result carries the attempt count, a run whose worker's setting differs from the recorded one fails, and request set v3 includes the `room-bookings` case. **The loop's effect has not been established; it is to be measured by two runs with the setting changed and both processes restarted between them, not by a per-run override.**
- The timeline and the approval console show the attempts. The count is also on the build and in its API schema.
- The rule that nothing may read the build trace to make a decision is untouched: the loop reads the smoke fact, never the trace.
- A worker restart in the middle of an attempt orphans the build as it does today (ADR 0037); nothing resumes an attempt.

## Amendment 2026-11-19: a repair's reason is stored

Running the loop on a real build showed that nothing recorded why it repaired: the shipped attempt's smoke fact replaces the first one's, the first attempt's image and branch are removed, and only `builds.repair_attempts` survives. The measurement could say a repair happened, not what it fixed, except through an agent-written call record, which is a claim.

**Decision.** The platform stores, on the build, the failing routes each repair attempt was started for: `builds.repair_routes`, nullable JSON, a list of `{attempt, routes: [{path, status, outcome}]}`, one entry per repair attempt started (at most 3, matching `BUILD_REPAIR_MAX_ATTEMPTS`'s ceiling) and at most 20 routes each. It is written when the attempt starts, from the same `failing_routes` output that composes the repair prompt, so what is stored is what the agent was told, plus the outcome word. It is platform-measured and carries the same safe-character-filtered paths as `smoke_checks`: never a body, a log line, a trace excerpt or agent text (§3 is unchanged). A discarded repair attempt still has its entry, because the count includes it.

NULL means no repair was started, or the build ran before this column existed; it is never read as "no failing routes".

**Not decided here.** Keeping the first attempt's full smoke fact, or an app-log excerpt (§Alternatives), stays deferred. The measured route list answers "what failed"; it does not say why.
