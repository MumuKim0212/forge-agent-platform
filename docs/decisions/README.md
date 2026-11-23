# Architecture decisions

These are the decision records behind Forge's design. They explain why the platform is shaped the way it is: where unvetted, LLM-generated code runs, what it may touch, and what has to be true before it reaches production. Each record states the context, the decision, the alternatives that were rejected, and the consequences. Several carry dated amendments where a later measurement or review changed the original call.

Source code comments cite these as `ADR NNNN`. Only the records below are published; the numbering has gaps on purpose, because the numbers are the identifiers the code uses.

## Where to start

If you read three, read these:

- [ADR 0003](0003-disposable-build-sandbox.md): the build sandbox and its four invariants. This is the hard trust boundary of the system.
- [ADR 0014](0014-agent-in-sandbox-llm-proxy.md): the agent runs inside the sandbox, and no credential ever does. Cost is measured at the proxy, never reported by the agent.
- [ADR 0032](0032-harvested-workspace-is-untrusted-input.md): what the agent produced is untrusted input when it crosses into the platform.

## Principles and runtime

| # | Decision |
|---|---|
| [0001](0001-final-form-guardrails-first.md) | Design for the final form; isolation, policy and ownership land before convenience features |
| [0002](0002-docker-traefik-single-host.md) | One Linux host, Docker and Traefik, no Kubernetes |
| [0003](0003-disposable-build-sandbox.md) | Disposable hardened build sandbox |
| [0046](0046-gvisor-for-build-and-preview.md) | gVisor as the production runtime for the build and the preview container |
| [0037](0037-label-driven-reconciler.md) | A label-driven reconciler repairs Docker drift from database state |
| [0038](0038-single-worker-lease.md) | One worker per installation, held by a database lease |

## The agent and its output

| # | Decision |
|---|---|
| [0014](0014-agent-in-sandbox-llm-proxy.md) | Agent runs inside the sandbox; credentials stay outside behind an LLM proxy |
| [0029](0029-agent-tool-set-platform-declared.md) | The build agent's tool set is declared by the platform |
| [0032](0032-harvested-workspace-is-untrusted-input.md) | The harvested workspace is untrusted input |
| [0035](0035-openai-second-engine-admin-selected.md) | A second agent engine, selected by an admin, with one proxy dialect per engine |
| [0009](0009-self-modification-code-only.md) | Self-modification is code-level only; no schema changes through natural language |
| [0047](0047-in-build-repair-from-measured-smoke-failures.md) | A build may repair its own output from measured smoke failures, inside one build and one budget |

## Preview, approval and deploy

| # | Decision |
|---|---|
| [0016](0016-preview-separate-ttl-container.md) | A preview is a separate TTL container, not the build image's runtime |
| [0028](0028-generated-content-origin-isolation.md) | Generated content never runs with the platform's origin |
| [0008](0008-auto-approve-prod-deploy.md) | Production deploys are auto-approved when the policy checks pass |

## Data

| # | Decision |
|---|---|
| [0023](0023-per-service-data-services.md) | Per-service data services: one database and role per service in a shared Postgres, one Redis per service |

## Identity and testing

| # | Decision |
|---|---|
| [0018](0018-identity-generic-oidc.md) | Identity through generic OIDC; the platform database owns authorization |
| [0022](0022-local-accounts-approved-signup.md) | Local accounts with admin-approved signup, for organizations without an identity provider |
| [0021](0021-frontend-test-scope.md) | Frontend tests: unit tests, no end-to-end layer, one narrow accessibility exception |
| [0048](0048-approval-read-access.md) | Approval requests are read by approvers, and by the requester and project owner of a request |
