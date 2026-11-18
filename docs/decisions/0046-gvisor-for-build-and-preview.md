# 0046. gVisor becomes the prod runtime for the build and the preview container

- Status: accepted (2026-11-12; previews included)
- Date: 2026-11-12
- Amends: 0003 (§Decision's "build and preview… swappable without code changes"; §Residual risk re-accepted 2026-08-11)

## Context

ADR 0003 deferred gVisor as "a later runtime-only upgrade, swappable without code changes", and its 2026-08-11 addendum re-accepted the residual escape risk with no slice. A review on 2026-11-12 found:

- **The seam covers only the build container.** `BUILD_CONTAINER_RUNTIME` is passed to the build container and the worker's disk-cap probe. Previews, deploys, the LLM proxy, service Redis and image assembly all use the default runtime, and tests pin that scoping. So ADR 0003's "build and preview… swappable without code changes" is false for previews.
- **Nothing has ever run under `runsc`.** It was recorded as unmeasured (no runsc on the Windows development host). CI runs on Linux with runc and only the stub agent image; the real agent workload has never run on a Linux engine.
- The setting is unvalidated. An unregistered runtime is only logged at worker start, and builds then fail `INFRA_ERROR`.
- **Preview containers run generated, unapproved code as root.** The template image has no `USER` and previews set none; caps are dropped. Deploy containers are the same.
- Inside the build container, setup execs (`mkdir`, `chown`) and the post-run OOM check (`cat memory.events`) run as root, the last one after untrusted code has run.
- Paths likely to change under gVisor (inferred, not measured): the disk watchdog reads the engine's `SizeRw`, and the OOM fallback reads the cgroup file from inside the container.

On 2026-11-12 it was decided that the platform goes to production. That ends the "internal-only, no slice" acceptance of ADR 0003's addendum.

## Decision

1. **`runsc` is the prod runtime for the build container and the preview container**, once the measurement lane below passes. Dev keeps the default runtime.
2. **The preview container joins the runtime seam**, with its own setting (default: the build runtime's value), so a host can run previews under gVisor without code changes. Deploy containers (L4) stay on runc: they run human-approved code, and moving them is a separate decision.
3. **Prod refuses to start when a configured runtime is not registered with the Docker engine** (fail closed, named error). This replaces the log line.
4. **Measure first, on a Linux host with `runsc` registered:** the real agent image, the sandbox and executor integration suites, a real-key build, and a preview with writes. Fix the disk-cap and OOM-classification paths if they break, and re-measure `BUILD_MAX_DURATION_MINUTES`. CI gains a runsc lane if the runner supports it; otherwise the lane is a documented manual gate per release.
5. **Preview containers run as a non-root user.** Whether the template needs a `USER` (and so deploy containers change with it) is measured in the same lane: a preview must behave like the deployment it previews (ADR 0016, byte-identical image).
6. **Root execs in a container that has run untrusted code are removed or justified:** the post-run OOM check reads from the host side, not by exec into the container.

## Ordering

ADR 0003 §Ordering still holds: the build container is hardened first, because it runs code no artifact records. The preview follows in the same slice.

## Alternatives considered

- **Keep the 2026-08-11 acceptance.** Rejected: the production decision ends the internal-only acceptance it rested on.
- **microVM (Docker Sandboxes, Kata).** ADR 0003's addendum reasons stand: no SDK, needs KVM, and nested virtualisation on the prod host is unconfirmed.
- **gVisor for deploy containers too.** Not now: L4 runs human-confirmed code, gVisor's syscall gaps would surface as production outages, and the escape scenario is owned by the approval gate there.

## Consequences

- The build sandbox documentation names the runtimes, the startup check and the non-root preview user. ADR 0003's §Decision sentence on previews is corrected by this ADR.
- **Blocked on a Linux host with `runsc`.** No step of item 4 can run on a Windows development host.
- Expect slower builds under gVisor; the build time budget is re-measured, not assumed.

## Amendment 2026-11-14: measured on a WSL2 test host; runsc stays opt-in, runc stays the prod default

Item 4 was measured on a WSL2 test host (Ubuntu 26.04, kernel 6.18 WSL2, Docker 29.9.3, cgroup v2, `runsc` release-20261109.0, systrap). `runsc` itself runs, but the platform does not work on it yet:

- **Harvest.** The default rootfs overlay keeps sandbox writes inside gVisor, so the tar airlock reads nothing. 51 of 53 suite failures had this cause. A real-key build spent $0.0082 and ended `AGENT_ERROR` because its `result.json` never came back, so the misconfiguration surfaced as an agent failure. **Observed fix (2026-11-14):** a runtime registered with `runtimeArgs: ["--overlay2=none"]` returns the file through `docker cp`.
- **DNS.** Docker's embedded resolver (`127.0.0.11`) is unreachable from gVisor on a user-defined network, so a generated preview cannot resolve `forge-service-postgres`. This happens with both runsc variants. **Observed fix:** with `extra_hosts` (`--add-host name:ip`), name resolution and a TCP round-trip work under runsc with `--overlay2=none`. The platform would have to pass its data-service addresses that way.
- **Disk cap.** The startup probe saw 4096 bytes after an 8 MiB write, so the writable-layer watchdog is blind. Builds keep the harvest bound; a preview would have only its TTL. Whether `--overlay2=none` restores the engine's size reporting is **not measured**, and it decides whether previews may run under runsc at all.
- **What held.** OOM is classified `RESOURCE_LIMIT`: the sandbox dies with exit 128 and both OOM signals fire. Process spawning is about 7.5x slower, small-file work about 1.5x, CPU about 1.2x.

**Decision:** item 1 is **not adopted**. `runsc` stays opt-in through `BUILD_CONTAINER_RUNTIME` and `PREVIEW_CONTAINER_RUNTIME`, and prod defaults to runc. The escape risk ADR 0003 describes is therefore still the accepted residual, now with the hardening of non-root previews and deployments, and no exec after untrusted code. A second measurement round is planned: the `--overlay2=none` registration, `extra_hosts` for data services, a startup airlock probe that covers the harvest direction, and the disk-cap measurement. Its trigger is the platform going beyond one team, or a host where an escape matters more than these costs.
