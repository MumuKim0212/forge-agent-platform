# 0037. A label-driven reconciler repairs Docker drift from database state

- Status: accepted
- Date: 2026-10-17

## Context

The platform's Docker side effects are made by best-effort calls inside request and worker paths: sandbox and proxy containers, build networks, previews, deployment containers, project Redis. The database is the record of what *should* exist. A code review on 2026-10-16 found that nothing ever compares the two:

- **L3 leaks are unbounded and dangerous.** A worker that dies or is restarted mid-build skips every `finally`. The sandbox keeps running, Docker does not kill an exec when its client goes away, and the proxy container keeps the real API key and a never-revoked token. The timeout sweep only flips the build row to `TIMEOUT`; the intended behaviour is that the sweep force-destroys the container, and it does not. L3 containers and networks carry no labels, so nothing could find them anyway.
- **L4 orphans can serve production traffic.** Traefik priority falls with container age, so an older surviving container of a project outranks the `ACTIVE` one. A crash between attaching a new container to Traefik and activating it, or a swallowed removal error on supersede, leaves exactly such a container. The deployment intent is that containers are labelled "so a restarted platform can rediscover and manage them"; nothing reads those labels.
- **Missing data services go unnoticed.** On 2026-10-17 a deployed service was found running without its Redis, with nothing positioned to notice.

## Decision

**One reconciler, in the worker, compares Docker objects carrying the platform's labels against database state and removes or restores the difference. It acts only on objects it can positively identify as its own.**

1. **Every platform-created container and network is labelled** with:
   - `forge.instance`, a per-installation id;
   - `forge.role`, one of a closed set: `sandbox`, `llm-proxy`, `build-network`, `preview`, `preview-redis`, `preview-network`, `deploy`, `deploy-network`, `service-redis`;
   - the ids that tie it to a row: build, deployment, project.

   Existing L4 labels stay, and the new ones are added beside them.
2. **The instance id scopes everything.** The reconciler only touches objects whose `forge.instance` is its own. The test suite and a second checkout on the same daemon use different ids, so a running dev worker can never reap a test's containers, and no test can reap a dev service.
3. **What it does, by role:**
   - `sandbox`, `llm-proxy`, `build-network`: removed when the build is not `RUNNING` (or does not exist). The proxy is revoked first where reachable.
   - `deploy`: removed when its deployment is neither `ACTIVE` nor an in-progress `DEPLOYING` claimed by this worker process. It is detached from Traefik before removal.
   - `preview` and its stores: removed when the preview is no longer live, which complements the TTL sweep.
   - `service-redis`: never removed by the reconciler. It is re-ensured (started or recreated, by the existing ensure path) for every project with an `ACTIVE` deployment.
   - **Never touched:** objects without the platform's labels, objects with an unknown role, other instances' objects, the shared Postgres, any volume.
4. **When it runs:** once at worker start, before claiming work, and then periodically (an interval setting). The build timeout sweep also destroys the timed-out build's L3 objects directly, as originally intended.
5. **Worker start treats in-flight rows as orphaned.** There is one worker process (a single worker is load-bearing on SQLite), so at start any `RUNNING` build, or any `DEPLOYING` deployment that a worker had already **claimed** (`started_at` set), belongs to a dead process. An unclaimed `DEPLOYING` row is a queued confirm with no objects yet, and it is left for the next claim. It is failed with a platform-authored reason ("worker restarted"), and its objects are reaped by rule 3. If the platform ever runs more than one worker, this rule must become a lease first; until then it is the simplest correct rule. **Amendment 2026-10-28:** the single worker is now enforced by a database lease taken before this rule runs (ADR 0038).
6. **Graceful stop.** On SIGTERM the worker stops claiming, tears down the build or deployment in flight through its normal failure path, and exits. Rule 5 covers the cases where it could not.
7. **Every removal and restore is logged, and audit-logged where a project is involved.** The reconciler never fails a request; it is background repair.

## Alternatives considered

- **Fix each `finally` instead.** Necessary but not sufficient. No `finally` runs when the process is killed, and that is the case that leaks a key-holding proxy.
- **Docker `--rm` / auto-remove on L3 containers.** It covers containers that exit, but the sandbox runs `sleep infinity` and the agent is an exec, so it covers nothing that matters.
- **Name prefixes instead of labels.** Names cannot carry the build/deployment ids cleanly, and "matches a prefix" is a description, not an identification. A prefix match has already removed a production Redis in practice.

## Consequences

- Gain: crash recovery becomes a property of the platform instead of of every code path. The L4 "older container outranks ACTIVE" failure becomes self-healing, and a missing Redis under a live service is restored.
- Give up: a deliberate manual container under the platform's labels and instance id will be removed. That is the point of the labels.
- Watch out: the instance id must be stable across restarts of one installation, and distinct per test run. It is stored, not derived from the hostname.
