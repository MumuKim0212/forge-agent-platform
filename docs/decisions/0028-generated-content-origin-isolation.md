# 0028. Generated content never runs with the platform's origin

- Status: accepted. Proposed and accepted 2026-09-28, after a per-layer review with browser measurement.
- Date: 2026-09-28

## Context

ADR 0003 draws the platform's hard trust boundary around **L3**, the place where unvetted, LLM-generated code executes, and hardens every L3 container against the rest of the system. That boundary is enforced for containers. **It is not enforced in the viewer's browser**, and a browser is where a preview is actually used.

### What the platform serves today

Two catch-all routes re-publish generated output **on the platform's own origin**:

| Route | Guarded by | Mounted |
|---|---|---|
| `proxy_preview`: `/api/projects/{id}/preview/{build_id}[/{path}]` | the signed preview token (ADR 0016) | always |
| `proxy_service`: `/api/projects/{id}/service[/{path}]` | nothing | always |

Both are registered in every mode. `create_app` in `main.py` mounts their routers unconditionally, and neither handler checks the mode.

**How they proxy.** Both work by `docker exec`, and forward only the method and the path. They return `Response(content=body, status_code=…)`, which means:

- no `Content-Type`,
- no `X-Content-Type-Options: nosniff`,
- no `Content-Security-Policy`,
- no other isolating header.

A browser therefore sniffs an HTML body and runs it as a document of the platform's origin.

**Why that origin matters.** ADR 0018 §2 put the SPA and the API on one origin, "so a cookie costs nothing". That same fact gives the proxied document everything the platform grants a page of its own:

- the viewer's `forge_session` cookie on every same-origin request;
- readable same-origin API responses;
- in `dev` mode, the identity stored in `localStorage`.

**How easily a viewer reaches it.** In `dev`, and wherever `TRAEFIK_ENABLED=false` (which `prod` accepts; see `test_traefik_enabled_mode_defaults_and_override` in `test_config.py`), the preview URL the UI shows *is* this route. It is linked from the approval console's 프리뷰 열기 (`ApprovalConsolePage.tsx`) and from the timeline (`Timeline.tsx`). Any authenticated user can read it.

### The attack this permits

1. A requester steers their build toward an HTML page with an inline script.
   - **Nothing gates a preview before it is served.** Previews are issued on build success, before any policy check runs, so the content an approver previews is the least-vetted code the platform hands to a browser.
   - **The gate would not catch it later either.** Egress detection scans `.py`/`.js`/`.ts` files for absolute URLs, so neither an inline `<script>` nor a root-relative `fetch('/api/…')` is flagged.
2. An approver does exactly what the console asks and opens the preview.
3. The script reads `GET /api/approvals` and posts `{}` to `/api/approvals/{id}/approve`. The approval is recorded as the approver's. **The self-approval ban is defeated**, and nobody did anything unusual.
4. If the viewer is an admin, the same script can also confirm, roll back or stop deployments, change roles, and deprovision.

`proxy_service` offers the same reach to every *deployed* service with no token at all. Approval of a service is not a review for this: nobody reading a diff looks for `fetch('/api/…')`.

### The residual path once the proxies are gone

When Traefik routes, previews and services live on `{slug}.{apps_base_domain}`, which is another *origin*. But nothing guarantees it is another *site*: no setting names the platform's own public host, and the platform frontend has no production routing yet.

If the two share a registrable domain, the `SameSite=Lax` session cookie **is** sent with a same-site request. Measured against FastAPI 0.141.1:

- **Blocked:** routes with a required body. A non-JSON simple request fails validation, and a JSON one needs a preflight that CORS (allowlist: `localhost:5173`) refuses.
- **Not blocked:** body-less routes execute on a plain simple `POST`, with no preflight. Those are `…/deployments/rollback`, `…/deployments/stop`, `…/unarchive`, `/auth/logout`, and in `local` mode signup `…/reopen` and `…/deactivate`. The browser cannot read the response, but the action has already happened.

### What limits it today, and why that is not enough

- The proxy forwards no request headers, so the viewer's cookie never reaches the container.
- It forwards no body and no query, which limits what a proxied app can do.
- It returns no upstream headers, so the proxied app cannot `Set-Cookie`.
- The session cookie is `httpOnly`, and the preview token expires after 60 minutes.

None of these stops a script from running as the platform's origin, or from making credentialed requests there.

**This exposure was unrecorded.** ADR 0016 accepted that anyone holding a preview link may *view* the preview. That is a confidentiality posture about reading. No earlier decision considered generated code *acting as the viewer*. It was confirmed by reading on 2026-09-27.

### Measured in a browser (2026-09-28)

A throwaway probe served test pages on `localhost:5175` and `:5176`, same host and therefore same site. The probe logged whether each request carried the `forge_session` cookie set by the running platform, and its fetch-metadata headers. It logged presence only, never the cookie value.

| Response shape | Script ran? | What the page had |
|---|---|---|
| **No `Content-Type`, no other header (today's proxy)** | **yes, as HTML** | `localStorage` worked, the cookie was sent on same-origin `GET`/`POST`, and a JSON `POST` went out without a preflight. This is the premise of this ADR, now observed. |
| `text/html`, no isolation | yes | the same, plus a body-less `POST` to the other port **carried the cookie** with `Sec-Fetch-Site: same-site`. The same-site path in §The residual path is real, and the header tells it apart. |
| No `Content-Type` + `nosniff` | **no**: rendered as plain text | nothing |
| `text/plain` + `nosniff` | no | nothing |
| `text/html` + `CSP: sandbox allow-scripts …` + `nosniff` | yes, sandboxed | `self.origin === "null"`, and both `localStorage` and `document.cookie` throw `SecurityError` |

**What the probe could not measure.** It could not see what a sandboxed document's outgoing requests carry: the embedded browser used for the probe blocks every request from an opaque origin client-side (`net::ERR_BLOCKED_BY_CLIENT`). **The decision below does not depend on that measurement**, and §Decision layer 3 explains why.

## Decision

**Content produced by generated code is never given the platform's origin authority.** Three layers, each closing a path the others leave open.

### 1. Every platform-origin proxy response is sandboxed

Both proxy routes attach one fixed header set, built in one named place:

- `Content-Security-Policy: sandbox …`. The document then gets an **opaque origin**:
  - its requests are cross-origin and cross-site to the platform, so a `Lax` cookie is not attached, CORS refuses to let it read responses, and `localStorage` is unavailable;
  - the token list **must never contain** `allow-same-origin`, `allow-top-navigation`, `allow-top-navigation-by-user-activation`, `allow-top-navigation-to-custom-protocols` or `allow-popups-to-escape-sandbox`;
  - the initial grant is `allow-scripts allow-forms allow-popups allow-modals allow-downloads`, which is what a generated web tool plausibly uses and gives none of them origin authority.
- `X-Content-Type-Options: nosniff`, so the browser executes only what is labelled executable.
- **The upstream `Content-Type`, forwarded, and nothing else from upstream.** A preview that is HTML should render, sandboxed. When upstream sends none, the platform sets a non-executable default (`text/plain; charset=utf-8`), never nothing. `Set-Cookie` and every other upstream header stay dropped.

This layer is what protects the configurations where the proxies must exist: `dev`, and `prod` with Traefik off.

### 2. The platform-origin proxies exist only where nothing else routes

**When `traefik_enabled` is true, neither proxy route is mounted.**

- In that mode the UI already hands out the Traefik URLs. `preview_manager.py` says the platform path "still exists but is no longer the one shown to users", and nothing in `app/`, the frontend or the Traefik test suite uses it.
- The routes currently share `projects.router` and `deployments.router` with core API endpoints, so they move to their own router(s), registered conditionally in `create_app`.
- The Traefik forward-auth endpoint (`/api/internal/preview-auth/{build_id}`) is a separate router and is unaffected.

### 3. The API refuses unsafe cross-site and same-site requests (Fetch Metadata)

A platform-wide check refuses any `POST`/`PUT`/`PATCH`/`DELETE` whose `Sec-Fetch-Site` is `cross-site` or `same-site`, **unless its `Origin` is one the operator already trusts in `CORS_ORIGINS`**. It returns `403`.

- `same-origin`, `none`, and an **absent** header pass. Non-browser clients (tests, Traefik's server-to-server forward-auth call, `curl`) send no fetch metadata and are not CSRF vectors.
- **The `CORS_ORIGINS` exemption.** The frontend supports calling the API cross-origin (`VITE_API_BASE_URL` in `api/client.ts`), with `CORS_ORIGINS` as the operator's explicit list of trusted callers, and such calls arrive as `same-site` (measured). Refusing them outright would break a supported configuration.
  - **`null` and `*` are never exemptions**, whatever the configuration says. `null` is exactly what a sandboxed document sends, and `*` would switch the layer off.
- **Why the design does not need the unmeasured cookie behaviour.** Take a sandboxed document's request to the platform:
  - its responses are unreadable, because CORS never allows `Origin: null`;
  - a JSON write needs a preflight that CORS refuses;
  - a simple write is refused here. Fetch Metadata never classifies an opaque initiator as `same-origin` or `same-site`, so the browser sends `cross-site`, and `null` is never exempt.

  Whether a `Lax` cookie rides along therefore changes nothing.
- `GET`/`HEAD` are not checked. That leaves top-level navigation, the OIDC callback and forward-auth untouched, which is correct only while **safe methods stay free of meaningful side effects**. Today's GET writes are session re-issue, login-state cookies and the `last_active_at` bump on service traffic, and none of them is an action a third party would want to cause.
- This closes the same-site sibling-subdomain path above **without** a platform-origin setting, which does not exist, and without touching any client call.
- It also refuses a sandboxed document's unsafe requests outright, so layer 1 does not rest on `SameSite` semantics for opaque origins alone.

### 4. The written record is corrected to match

- **The platform's role/route permission matrix (not published).** The row "Preview and deployed-service proxying … signed token only" is split in two.
  - **Preview:** signed token.
  - **Deployed service:** no identity and no token, deliberately at parity with its Traefik route, since end-user auth for deployed services is out of scope.
  - The identity-free rule for these rows stands. Adding authentication would not help anyway: in this attack the viewer *is* authenticated, and that is the problem.
- **ADR 0016 §Decision (access).** It is amended on acceptance. The link is shown on the project detail page, the timeline and the approval console, to any authenticated user. The *viewing* posture is unchanged; the platform's *authority* is withheld by this ADR.
- **The build sandbox's preview container description** carries the same correction, plus the sandbox rule from layer 1.
- **A constraint for the platform frontend's production routing:** the SPA and the API are either **same-origin**, or the SPA's origin is listed in `CORS_ORIGINS`. Layer 3 refuses every other same-site or cross-site write, so a sibling-host SPA left off that list locks users out of every write. Generated services' hosts must **never** be added to `CORS_ORIGINS`.

## Alternatives considered

- **A separate origin for proxied content in every mode** (a dedicated port or host even in `dev`). This is the strongest isolation and the textbook answer. Rejected for now because it needs a second listener and host configuration in dev, where the platform deliberately assumes no Traefik. The sandbox achieves an opaque origin with one header and no infrastructure. Revisit if a generated service legitimately needs same-origin features (cookies, storage) on the proxy path.
- **Mounting the proxies only in `dev`, with no headers.** Rejected: `prod` with Traefik off is an accepted configuration, and `dev` is exactly where the approval-console click happens.
- **Authenticating the proxy routes.** Rejected: identity-free by design, it would break routing that authenticates by token, and it does not address the flaw. The flaw is not that an unauthenticated user gets in; it is that an authenticated user's authority leaks out.
- **A policy check that scans generated code for calls to the platform API.** Rejected: content scanning of LLM output is bypassable by construction (string building, encoding, one indirection). The policy gate is framed as static analysis on outputs, not the isolation mechanism (ADR 0003). The boundary has to hold whatever the code says.
- **An `Origin` allowlist instead of Fetch Metadata.** Rejected as the *primary* signal because it needs the platform's public origin as configuration, and no such setting or routing design exists. The dev proxy also sends an `Origin` that differs from the backend's `Host`. It survives in a narrow role, as the exemption: the *already-configured* `CORS_ORIGINS` list decides which non-same-origin callers are trusted, and no new setting is introduced.
- **CSRF tokens or a required custom header.** Workable and browser-independent, but they touch every client call and many tests, and `dev` mode already sends a custom header, which blurs the signal. Fetch Metadata gives the same browser-side guarantee with no client change. Keep a custom header as the fallback if a supported browser is ever found not to send fetch metadata.
- **`Content-Disposition: attachment` or forcing `text/plain` on everything.** Safe, but it makes previews unviewable, which removes the reason they exist.

## Consequences

- **Gain.** Generated code can no longer act as whoever views it. The self-approval ban holds against its most natural bypass, and an admin opening a preview no longer lends it admin authority.
- **Gain.** Same-site CSRF against body-less actions is closed before the platform's production domain layout is even decided.
- **Give up.** A preview on the proxy path runs in an opaque origin: no cookies, no `localStorage`, no same-origin requests to itself. The practical loss is small, because the proxy already drops query strings, so root-relative subresources lack the token and fail. It is still a real limit on what a dev-mode preview can demonstrate.
- **Give up.** In Traefik mode, bookmarked platform-origin preview or service links stop working. The UI never showed them in that mode.
- **Watch out: the check lets requests without fetch metadata through.** A browser that does not send `Sec-Fetch-Site` gets no protection from layer 3. Layers 1-2 still apply, and every evergreen browser sends it (Chromium 76+, Firefox 90+, Safari 16.4+).
- **Watch out: safe methods must stay safe.** A future `GET` that performs an action is outside layer 3 by construction.
- **Watch out: the links.** Preview links must keep `rel="noreferrer"`, which implies `noopener`. Without it, even a sandboxed preview could navigate the opener tab (tab-nabbing).
- **Not closed by this ADR: cookie tossing from a same-site generated service.** Such a service can set a parent-domain `forge_session` that the browser sends alongside the real one. The `__Host-` cookie prefix closes it, but it requires `Secure`, and a default install is plaintext. Revisit when TLS becomes the default, or when the platform frontend's production routing lands. **Closed for prod on 2026-11-12:** prod requires TLS and the cookies take the `__Host-` prefix; prod cookies were already `Secure`. Dev keeps the open item.
- **How this is verified, as a chain of three links:**
  1. **The browser half is established** by §Measured: what each header combination does in a real browser.
  2. **The platform half is pinned by tests:**
     - unit tests for the header set, conditional mounting and the Fetch Metadata rule, including the exemption and its two refusals;
     - Docker integration tests that assert the headers on **real** proxied responses (`test_preview_manager_integration.py` and `test_deploy_integration.py` already drive the real proxy route against real containers).
  3. **An end-to-end run in an ordinary desktop browser** covering a sandboxed page's attempted write. It is desirable but not required, because the embedded browser used for automated checks blocks opaque-origin requests itself and cannot tell "refused by the platform" from "refused by the tool".
- **Watch out: `CORS_ORIGINS` is now also a CSRF trust list.** An operator who adds a host to it grants that host write access with the user's cookie. That was already true through CORS for JSON writes; it is now true for simple writes too. The `.env.example` entry must say so.
- **Revisit triggers:**
  - the first generated service that needs same-origin features on the proxy path;
  - the platform frontend's production routing;
  - TLS becoming the default install.

## Amendment 2026-10-17: `CORS_ORIGINS` is validated at startup

§3 says `null` and `*` are never *CSRF exemptions*. A code review on 2026-10-16 found that the CORS middleware itself still honoured them: with credentials enabled, Starlette answers `*` by echoing whatever origin asks. Reads are not covered by Fetch Metadata, so with `CORS_ORIGINS=*` a generated service at `{slug}.{apps_base_domain}`, which is same-site with the platform so the Lax cookie rides along, could read a visitor's user list, timelines and approvals.

**Decision.** The platform refuses to start when `CORS_ORIGINS` contains `*`, `null`, a value that is not a bare `scheme://host[:port]` origin, or any origin whose host is `apps_base_domain` or a subdomain of it. The rule that generated-service hosts must never be listed becomes a startup check instead of a sentence.
