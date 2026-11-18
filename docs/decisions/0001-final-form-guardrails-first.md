# 0001. Target the final form, but build guardrails first

- Status: accepted
- Date: 2026-06-22

## Context

Forge could be released in phases (prototype → preview → deploy → self-modify) or built toward the full target spec from the start. The riskiest path in the whole system is a non-developer's natural-language request reaching production.

## Decision

Target the **full final-form spec** from the outset (no staged public releases), **but** order implementation so that guardrails — L3 isolation, policy gate, ownership enforcement — are completed **before** convenience features (self-modification, multi-template).

## Alternatives considered

- **Classic phased MVP rollout:** simpler to ship early, but invites building convenience features before the safety envelope exists, exactly where incidents happen.
- **Everything in parallel:** maximizes apparent progress but leaves the non-dev→prod path unguarded the longest.

## Consequences

- Gain: the dangerous path is never live without its safety net; clear build ordering for the roadmap (isolation, then policy gate and ownership, then self-modify).
- Give up: less impressive early demos (the flashy self-modify loop comes later).
- Watch out: resist "just ship the agent build first" pressure — it inverts this ADR.
