# 0003. Disposable hardened build sandbox (L3)

- Status: accepted
- Date: 2026-06-22

## Context

Unvetted, LLM-generated code must actually execute somewhere to be built and previewed. That execution is the single biggest security exposure in the platform.

## Decision

Build and preview run in a **per-project, single-use hardened container** (L3, volatile). On confirmation, the image is promoted to the operations zone (L4) and may be reused. Starting isolation strength = hardened container; **gVisor is a later runtime-only upgrade** (swappable without code changes).

**Four invariants (non-negotiable):**
1. Single-use — destroyed when the job ends.
2. No Docker socket exposed inside the sandbox.
3. Default-deny network to prod services/DBs/secrets (then allowlist).
4. Memory/CPU caps.

## Alternatives considered

- **Build directly in the ops zone:** removes the L3/L4 trust boundary — unacceptable; unvetted code would touch prod.
- **gVisor/microVM from day one:** stronger isolation but more moving parts up front; deferred as a drop-in runtime upgrade since the 4 invariants already bound the risk.

## Consequences

- Gain: a hard trust boundary (L3 ≠ L4); blast radius of malicious/broken generated code is contained and ephemeral.
- Give up: rebuild cost per run; some latency to spin up/tear down sandboxes.
- Watch out: every future change to the build path must preserve all 4 invariants; security is enforced here + the policy gate (ADR 0008), never by the agent engine.

## Addendum 2026-08-11: microVM candidates evaluated; gVisor remains the upgrade path

The "later runtime-only upgrade" above was re-examined against **Docker Sandboxes** (`sbx`, GA 2026-01-30) — microVM isolation for coding agents, a dedicated Linux kernel per sandbox behind a hardware-virtualisation boundary (VT-x/AMD-V). Its boundary is genuinely stronger than gVisor's syscall interposition, and its design independently converges on ADR 0014: default-deny egress through a host-side proxy that injects the real credential so the agent never holds it. **Read as external confirmation that ADR 0014 needs no revisiting.**

**Rejected as the L3 runtime, for four reasons — none about isolation strength:**

1. **No programmatic API or SDK** — CLI only, and shaped for a human at a terminal (`sbx run claude`). `services/sandbox_runner.py` needs provisioning, log-stream consumption for wall-clock timeout detection, `OOMKilled` inspection for `RESOURCE_LIMIT`, and diff harvest. Those would have to be re-derived on a surface that does not expose them, costing the build failure taxonomy.
2. **Requires KVM** (Linux: Ubuntu 22.04+ x86_64) — a nested-virtualisation dependency on the single host that the platform does not currently have. This is an open blocker for the microVM path.
3. **Workstation-shaped** — secrets in the host OS keychain, egress policy chosen at login from three presets. The platform issues and revokes per-build tokens programmatically and fixes its allowlist in code.
4. **Org-level network policy enforcement sits behind a paid subscription** (Docker AI Governance). Moving a trust boundary the platform currently owns in code onto a vendor SKU contradicts "security is enforced here, never by the agent engine".

Also note the platform's LLM proxy stays regardless: it enforces the per-build token/USD budget and writes `Build.cost_usd`, which all of the platform's cost metering derives from. Adopting `sbx` would mean two proxies, not one.

**What stands:** gVisor is still the upgrade that fits the seam — `--runtime=runsc` is a Docker runtime argument, so it enters the existing `docker`-SDK call site without touching orchestration, failure classification, or the four invariants. Any microVM path is a larger change gated on reason 2.

Uncertain and unverified at evaluation time: `sbx` resource-limit (memory/CPU) configuration is undocumented, and the published CLI reference was empty — so invariant 4 could not be confirmed satisfiable there at all.

### Ordering, if isolation is ever strengthened: build container before preview

Both L3 containers share the host kernel, so the one scenario a stronger runtime addresses is **kernel-level container escape landing the attacker on the host that also runs L4 and the platform process** (single host, ADR 0002). Nothing else improves: deployment of malicious code, prod-network reach, and credential exposure are already owned by the policy gate, the approval gate, invariant 3, and ADR 0014.

**The build container is the one to harden first, and the preview container is the weaker candidate despite being longer-lived and the only one with ingress.** Reasoning:

- Hardening the preview buys only its TTL window. It runs the **same OCI image that promotion puts into L4** (ADR 0016, byte-identical). If that image is hostile, the exposure is L4, and L4's guarantee is "human-confirmed", not "verified safe" — the approval gate is a human risk judgement, not a sandbox.
- The build container executes code that **no artifact records and no check inspects**: package postinstall scripts, one-shot commands the agent ran, code loaded during test runs. Harvest takes the workspace diff only and the policy gate's five checks are static checks over that harvested workspace — neither sees build-time execution history. ADR 0014 §Consequences already names this channel and accepts it.

Note the two criteria diverge: gVisor breaks workloads whose syscalls it does not implement, and the workload more likely to break is the build container (package installs, compilation, test runs) rather than a preview serving HTTP. So the riskier target is also the harder one to validate — doing the easy one first would not reduce actual risk.

### Residual risk re-accepted 2026-08-11; no slice, with a trigger

The escape scenario above **stays accepted** on the original grounds: the four invariants bound it, and the platform is internal-only with tens-to-hundreds of users behind an approval gate. No isolation slice is planned.

The trigger for revisiting is evidence, not a date: **make the sandbox runtime a configuration value and measure `runsc` compatibility in dev.** Without that measurement no microVM or gVisor slice can be estimated at all. This is scoped to the build container only, per §Ordering above.

## Amendment 2026-11-12: gVisor for build and preview (ADR 0046)

The runtime seam built after the 2026-08-11 addendum covers the build container only, so "build and preview… swappable without code changes" above is false for previews. The production decision of 2026-11-12 ends the 2026-08-11 acceptance: ADR 0046 sets out `runsc` as the prod runtime for the build and the preview container after a measurement lane on a Linux host, and puts the preview on the seam (its 2026-11-14 amendment keeps `runsc` opt-in and `runc` the prod default until the open issues it lists are fixed). §Ordering still holds.
