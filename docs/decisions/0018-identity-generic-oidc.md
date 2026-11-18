# 0018. Identity via generic OIDC; the platform DB owns authorization

- Status: accepted
- Date: 2026-07-29

## Context

This work replaces the dev identity stand-in (`deps.get_current_user` resolving an `X-Forge-User-Id` header; frontend `IdentityContext` + `/login` switcher) with real SSO. Three things had to be settled before a spec could be written:

1. **Which IdP.** The org has not named one.
2. **Where authorization comes from** — IdP groups, or the platform's own DB.
3. **What happens to existing dev-mode identities.**

Constraints in play: tens-to-hundreds of internal users; `.env`-driven runtime modes with prod fail-closed on dev identity (an earlier decision on runtime modes required that a prod/dev-identity mix "must never happen silently"); local dev must stay lightweight, and the existing test suite must keep running without an IdP.

The same shape of problem was already solved for git hosting — an undecided vendor behind a thin seam, with config choosing the implementation. This ADR deliberately reuses that pattern rather than inventing a second one.

## Decision

### 1. Generic OIDC via discovery — the IdP is config, not code

No vendor-specific code paths. The platform speaks standard OIDC and discovers endpoints from `{OIDC_ISSUER}/.well-known/openid-configuration`.

Config (`.env`): `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES` (default `openid email profile`).

Any compliant IdP — Google Workspace, Microsoft Entra ID, Okta, self-hosted Keycloak — works by configuration alone. **The open question of which IdP therefore does not need an answer to proceed**; naming the IdP becomes an ops task, not a code change.

### 2. Server-side authorization-code flow with PKCE; httpOnly session cookie

The backend holds the client secret and performs the code exchange; the SPA never sees a token. On success the backend sets an httpOnly, `SameSite=Lax`, `Secure`-in-prod session cookie.

Rejected: issuing a bearer token to the SPA and storing it in `localStorage` (the shape the dev stand-in uses today). A token readable by JavaScript is XSS-exfiltratable, and the frontend is served same-origin with the backend, so a cookie costs nothing.

### 3. `sub` is the identity key; users are provisioned just-in-time

`User` gains a unique `oidc_sub`. Lookup is by `sub`, never by email — email is a mutable attribute and reassignable in some directories. Email and display name are refreshed from claims on every login.

First login creates the `User` row (JIT provisioning). There is no pre-provisioning step: the Requester persona is "any employee", so gating account creation would add an admin bottleneck with no security benefit — authentication already proves org membership.

### 4. The IdP is authoritative for identity; the platform DB is authoritative for authorization

Roles are **not** read from token claims. The IdP answers "who is this?"; the platform DB answers "what may they do?" (the role model is a separate decision).

Why:
- Works with any IdP, keeping decision 1 real — group claims are the least consistent part of OIDC across vendors.
- No dependency on the org provisioning groups for us, and no IdP-admin round-trip to change a role.
- `ApprovalRequest`/`ApprovalDecision`/`AuditLog` already live in the platform DB. Keeping the grant that authorized a decision in the same database as the decision itself makes the audit trail answerable by one query.

Group-claim → role mapping is a possible **additive** layer later (map at login, DB still stores the result). Explicitly not built now.

### 5. Bootstrap admin via config

`BOOTSTRAP_ADMIN_EMAILS` (comma-separated, `.env`) — at login, a matching email is granted `admin` **only when the `User` row is being created**. Not on every login.

This resolves the chicken-and-egg (role management needs an admin; admins are granted by an admin) without a seed script. Applying it only at row creation is the load-bearing detail: re-checking on every login would silently re-promote anyone an admin had deliberately demoted.

### 6. Dev mode is retained and prod is fail-closed

`AUTH_MODE=dev | oidc`, defaulting from `FORGE_ENV`. Dev mode keeps the header stand-in so local development and the existing test suite need no IdP. **`FORGE_ENV=prod` with `AUTH_MODE=dev` fails at startup**, satisfying the earlier requirement that this "must never happen silently". No override flag is provided — the earlier decision allowed one; we are choosing the stricter option, since a "temporary" auth bypass flag in prod is exactly the kind of thing that outlives its reason.

### 7. Dev-mode identities are discarded, not migrated

Decision (2026-07-29): **initialize, don't map.** Dev users are fixtures; no production deployment exists yet.

Mechanically this is a **fresh dev database**, not a data-deleting migration. The Alembic migration only adds columns (`oidc_sub` etc.); it contains no `DELETE`. Developers discard `forge_dev.db` and re-seed.

Rationale for keeping the destruction out of Alembic: a migration that deletes user rows (cascading into projects, approvals, deployments) is a loaded gun aimed at whichever machine runs `alembic upgrade` next. Making it a documented, deliberate local file deletion means no automated path can ever eat data someone cared about.

## Alternatives considered

- **Pick one IdP and integrate its SDK (e.g. Entra-specific).** Fewer abstractions, and normally the right instinct — but here the vendor is genuinely unknown, and OIDC discovery is a standard, not an abstraction we invented. Vendor SDKs would also make the decision expensive to reverse for no gain.
- **IdP groups as the source of roles.** Single source of truth and no role UI to build. Rejected: it makes this work depend on org-side group provisioning that nobody has committed to, couples us to per-vendor claim formats, and splits the audit trail across two systems.
- **SAML.** Still common in enterprises, but heavier (XML, signature handling) and OIDC covers every candidate IdP listed. If an org mandate ever forces SAML, it is a new provider seam at the same place.
- **Session tokens in `localStorage`** — see decision 2.
- **Preserving dev identities via a mapping table.** Rejected; would also mean carrying fixture-shaped rows into the first real deployment.

## Consequences

- Gain: this work is unblocked without an org-level IdP decision; switching IdP later is `.env` only.
- Gain: prod cannot accidentally run with the dev identity switcher.
- Give up: no group-based role automation on day one — the first admin comes from config, the rest from the admin surface (a later slice). With hundreds of users at most, manual grants of two roles is not a real burden.
- Watch out: **cookie domain vs preview hosts.** Previews and deployed services live on their own Traefik hosts, so a platform session cookie scoped to the platform host is not sent to them. This does **not** change the scope here — preview access stays signed-token-based per ADR 0016. Session-based preview auth is now *possible* but is deliberately **out of scope**; revisit it as its own piece of work, and note it would require a parent-domain cookie.
- Watch out: JIT provisioning means anyone who can authenticate at the IdP gets a platform account. That is intended (internal SSO = org membership), but it makes the IdP's own access scope the real perimeter — worth confirming when the IdP is named.

## Amendment 2026-08-26: IdP-agnosticism is a requirement, not a stopgap

Clarification: **the IdP is left unconfigured on purpose, so the finished platform can be attached to whatever account system the installing organization already runs.** Decision 1 above is therefore a **standing product requirement**, not a workaround for an unanswered question.

This amendment exists because the original text is readable the other way. §Context frames the unnamed IdP as an open item ("the org has not named one"), §Alternatives concedes that integrating one IdP's SDK is "normally the right instinct", and the naming was tracked as an open blocker. Nothing in that arrangement forbids a future reader from proposing "the IdP is settled now, so let's simplify onto its SDK" — which would destroy the property being deliberately protected.

What this pins:

- **No vendor SDK, no vendor-specific branch, ever** — not after the IdP is named, not as a simplification, not for one provider's quirk. Protocol libraries (JWT/JWKS primitives) are not vendor SDKs; the distinction already stated in `pyproject.toml` next to `pyjwt` is the line.
- **Naming an IdP is a deployment-time input, not a project decision.** It does not belong among the project's blockers, and its absence blocks nothing.
- A provider that needs behaviour outside plain OIDC discovery is accommodated by **configuration** (e.g. `OIDC_REDIRECT_URL`, scopes) or by a documented failure, never by a code path keyed on which IdP is in use.

Consequence: ADR 0022 adds a `local` account mode alongside `oidc` for organizations that have no IdP to attach, or have not attached one yet. That is the same requirement applied one step further out — the platform must not *require* any particular identity provider, including requiring that one exists at all.
