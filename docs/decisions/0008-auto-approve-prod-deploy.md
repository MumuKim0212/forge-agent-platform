# 0008. Auto-approve prod deploy on passing policy checks

- Status: accepted
- Date: 2026-06-22

## Context

Requiring a human to approve every deploy would bottleneck the platform; approving nothing would be unsafe. We need a default that's both fast and safe.

## Decision

Prod deploys proceed **automatically when automated policy checks pass** (secrets/PII scan, dependency vuln scan, egress detection, resource limits, template compliance). A human is involved **only** when a check flags something or the action is explicitly high-risk. Risk tiers (low/med/high) route differently and are operator-configurable (e.g. high-risk = 2-person approval). Residual risk (a merely "broken/inappropriate" tool, not a security incident) is covered by project-owner accountability + audit log + one-click rollback + new-deploy notification.

## Alternatives considered

- **Human approval for every deploy:** safe but a throughput killer; defeats the non-developer self-service value.
- **Fully automatic, no human ever:** fast but no backstop for genuinely high-risk actions.

## Consequences

- Gain: fast common path; humans spend attention only where checks demand it.
- Give up: relies on the policy checks being good — they are a hard dependency.
- Watch out: the policy gate and approval routing are guardrails and per ADR 0001 must land before convenience features lean on auto-deploy.
