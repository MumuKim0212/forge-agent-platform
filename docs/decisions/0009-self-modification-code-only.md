# 0009. Self-modification is code-level only

- Status: accepted
- Date: 2026-06-22

## Context

Natural-language self-modification of a running service is powerful but dangerous if it can alter persistent data structures.

## Decision

Natural-language changes are allowed at the **code level only**. **DB schema changes and migrations are forbidden** via the self-modify path (data corruption / irreversibility risk). Requests that require a schema change are routed to the developer path.

## Alternatives considered

- **Allow agent-driven migrations:** maximal autonomy, but an agent-generated bad migration can irreversibly damage real data; unacceptable risk for non-developer-initiated changes.
- **Forbid self-modification entirely:** throws away a core value prop; the code-only boundary keeps the benefit while removing the worst hazard.

## Consequences

- Gain: the irreversible-data-damage class of failure is structurally excluded from the self-modify loop.
- Give up: schema-affecting changes can't be done in natural language — they need a developer.
- Watch out: every self-modify build must auto-create a rollback point and re-run existing tests; enforce the schema-change block where the change is applied.

## Amendment 2026-10-05 — where the block is enforced, and why it refuses rather than drops

§Consequences says to "enforce the schema-change block where the change is applied". Until this amendment nothing enforced it. The prompt asked the agent not to touch schema on a modify build, and the build-sandbox design promised the change would be "dropped at harvest and reported as drift". Until publishing was wired up, nothing the agent wrote was ever published at all.

**Decision: on a modify-type build, any change to a schema path refuses the build.** A change means a file added, edited or deleted, compared with the build's own input tree (the confirmed ref it was staged from). The build fails with `AGENT_ERROR` and a platform-template detail saying a schema change was attempted on a self-modify build and must take the developer path. Nothing is published.

- **Why refuse, not drop and continue.** A schema change is rarely alone. An agent that adds a column also writes the code that reads it. Dropping the migration while publishing that code deploys a service that fails at runtime against a schema it assumed. That is the "irreversible damage" class this ADR exists to exclude, reached by the back door, and worse than the change it removed. Refusing is the only outcome that never publishes an inconsistent tree.
- **Template-owned manifests are different.** These are the Dockerfile, compose and instrumentation bootstrap. They keep the restore-and-report-drift behaviour, because image assembly always uses the pinned copies and no generated code depends on the agent's edits to them.
- **Schema paths are declared by the platform, as prefixes,** for the one template shape: its Alembic directory and config. Create-type builds are unaffected and may add forward-only migrations (ADR 0023, policy check #6).
