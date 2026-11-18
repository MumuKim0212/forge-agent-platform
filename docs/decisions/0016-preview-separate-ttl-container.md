# 0016. Preview runs as a separate TTL container from the promoted build image

- Status: accepted
- Date: 2026-07-06

## Context

ADR 0003 invariant 1 destroys the build sandbox when the job ends, but the preview must stay reachable *after* the build finishes, for as long as its TTL. Also, what the requester previews should be byte-identical to what later gets promoted to L4 (ADR 0003: "the image is promoted").

## Decision

Build and preview are **two different containers with two different lifecycles**, sharing one artifact:

- A successful build produces an **OCI image** (assembled by L2 from the harvested workspace + the template's pinned, agent-untouchable build manifest — the sandbox has no Docker socket, so it can't build images itself).
- The preview is a **fresh L3-zone container started from that same image**, TTL-bound (config; initial default 60 minutes), with the same hardening as the build sandbox: no Docker socket, default-deny egress, resource caps.
- Preview data services are **disposable and empty** — never prod data.
- L2 registers a Traefik route for the preview and removes it on teardown. Teardown triggers: TTL expiry, a newer successful build of the same project, promotion/confirmation, manual stop.
- Access (initial posture, before SSO): a **signed, expiring preview URL** (token bound to the build, lifetime ≤ preview TTL), shown only on the project detail page; issuance is audit-logged. Anyone on the intranet holding the link can view until expiry — accepted residual risk while no policy gate exists and no SSO forward-auth exists; tighten to session-based auth when SSO lands.

## Alternatives considered

- **Keep the build sandbox alive through the preview window:** stretches "single-use" until it means nothing, couples build resources to human viewing time, and previews a live workspace instead of the deployable image.
- **Preview only after deploy to L4:** defeats the point of preview — unvetted code must never reach L4 (ADR 0003).
- **Platform-session auth on preview from day one:** cleanest, but the dev-identity header can't authenticate a plain browser request to a different host/route without building forward-auth now; deferred to SSO.

## Consequences

- Gain: invariant 1 stays crisp (build container dies at job end); what-you-preview-is-what-you-deploy; preview load is capped and self-cleaning.
- Give up: extra container churn per build; a few seconds of preview startup.
- Watch out: preview containers are still L3 (unvetted code) — they must never gain prod egress; the TTL reaper must be reliable or previews become zombie load on the host.

## Amendment 2026-09-28 (ADR 0028)

**Two corrections to the access bullet.**

**1. The link is not shown "only on the project detail page".** It is rendered by the timeline, which the project detail page embeds, and by the approval console's 프리뷰 열기. Any authenticated user can read it (`GET /approvals`, the timeline).

**2. The residual risk this ADR accepted concerned *viewing* only.** Anyone holding the link may see the preview until it expires, and that posture is unchanged.

What this ADR never considered is the preview **acting as its viewer**. On the platform-origin proxy path, a preview document ran as the platform's own origin and could call the API with the viewer's session. ADR 0028 withholds that authority:

- every platform-origin proxy response is served under a CSP `sandbox` and `nosniff`;
- that path is not mounted when Traefik routes;
- unsafe cross-site and same-site requests are refused.

The watch-out above ("preview containers are still L3") applies to the viewer's browser as well as to the host.
