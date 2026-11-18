# 0022. Local accounts with admin-approved signup; OIDC retained as a mode

- Status: accepted
- Date: 2026-08-26

## Context

Direction as of 2026-08-26, in three parts:

1. **IdP-agnosticism is a product requirement, not a pending decision.** The finished platform must attach to whatever account system the installing organization already runs. Recorded as an amendment on ADR 0018; it is the standing reason no vendor SDK may ever be introduced, and the reason "the IdP is unnamed" must not be filed as a blocker.
2. **The bootstrap admin lives in `.env` with an email *and a password*** — `admin@admin.com` / `admin` during development, changed before live.
3. **Before any SSO is wired, users request an account from the first screen** (email, name, department) and an **admin approves** after checking those details.

(2) and (3) are one change, not two. The platform today has **no concept of a password**: `dev` mode resolves an `X-Forge-User-Id` header against seeded rows, and `oidc` mode delegates the credential entirely to the IdP. Nothing stores, hashes, or verifies a secret anywhere. So a bootstrap admin *with a password* is not a config value — it requires a credential store. And self-service signup requires a gate to replace the org-membership proof that IdP authentication previously supplied for free.

Constraints in play:

- The identity seam (`deps.get_current_user`) and the stateless signed session cookie are **already IdP-independent** — the session layer was built with no OIDC coupling, so a second credential mechanism reuses it as-is.
- `dev` mode is load-bearing for the test suite (430 tests) and the dev switcher; it cannot be removed as part of this change.
- **The platform cannot send email.** No SMTP dependency, no mail service, no mail setting exists (verified across `pyproject.toml` and `.env.example`). Any design step that assumes a delivered message is unbuildable today.
- Prod fail-closed precedent: `FORGE_ENV=prod` + `AUTH_MODE=dev` refuses to start, deliberately with no override flag (ADR 0018 §6).

## Decision

### 1. A third authentication mode: `AUTH_MODE = dev | local | oidc`

`local` authenticates against a password stored on the platform's own `User` row. Mode defaults are **unchanged** (`FORGE_ENV=dev` → `dev`, `prod` → `oidc`); `local` is always set explicitly. Prod auth mode should be an explicit statement, never something a default slides into.

The existing rule that prod + `dev` refuses to start is unchanged and unaffected.

### 2. The identity seam does not move, and the session layer is reused verbatim

`local` is a third branch behind `deps.get_current_user`, and it issues **the same** stateless signed session cookie as `oidc` (`services/platform_session`, 8h sliding / 7d absolute). No new session concept, no session table, and `role` still never travels in the cookie.

Consequence worth stating plainly: everything downstream of the seam — every router, the permission matrix, the self-approval ban, the frontend 401 hook — is untouched by this ADR.

### 3. Credential storage

- `User.password_hash`, **nullable**: OIDC-provisioned users and dev-seeded rows have none.
- Hashed with a purpose-built password hash (bcrypt), never reversible. Never logged, never audited, never present in any response schema — the same handling class as `Project.service_env_json`.
- **`password_hash IS NULL` cannot log in via `local` mode.** Fail closed: a row that never set a password is not a row with an empty password.

### 4. The bootstrap admin gets a password, applied at row creation only

- Reuse the existing `BOOTSTRAP_ADMIN_EMAILS` setting rather than adding a second, near-identically-named one. In `local` mode it must contain **exactly one** entry; more (or none) fails at startup. One declaration site for "who is the first admin".
- New `BOOTSTRAP_ADMIN_PASSWORD`, required in `local` mode.
- The row is created **at startup** (seed), not at first login. In `local` mode nobody can authenticate without an existing row, so there is no login event for JIT creation to hang off.
- Created with `status = ACTIVE`, bypassing the approval gate — there is nobody to approve the first admin.
- **The password is applied only when the row is created**, never on a later startup. Same discipline and same reason as the existing role grant (ADR 0018 §5): re-applying every startup would silently reset a password the admin had changed, and would make the `.env` default immortal.

### 5. Prod refuses a default or weak bootstrap password — no override flag

`FORGE_ENV=prod` with `AUTH_MODE=local` **fails at startup** when `BOOTSTRAP_ADMIN_PASSWORD` is unset, equals `admin`, or is shorter than the documented minimum.

The stated intent to change the default before going live is exactly the kind of intent that outlives the person holding it. ADR 0018 §6 already chose the stricter option in this same situation and declined an override flag; this follows that precedent rather than inventing a second posture.

### 6. Signup is a *request*; the approval gate replaces the IdP's membership proof

ADR 0018 §3 rejected pre-approval on the grounds that "gating account creation would add an admin bottleneck with no security benefit — authentication already proves org membership."

**That reasoning does not survive the removal of the IdP.** With a self-chosen password there is no proof of anything: anyone who can reach the login page could create an account. Admin approval is not new bureaucracy — it is the substitute for the proof `oidc` mode got for free. The two designs are consistent, not contradictory, and **`oidc` mode keeps JIT provisioning exactly as ADR 0018 §3 specifies.**

`User` gains `status = PENDING | ACTIVE | REJECTED` and `department` (nullable free text).

### 7. `status` is enforced at the identity seam, not at the login endpoint

Only `ACTIVE` resolves to a user. A `PENDING` or `REJECTED` row presenting a valid session cookie is `401` — never a logged-in user with fewer permissions.

Load-bearing for the same reason `role` is re-read per request: approval can be revoked, and a session issued before the revocation must stop working on the next request. Enforcing at the login endpoint only would leave an already-issued cookie valid for up to 7 days.

### 8. No email is sent, because the platform cannot send email

Consequences accepted deliberately here rather than discovered during implementation:

- The requester **sets their own password at request time**, so approval needs no invite link or temporary-credential delivery.
- **There is no approval notification.** A pending user retrying login is told they are awaiting approval; the admin informs them out of band.
- **Password reset is admin-mediated only.** No self-service reset exists, because it would require email. Out of scope for the first implementation, and named here as the gap that will be felt first.

### 9. Rejected rows are kept, not deleted

Preserves the audit trail and prevents re-request spam on the same email (`User.email` is unique). An admin can move a `REJECTED` row back to `PENDING` or `ACTIVE`.

### 10. Login attempt limiting ships *with* local login, not after it

`oidc` mode never had a password to guess. `local` mode does, and the platform has **no rate limiting anywhere today**. Unbounded password attempts against a known internal URL are the one genuinely new attack surface this ADR opens, so a per-account attempt counter with a lockout window is part of the same change — not a follow-up.

## Alternatives considered

- **Stay on dev-header mode until an IdP exists.** Rejected. The header stand-in is not authentication at all, so the platform would stay unusable by real users for an unbounded wait on an org decision.
- **Admin creates every account manually; no self-service request.** Fewer moving parts, but it pushes data entry (name, department) onto the admin and leaves the requester with no way in. The request flow was a stated requirement.
- **Email-verified signup with no admin approval.** Needs SMTP the platform does not have, and email possession is a weaker membership proof than a person confirming a name and department against the org chart.
- **Replace OIDC with local accounts entirely (delete the OIDC path).** Rejected: it is built and covered by 37 tests, and it remains the target end state per ADR 0018 §Amendment. The modes coexist.
- **Store the bootstrap password pre-hashed in `.env`.** Better hygiene in principle, but typing a bcrypt hash into `.env` for a *development* default is friction with no gain, given that decision 5 makes prod refuse the default outright.
- **A separate `credentials` table instead of a column on `User`.** Warranted if a user could hold several credentials; with one optional password per row a nullable column keeps the FK for free.

## Consequences

- **Gain:** the platform becomes usable by real employees immediately, with no org-level IdP work on the critical path.
- **Gain:** `local` and `oidc` share one identity seam and one session layer, so adopting SSO later is a config change plus a per-user link — not a rewrite.
- **Give up:** the platform now **stores credentials**. That is a new class of secret in the DB and a new failure mode (weak passwords, credential stuffing) that the IdP previously owned on our behalf. Password policy and lockout are therefore not optional extras, which is why decisions 5 and 10 sit in this ADR rather than in a backlog.
- **Watch out — two provisioning paths now exist** (approved local signup, OIDC JIT). A user who signs up locally and later logs in via OIDC has a row with no `oidc_sub` and a matching email, which today is an `IdentityConflictError` **by design** (the provisioning code deliberately covers exactly this "row with a NULL `sub`" shape). **Linking local rows to IdP subjects is unbuilt and must be decided before any deployment runs both modes.** The safe interim rule is one mode per deployment.
- **Watch out:** `department` is free text shown to the approving admin as evidence. It is **never** an authorization input — roles remain the only authorization axis.
- **Watch out:** the first admin now exists *before* anyone logs in, so `local` mode has a row whose password came from a file on disk. Rotating it is an in-app action; `.env` cannot rotate it after creation (decision 4).

## Note 2026-11-12: operator email exists

A later decision adds SMTP for operator notices. §8 still holds for users: self-service password reset is not built and needs its own decision.
