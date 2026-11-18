# 0023. Per-service data services: shared Postgres (database + role per service), Redis per service

- Status: accepted
- Date: 2026-08-29

## Context

An earlier decision fixes the generated-service stack at FastAPI + PostgreSQL + Redis. Another requires each service to own only its own DB (arbitrary cross-service data access structurally blocked, with an operator escape hatch for explicitly connecting specific tables). Call these the stack decision and the data-isolation decision. **Neither exists in code.** The template reads no environment variable at all, its Dockerfile installs only `fastapi` + `uvicorn`, its `compose.yaml` is a labelled placeholder, and the four keys the platform issues at first deploy — `FORGE_SERVICE_ID`, `FORGE_SERVICE_NAME`, `FORGE_SERVICE_BASE_URL`, `FORGE_APP_SECRET_KEY` — carry no `DATABASE_URL` or `REDIS_URL`. Provisioning was explicitly deferred until "the template gains real data services".

This is the largest remaining product gap: a generated service cannot hold state, which is most of what an internal tool is for.

Three constraints narrow the design.

- **Single host** (ADR 0002). Everything runs on one Linux machine beside the platform, the L3 sandboxes, and every deployed service. Per-service Postgres containers are the dominant memory cost at a few dozen services; Redis is not (an idle Redis is single-digit MB, an idle Postgres is an order of magnitude more and grows with its connection pool).
- **Per-deployment internal networks.** `DeployManager.start_service` creates each deployment's network with `internal=True`. A data service must sit on that network to be reachable, so the choice of shape is also a choice about network topology.
- **The data-isolation decision's escape hatch is cross-database by nature.** "An operator explicitly connects specific tables … by issuing a scoped DB role/view that can only SELECT the named tables" is a `GRANT` and a `CREATE VIEW` inside one Postgres instance. Across separate instances the same feature needs `postgres_fdw` or a replication path — substantially more machinery for the same guarantee.

## Decision

**Postgres: one shared instance, one database and one login role per service. Redis: one container per service.**

The asymmetry is deliberate — the two stores have opposite cost curves on a single host, and only Postgres has a cross-service feature (the escape hatch) that wants co-location.

**"Shared" here means one server process, not one database.** A Postgres connection is bound to a single database, and there is no cross-database query without `postgres_fdw` or `dblink`. Service A's connection cannot name a table in service B's database — not by convention or careful naming, but because the protocol gives it no way to. Two services' rows never land in the same namespace, and a table-name collision between services is not a thing that can happen. What is genuinely shared is the server process; that, and nothing else, is what the Consequences section below is about.

### Postgres

- A single operator-provisioned Postgres instance holds one database per project, owned by one login role per project. The platform holds an admin DSN and issues `CREATE DATABASE` / `CREATE ROLE` through it.
- The service role has privileges on **its own database only**: no `CREATEDB`, no `CREATEROLE`, no superuser, and `REVOKE CONNECT ON DATABASE … FROM PUBLIC` on every service database so a role cannot connect sideways.
- The shared instance attaches to each deployment's internal network. Services still cannot reach each other — Docker networks are not transitive and Postgres does not forward — but the instance is multi-homed, which is the cost recorded below.

### Redis

- One Redis container per project, on that project's deployment network only, no published port, `--save ""` unless the service opts into persistence.
- Rejected the shared alternative: separating services by numeric DB index gives no authentication boundary at all, and Redis ACL users are a per-service credential to manage anyway — at which point a container is simpler and strictly more isolated.

### Provisioning lifecycle

- **Created lazily at first deploy**, in the same place and the same shape as the existing service env: `ensure_service_env()` already issues once and returns the stored value on every later call. Provisioning extends that function rather than adding a second lifecycle.
- **`DATABASE_URL` and `REDIS_URL` join the service env**, so they inherit its existing guarantees unchanged: stable across redeploys and rollbacks, never returned by any API, never logged, never baked into an image, audit-logged by key name only.
- **Never deleted implicitly.** Stopping a service, superseding a deployment, a failed deploy, and a rollback all leave the data intact. Deprovisioning happens only through the staged zombie path (stop → archive → delete) or an explicit operator action, both audit-logged.
  **Corrected 2026-09-14:** the staged path's "→ delete" half no longer exists. A later amendment to the zombie-service decision (2026-09-11) ends the automatic path at archive — with no channel on which a confirmation can be requested, silence from an absent project owner cannot authorize destruction — so the *only* route to deprovisioning is the explicit operator action, whose shape is defined by a separate decision. This strengthens the promise above rather than weakening it: "never deleted implicitly" now has no implicit path left to exclude.

### Schema migrations

- The template runs `alembic upgrade head` at container start, before the app serves. Migrations are **forward-only**; nothing downgrades.
- **Rollback does not touch the schema.** Rollback is already runtime-only, and reintroducing a downgrade path would make it data-destroying. The consequence is a rule on generated migrations: **a migration must leave the previous image able to run** — additive columns/tables, no drop or narrowing type change in the same deploy that starts using the new shape.
- That rule is enforceable where the other five are: a sixth policy-gate check (added to the gate's check catalog) flags destructive DDL in the build's diff. Like every check, it fails closed and flags rather than blocks, routing to a human at `medium`.

### Backup

Operator-configurable with a working default (decided 2026-08-29):

| Setting | Default | Meaning |
|---|---|---|
| `SERVICE_DATA_BACKUP_ENABLED` | `true` | Master switch; `false` disables all scheduled dumps |
| `SERVICE_DATA_BACKUP_INTERVAL_HOURS` | `24` | Dump cadence |
| `SERVICE_DATA_BACKUP_RETENTION_DAYS` | `7` | Dumps older than this are deleted |

Scope: Postgres only. Redis is a cache by default (`--save ""`) and backing it up would imply a durability guarantee the platform does not make.

## Alternatives considered

- **A Postgres container per service.** The strongest isolation and the honest reading of the data-isolation decision's "structurally blocked". Rejected on the single-host constraint: the per-instance overhead is the one that scales with service count, and the cross-service escape hatch becomes `postgres_fdw` instead of a `GRANT`. **This is the alternative to revisit if the platform ever moves off one host** (ADR 0002's scale path is "add hosts", not autoscaling).
- **Shared Postgres, per-service schema in one database.** Cheaper still, but `search_path` is a runtime setting rather than a boundary — one mistaken `SET search_path` or a fully-qualified name reaches another service's tables. A database boundary is enforced by the connection.
- **Shared Redis with per-service ACL users.** Comparable isolation to a container, but adds ACL lifecycle management for a component whose per-instance cost is the thing that made sharing attractive.
- **No provisioning; let each service bring its own store.** Destroys the operational uniformity the stack decision exists to create.

- **SQLite per service, no database server at all.** Attractive because it looks like it deletes the provisioning work. It does not — the work moves from `CREATE DATABASE` + `CREATE ROLE` to a per-service Docker volume that must survive redeploy and rollback — and it collides with three things this repo has already settled. **(1)** `DeployManager.start_service` creates L4 containers with `volumes={}`, and "no volumes at all" is written down as a hardening invariant beside `no Docker socket` / `cap_drop=ALL`; SQLite requires punching a writable mount through it. **(2)** The health-gated switchover runs **both containers concurrently** — start new → `wait_healthy` → route flip → remove old — so for the whole health window (30s default) two processes hold the same database file, and the new one runs `alembic upgrade head` against a file the old one is still serving from. Avoiding that means stop-then-start, which surrenders ADR 0008's safety backstop: a new container that fails health would take the service down instead of leaving the old one routed. **(3)** SQLite has no roles, no `GRANT`, and no users, so the data-isolation decision's scoped-table escape hatch becomes *unimplementable* rather than merely harder. It would also require amending the stack decision. None of this is a judgement on SQLite — it is fine for one small tool — the objection is that this platform's deploy model and its data-governance decisions both assume a server.
- **Hybrid: Postgres for externally-facing services, SQLite for internal ones.** Rejected on its premise. The project's non-goals state "hosting externally-public services — internal only", and an earlier routing decision restates that deployed services are **never publicly routable**. There is no externally-facing class to split on, and no field on any model that could carry the classification. Even given one, the call would be made by the requester at intake — the moment of least information — and a later reclassification would need a SQLite-to-Postgres migration feature nobody has scoped. It also pays *both* option's costs and forks the golden path into two Dockerfiles, two backup paths, two migration paths, and two policy profiles, which is exactly the operational uniformity the stack decision exists to protect. **If exposure-based tiering is wanted later, the lever is a second Postgres instance for a high-sensitivity class — not a second engine.**

## Consequences

- Gain: generated services can hold real data — the precondition for the platform's actual purpose.
- Gain: the data-isolation decision's scoped-table escape hatch stays a `GRANT` plus a view inside one instance.
- Gain: one instance to back up, tune, and upgrade.
- **Give up — and this needs stating plainly: the data-isolation decision's isolation weakens from structural to role-based.** Its wording ("arbitrary access … is structurally blocked") describes a per-instance world. Under this decision the boundary is a Postgres role and a database grant: strong, standard, and audited, but enforced by the database rather than by the absence of a route. **That earlier decision must be amended to say so** rather than left to read as a stronger promise than the system makes.
- Give up: the shared instance sits on every deployment's internal network, so compromising Postgres itself reaches every service network. It does not create service-to-service reachability (networks are not transitive), but it is the one component whose blast radius is platform-wide. It gets the same hardening as an L4 service: no published port, resource caps, no Docker socket.
- Watch out: a per-service connection pool times the number of services against `max_connections`. Size it against the service count, not the default 100, before this is load-bearing.
- Watch out: the forward-only migration rule is a **constraint on what the agent may generate**, so it belongs in the agent's prompt and the policy gate, not only in this ADR.

## Amendment 2026-09-01 — nine things the decision left unstated

Written before implementation began. None of these reverses the decision above; each closes a gap that would otherwise be discovered mid-implementation, where the cheapest available answer is rarely the right one.

### 1. Data services attach and **detach** per deployment, mirroring Traefik

`DeployManager.start_service` creates a **fresh** `internal=True` network per deployment, named with a random suffix (`deploy_manager.py`), and `remove_service` destroys it — after explicitly disconnecting Traefik first (`_empty_network` → `_destroy_network`). Docker will not remove a network that still has a container attached, so the original wording ("the shared instance attaches to each deployment's internal network") describes half a lifecycle and the missing half breaks teardown.

The rule, and it is the pattern already in the file rather than a new one:

- The shared Postgres and the project's Redis are **connected** to a deployment's network at start and **disconnected** before that network is destroyed, exactly as `connect_to_traefik` / `_disconnect_traefik` bracket Traefik.
- **Two deployments are live at once during the health-gated switchover**, so both are attached simultaneously for the health window. That is correct and must not be "fixed" by moving the attachment.
- Attachments are therefore transient and bounded by live deployments, not accumulated. A leaked attachment is an operational bug with the same shape as a leaked network: the teardown path owns it.

### 2. A preview needs its own data services, and must never receive the service's

`PreviewManager.start_preview` runs `build.image_ref` — **the same image L4 runs** — on its own fresh `internal=True` network (`preview_manager.py`). Once the template requires `DATABASE_URL` and runs `alembic upgrade head` at start, a preview that gets no database does not start at all, and a preview that gets the *service's* database puts unvetted, L3-produced code on production service data. The second outcome contradicts the L3/L4 boundary that the project treats as the hard one.

So: **a preview gets an ephemeral database and an ephemeral Redis, created with the preview and dropped with it when the TTL expires.** They are the one exception to "never deleted implicitly" — that rule protects *service* data, and preview data is by construction throwaway. A preview is never handed the service's `DATABASE_URL`.

**This is not a new decision, which is the strongest thing that can be said for it.** The preview container's design already reads "**Disposable, empty data services** — never prod data", and has since previews were built. The rule was written before there were any data services to dispose of; this ADR then specified provisioning without noticing that the preview design had already answered this half. Nothing here overrides anything — it gives an existing rule its mechanism. Teardown is not a new trigger either: the preview's four existing teardown paths (TTL expiry, a newer successful build, promotion, manual stop) are where the drop belongs, so a leaked preview database has the same shape as a leaked preview container and the same owner.

### 3. With no admin DSN, deployment is refused — it does not degrade

The original plan recorded "DSN unset ⇒ provisioning off ⇒ services get no `DATABASE_URL`, i.e. today's behaviour". That stops being true the moment the template consumes those variables: the unset path becomes a new failure mode in every generated service rather than the status quo.

Making the template tolerate a missing database would fork the golden path into a with-data and a without-data shape, which is exactly the operational uniformity the stack decision exists to protect. So the platform refuses instead: **with no admin DSN configured, promotion to L4 fails at the confirm step with an operator-actionable reason**, before any container starts. The failure belongs at promotion, not at container start, so it surfaces as a named deploy failure rather than as a health-check timeout that looks like a bad build.

### 4. Backups are written to a local directory; the platform does not ship them off-host

The settings table above specifies cadence and retention and stops there — no destination, no file mode, no statement about whether dumps leave the host. Filling that in looked like it needed an answer only the installing organization has (what storage do they already run?), which is the shape of question this repo has answered twice: ADR 0018 §Amendment refuses to name an IdP, and an earlier decision refuses to choose a TLS posture. Same answer here.

| Setting | Default | Meaning |
|---|---|---|
| `SERVICE_DATA_BACKUP_DIR` | `backups/service-data` | Where dumps are written. One subdirectory per project. |

- Dumps are written mode `0600`, owned by the platform's own user. A dump is a complete copy of a service's data; it gets the file permissions of a credential.
- **The platform does not copy dumps anywhere.** No scp, no object storage, no remote target. An operator who wants off-host durability points `SERVICE_DATA_BACKUP_DIR` at a mount that their existing backup infrastructure already covers — which is the one thing they certainly have and the platform certainly does not. The alternative, a platform-owned copier, needs credentials for that remote store: a new secret of high privilege, held by the component whose compromise ADR 0002 already calls host takeover, bought for no capability the mount does not already provide.
- **Disclose the consequence rather than let it be discovered.** On a default install, service-data dumps sit unencrypted on the same host as the database they came from. That makes them useful against corruption, a bad migration, and a mistaken `DROP` — and useless against losing the host. Do not describe the default as disaster recovery. This is the same disclosure the TLS decision makes about plaintext being the default: the limitation is real, and it stops being dangerous once it is written down.
- Interaction with the decision to encrypt the platform's `service_env_json`, stated because it reads like a contradiction otherwise: that decision encrypts the column because database copies leave the host, and this one writes service-data copies in the clear. Both are consistent — the encryption protects *credentials* in a column the platform owns, while a service's own data is the thing the service exists to hold and the platform has no key to give it. What follows is a rule for whoever points this directory at a network mount: that mount is now as sensitive as the Postgres volume.

### 5. The platform's admin role is `CREATEDB CREATEROLE`, not `SUPERUSER`

The decision says "the platform holds an admin DSN and issues `CREATE DATABASE` / `CREATE ROLE` through it" without saying what that role is. Left unsaid, both the operator standing up the instance and whoever implements provisioning will reach for `postgres`, because it is the one that certainly works.

It should not be a superuser. `CREATEDB CREATEROLE` is sufficient for everything the platform does, and the gap between the two is large: a superuser bypasses every `REVOKE` this ADR relies on, can read every service's data regardless of grants, and on most builds can reach the filesystem through `COPY … PROGRAM`. The role-based isolation this decision accepts in place of per-instance isolation is only worth what the admin role cannot casually undo.

The decision to encrypt stored service env already flags this DSN as "a new credential of higher privilege than anything the platform has held" and asks for `GITHUB_TOKEN`'s handling discipline. This is the other half of that: hold it carefully **and** make it smaller.

### 6. The shared instance must be a Docker container on the same daemon — not a managed service, not a host process

This follows from §1 and is worth stating before an operator stands anything up, because getting it wrong is not a configuration mistake but a topology mistake.

The platform reaches a data service by **connecting it to each deployment's `internal=True` network** — `network.connect(container)`, the same call `connect_to_traefik` makes. That call takes a container on the same Docker daemon. So:

- **A managed/cloud Postgres cannot work here.** There is no container to attach, and an `internal=True` network has no route out by definition. Supporting one would mean giving up per-deployment network isolation for the database — a different decision, not a configuration value.
- **A Postgres running on the host outside Docker cannot work either**, for the same reason.
- The platform therefore needs the instance's **container name** as configuration, exactly as it already holds `TRAEFIK_CONTAINER_NAME` — same posture (the operator runs it, the platform addresses it), same shape of setting.

The operator still owns the instance's lifecycle, volume, version, and tuning. What is constrained is only *where* it runs. If an installing organization requires a managed database, that is a revisit of this ADR's topology, not a value someone can set — and the honest form of that revisit is the per-service-Postgres alternative in §Alternatives, which has the same problem, or dropping network isolation for the data path.

### 7. This is not the platform's own database, and the two must not be merged

Worth stating because "shared Postgres" invites the question. There are now three distinct stores and only the middle one is new:

| Store | Setting | Holds | Who runs it |
|---|---|---|---|
| Platform DB | `DATABASE_URL` | projects, users, builds, deployments, `service_env_json` | operator (SQLite in dev) |
| Shared service Postgres | admin DSN (this ADR) | one database per generated service | operator |
| Redis | — | one container per project | the platform |

Merging the first two would look like a saving — one instance to run — and it is the wrong trade. §1 attaches the shared instance to **every deployment's network**. Do that with the instance holding the platform database and the platform's own store, which contains every service's credentials, is multi-homed onto every service's network. A service's login role still could not connect to it (`REVOKE CONNECT`), so this is not a hole; it is a stacking of two boundaries into one. This ADR already gives up the data-isolation decision's *structural* isolation for role-based isolation and says so plainly — spending the platform DB's structural separation on the same bet is how a documented, bounded trade turns into an undocumented one. The L4 runtime rule ("a service reaches only its own data services — never the platform DB") is what stays true because these are separate processes.

### 8. Why it is operator-provisioned — and why that reasoning does not apply in dev

The original framing was "same posture as Traefik: the platform writes labels, it does not run Traefik". That is a pattern-match, not a reason, and it does not survive contact with the code: the platform runs containers in `deploy_manager`, `preview_manager`, and `proxy_container`, and this very ADR has it run a **Redis per project**. "The platform does not run infrastructure" is simply not true of this platform.

The actual reasons are about **durability**, and they are why Redis and Postgres get opposite answers:

- **A per-project Redis is a cache** (`--save ""`). The platform may create, destroy, and recreate it freely because losing it costs nothing. The shared Postgres holds every service's data permanently. If the platform owned its lifecycle, then the platform's own recreate/upgrade/repair logic becomes an automatic path to destroying all service data — precisely the thing §Provisioning lifecycle spends a paragraph forbidding ("never deleted implicitly"; the staged zombie path is the *only* automatic route to destruction). Keeping the instance outside the platform's automation is what makes that promise cheap to keep instead of dependent on the platform never having a bug in the one place a bug is unrecoverable.
- **Its tuning and versioning are ops decisions with no platform-side answer**: `max_connections` against expected service count, `shared_buffers`, and above all a major-version upgrade, which is a `pg_upgrade` with a maintenance window. A platform that auto-creates the instance either hardcodes those or grows a configuration surface for all of them, and the second one is just the operator's job with extra steps.

**Neither reason applies to the dev box, so dev must not inherit the manual step.** In dev there is no service data worth protecting, no tuning to decide, and no upgrade window — the instance is as throwaway as `forge_dev.db`. Standing it up by hand there is friction `dev.ps1` exists to remove. **The dev tooling therefore brings up a dev service-Postgres itself** (a named container on the same daemon, Docker Desktop already being a documented dev prerequisite), and the operator action is a **prod-install** step only. Note what this does *not* change: even in dev the container is addressed by name and attached per deployment (§6), so dev and prod differ in who starts it, not in how it is reached.

### 9. Three watch-outs get a home

- `max_connections` sizing against service count (Consequences, above) belongs with the provisioning implementation — it is the code that creates the connections, and a default of 100 is a ceiling that arrives without warning.
- Backup destination, mode, and retention sweep belong with the backup implementation, per §4.
- `SERVICE_ENV_ENCRYPTION_KEY`'s own backup ("back it up like the database itself", from the decision that introduced it) is an operator obligation the platform cannot verify. It belongs in the same operator documentation as the backup work, next to §4's disclosure — one page about what survives what.

## Amendment 2026-09-04 — 10. How the platform reaches the admin DSN, and what "no published port" means

§Amendment 6 settled *where* the instance runs (a container on the same daemon) and §Consequences said it gets "no published port". Together those leave one thing unstated that blocks the start of implementation: **the platform itself is not a container.** There is no Dockerfile and no compose file for it anywhere in the repo — it runs as a host process (`uvicorn`), talking to Docker through the SDK. So "attach it to each deployment's network" answers how *services* reach Postgres and answers nothing about how the *platform* reaches it to issue `CREATE DATABASE`.

The two obvious readings both fail. Resolving the container's IP through the Docker API and connecting to it works on a Linux host, where the bridge is routable, and **not on a Docker Desktop dev setup**, where the container network lives inside a VM and is unreachable from the host — so it would make dev and prod structurally different at the one seam that cannot be tested locally. Putting the platform on a deployment network is worse: those are `internal=True` and per-deployment.

**Decision: the admin endpoint is published on loopback, and the DSN carries it.**

- "No published port" means **no port on a routable interface**. A `127.0.0.1`-bound publish is not network exposure — nothing off-host can reach it — and it is what makes a host process able to connect at all. Publishing on `0.0.0.0` stays forbidden.
- The endpoint therefore lives inside `SERVICE_POSTGRES_ADMIN_DSN`, which the operator sets; the platform does not compose a host or port of its own. The **container name** stays a separate setting because attaching to networks needs the name, not the endpoint — the same split Traefik already has.
- **Dev and prod differ only in who starts the container**, exactly as §Amendment 8 says: `dev.ps1` starts it and writes a working DSN; prod's operator starts it and supplies one. The reachability mechanism is identical, which is the property this amendment exists to buy.
- Dev publishes on **55432**, not 5432, so a Postgres already installed on a developer's machine does not silently answer instead.

**The exposure this accepts, stated rather than left implicit:** anything running on the host as any user that can reach loopback can attempt to connect to the shared instance, so its isolation from the host rests on the admin password rather than on unreachability. That is inside the boundary ADR 0002 already draws — the platform holds the Docker socket, and socket access is host takeover — so this adds no new class of attacker. It does mean the admin password is a real credential and not a formality, which §Amendment 5 already required for a different reason.

### Sizing, and where the number lives

§Consequences says to size `max_connections` against service count rather than the default 100. That is only actionable if the service count is written down somewhere, so it becomes configuration rather than a comment:

| Setting | Default | Meaning |
|---|---|---|
| `SERVICE_DATA_EXPECTED_SERVICES` | `100` | Planned number of deployed services; the sizing input, set by the operator |
| `SERVICE_DATA_CONNECTIONS_PER_SERVICE` | `5` | Connections one service is assumed to hold (SQLAlchemy's default pool size) |

`max_connections` for the **dev** instance is derived from these by one named function plus a fixed headroom for the platform's own admin connections and live previews — never a literal in a script. For **prod** the operator sets `max_connections` on their own instance; the same two settings are what tells them which number to set, and they are read-only configuration the admin console displays (`GET /api/console/configuration`).

**Editing them from the admin console is deliberately not part of this.** The console router has no write action at all — a test asserts that — and `Settings` is environment-read and restart-only by design. Making a value editable in the UI is a change to that posture, not a field on a form, so it is its own decision; until then the console shows the number and `.env` sets it.

## Amendment 2026-10-07 — 11. The dev instance keeps its data in a named volume

§Amendment 8 called the dev instance "as throwaway as `forge_dev.db`". That stopped being true when the dev setup began to be used as a demo: services deployed there hold data someone typed in, and `dev.ps1` created the container with no `-v`, so the data sat in an anonymous volume that `docker rm` of the container silently discards.

**Decision:** `dev.ps1` mounts a **named volume** at the image's data directory, so the data outlives the container. Removing the volume is the only way to discard it, and that is a deliberate, separate command. Nothing else in §Amendment 8 moves: the platform still never starts, recreates or removes the instance from application code, and prod remains operator-provisioned (the operator owns the volume, §6).

**The new state this creates:** a volume can now exist without its container, holding the old `forge_admin` role and password. A recreated container must reuse the DSN already in `backend/.env` against that data, and `dev.ps1` must refuse with a remedy, never improvise, when the volume exists but the DSN does not.

## Amendment 2026-10-17 — 12. A project Redis is isolated, authenticated, and re-ensured rather than trusted

A code review (2026-10-16) found that the decision's "on that project's deployment network only" was not what the code did. Every Redis, service and preview alike, was created on Docker's **default bridge** (no `network=`) and only *additionally* attached to private networks, with no password and no restart policy. Preview teardown also matched it: it removed every container carrying the project's label but no build id, which is exactly a project Redis. On the demo host a deployed service's Redis was found missing on 2026-10-17 while the service still ran with its `REDIS_URL`.

**Decision.**
- A project or preview Redis is never attached to the default bridge. The bridge has egress NAT and reaches every container on it, so a Redis there turns `REPLICAOF`/`MIGRATE` from an L3-built preview into both a cross-project copy channel and an exit from `internal=True`.
- Each store gets a generated password carried in `REDIS_URL` (so §Decision's service-env guarantees cover it), and the server-to-server commands are disabled.
- It restarts with the daemon (`unless-stopped`), like the service container.
- **The stored env is not evidence that the store exists.** Every deployment start re-ensures it (start a stopped one, recreate a missing one), because this ADR already made Redis a cache the platform may recreate freely (§Amendment 8's durability argument). A legacy password-less `REDIS_URL` is upgraded at the first re-ensure; this is the one sanctioned change to an issued env value.
- Teardown identifies what it removes by a positive marker. "Has the project label but not a build id" is a description of absence, and absence matched production.
