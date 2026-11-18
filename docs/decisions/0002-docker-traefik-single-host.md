# 0002. Single-host Docker + Traefik runtime

- Status: accepted
- Date: 2026-06-22

## Context

The platform needs to route, run, and TLS-terminate deployed services for tens-to-hundreds of internal users. Options range from a managed PaaS to full Kubernetes.

## Decision

Run everything on the **same Linux machine** using **Docker + Traefik** for routing and HTTPS. No separate PaaS or Kubernetes. (Management UIs like Coolify are a later optional consideration only if needed.)

## Alternatives considered

- **Kubernetes:** real autoscaling and isolation, but operationally heavy and unjustified at this scale; declared a non-goal.
- **External PaaS:** offloads ops but adds cost, external dependency, and weakens the "all on one trusted host" control model.

## Consequences

- Gain: simple operational model; L2 controls Docker directly (API/compose) and registers Traefik routes.
- Give up: no built-in autoscaling; resource contention is a real risk → per-container CPU/memory caps are mandatory.
- Watch out: the platform controlling Docker means control-plane/socket compromise = host takeover → control plane is intranet-only + SSO + least-privilege, and the sandbox never sees the Docker socket (see ADR 0003).
- Scale path: add VMs/hosts when one machine is insufficient, not autoscaling.
