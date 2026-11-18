# 0038. One worker per installation, held by a database lease

- Status: accepted
- Date: 2026-10-28

## Context

The single worker process is load-bearing on SQLite. ADR 0037 rule 5 builds on it: at start, the worker treats every `RUNNING` build and every claimed `DEPLOYING` deployment as belonging to a dead process, fails them, and the reconciler then removes their sandbox, proxy and deploy containers. ADR 0037 says "if the platform ever runs more than one worker, this rule must become a lease first".

Nothing enforces the single worker. An audit on 2026-10-26 found:

- The worker is started by hand (`dev.ps1` does not start it). A second `python -m app.worker` is one mistyped command away.
- Since ADR 0037's recovery, a second worker's **start** is destructive. It fails the first worker's in-flight build and deployment, and its reconciler removes their objects while the first worker is still using them.
- `deploy._activate` writes `ACTIVE` without re-reading the row. If the first worker had already passed the health gate, it can activate a row the second worker failed, then supersede and remove the live container.

## Decision

**A worker must hold the installation's worker lease before it does anything that assumes it is the only worker. A second worker refuses to start.**

1. **The lease is one database row** (in the platform DB, beside `platform_instance`): a holder id (random per process, plus host and pid for the operator's message), `acquired_at` and `heartbeat_at`. It is taken and renewed by a conditional `UPDATE` whose row count says who won, so it works the same on SQLite and Postgres.
2. **Acquire before anything destructive.** At start, before ADR 0037 rule 5 recovery, the first reconcile and the first claim:
   - free or stale (no heartbeat for the stale window) → take it;
   - held, and its heartbeat **advances** while we wait → another live worker: refuse to start with exit code 1 and a message naming the holder's host, pid and heartbeat age;
   - held but not advancing (a previous worker that was killed) → wait until it is stale, then take it.
3. **Renewal runs on its own thread**, because a build blocks the worker loop for minutes. Heartbeat every 10 s; stale after 60 s. These are constants, not settings.
4. **The holder re-checks before each destructive step.** Each claim, each reconcile pass and each timeout sweep first confirms the lease is still its own. A worker that finds it has lost the lease stops claiming and exits. It never runs recovery or reaping without the lease.
5. **Graceful stop releases the lease** (the SIGTERM path of ADR 0037 rule 6), so a normal restart does not wait out the stale window.
6. **Activation is a compare-and-set.** `_activate` moves a deployment to `ACTIVE` only if it is still `DEPLOYING`. Otherwise it tears down the container it started and leaves the row alone. This is defence in depth for the window a lost lease cannot close.

## Alternatives considered

- **An OS file lock or pidfile.** It is local to one host and one checkout, and a stale pidfile after a crash needs a human. The DB is already the one thing every worker of an installation shares.
- **Postgres advisory locks.** They do not exist on SQLite, which is the dev store.
- **Allow N workers now** (lease per build, `SKIP LOCKED`). That is the scale path for the worker, and it needs per-row ownership throughout recovery and reconcile. Nothing needs it yet.
- **Document the rule only.** Rejected, because with ADR 0037's recovery the cost of one mistake is the live service.

## Consequences

- Gain: an accidental second worker is a refused start with a clear message, not lost work and a removed production container.
- Give up: after a hard kill, the next worker waits up to the 60 s stale window before it starts.
- Watch out:
  - The lease is per installation (the platform DB), not per host. Two checkouts on one daemon have two DBs and two instance ids (ADR 0037 rule 2), so each has its own lease.
  - A restored DB backup carries an old lease row. It is stale by construction, so it is taken over.
  - Moving to N workers supersedes rules 2 and 4 with per-row leases.
